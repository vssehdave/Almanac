import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import test_summarize as ts
from helpers import Rig
from test_memory import human, make_repo, reply

from getalmanac import cli, config, embed, memory as memory_mod, semantic
from getalmanac.memory import Memory, MemoryLayer, ProjectLayer
from getalmanac.store import Store

CONCEPTS = [("cache", "cache prefix proxy"), ("bread", "bread sourdough flour loaf baguette bake"),
            ("retry", "retry backoff jitter"), ("money", "invoice cent rounding refund penny bill")]
BREAD = "How do I bake sourdough with rye flour?"
CACHE = "Why does the proxy drop the cache after a tool loads?"


def fake_vector(text) -> bytes:
    """One axis per concept, so which texts are near each other is known in advance."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    vec = [float(sum(w in words for w in group.split())) for _, group in CONCEPTS] + [0.1]
    norm = sum(v * v for v in vec) ** 0.5
    return embed.pack([v / norm for v in vec])


def make_fake(name="fake-1"):
    return semantic.Embedder(name, lambda texts: [fake_vector(t) for t in texts])


FAKE = make_fake()


class Case(unittest.TestCase):
    fake = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-sem-")
        self.model = FAKE if self.fake else None
        patches = [mock.patch.dict(os.environ, {"ALMANAC_HOME": self.tmp})]
        if self.fake:
            patches.append(mock.patch.object(semantic, "load", lambda: self.model))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.store = Store(os.path.join(self.tmp, "almanac.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.alpha = make_repo(self.tmp, "alpha", "https://github.com/Someone/Alpha.git")
        self.key = self.memory.projects.see(self.alpha)

    def add(self, lane, prompt, answer="Done."):
        self.memory.index(lane, "s", self.key, [human(prompt)], reply(answer))

    def search(self, query, **kw):
        return [h["title"] for h in self.memory.search(query, project=self.key, **kw)]

    def leaf(self, title_start):
        return dict(self.store.run("select * from node where kind = 'leaf' and title like ?", (title_start + "%",))[0])


class SearchTest(Case):
    def test_a_paraphrase_with_no_shared_word_is_found_only_by_meaning(self):
        self.add("l1", BREAD, "Mix flour, water and starter.")
        self.add("l2", CACHE, "The prefix changed.")
        self.assertEqual(self.memory.embed_pending(), 2)
        self.assertEqual(self.search("baguette recipe"), [BREAD])
        config.set_value("memory.semantic", "false")
        self.assertEqual(self.search("baguette recipe"), [])

    def test_a_vector_below_the_minimum_cosine_is_not_returned(self):
        self.add("l1", BREAD)
        self.memory.embed_pending()
        self.assertEqual(self.search("zebra migration"), [])

    def test_words_and_meaning_are_merged_with_the_shared_hit_first(self):
        self.add("l1", CACHE)
        self.add("l2", "Explain the prefix invalidation")  # no word of the query, same meaning
        self.memory.embed_pending()
        self.assertEqual(self.search("proxy cache"), [CACHE, "Explain the prefix invalidation"])
        config.set_value("memory.semantic", "false")
        self.assertEqual(self.search("proxy cache"), [CACHE])

    def test_results_are_the_same_every_time_and_ties_go_by_id(self):
        self.add("l1", BREAD)
        self.add("l2", BREAD)
        self.memory.embed_pending()
        self.store.run("update node set ts = 5 where kind = 'leaf'")
        first = [h["id"] for h in self.memory.search("baguette recipe", project=self.key)]
        self.assertEqual(first, sorted(first))
        self.assertEqual(len(first), 2)
        self.assertEqual(first, [h["id"] for h in self.memory.search("baguette recipe", project=self.key)])

    def test_the_project_and_the_current_conversation_are_respected(self):
        self.add("l1", BREAD)
        self.memory.embed_pending()
        self.assertEqual(self.memory.search("baguette recipe", project="git:github.com/someone/beta"), [])
        self.assertEqual(self.search("baguette recipe", exclude_lane="l1"), [])
        self.assertEqual(len(self.memory.search("baguette recipe")), 1)

    def test_without_a_model_or_with_the_setting_off_results_are_the_keyword_ones(self):
        self.add("l1", CACHE)
        self.add("l2", "Explain the prefix invalidation")
        self.memory.embed_pending()
        queries = ["proxy cache", "baguette recipe", "prefix"]
        with_vectors = {q: self.memory.search(q, project=self.key) for q in queries}
        config.set_value("memory.semantic", "false")
        off = {q: self.memory.search(q, project=self.key) for q in queries}
        config.set_value("memory.semantic", "true")
        self.model = None
        none = {q: self.memory.search(q, project=self.key) for q in queries}
        self.assertEqual(off, none)
        self.assertEqual([h["title"] for h in off["proxy cache"]], [CACHE])
        self.assertEqual(off["baguette recipe"], [])
        self.assertNotEqual(with_vectors["proxy cache"], off["proxy cache"])


class VectorTest(Case):
    def test_vectors_of_another_embedder_are_ignored_and_queued_again(self):
        self.add("l1", BREAD)
        self.memory.embed_pending()
        self.assertEqual(self.memory.dense_counts("fake-1"), (1, 0))
        self.model = make_fake("fake-2")
        self.assertEqual(self.search("baguette recipe"), [])  # the fake-1 vector is not compared
        self.assertEqual(self.memory.dense_counts("fake-2"), (0, 1))
        self.assertEqual(self.memory.embed_pending(), 1)
        self.assertEqual(self.search("baguette recipe"), [BREAD])
        self.assertEqual(self.leaf("How do I bake")["dname"], "fake-2")

    def test_an_existing_database_is_migrated_and_keeps_its_exchanges(self):
        path = os.path.join(self.tmp, "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            create table convo (id integer primary key, lane text unique not null, session text not null, project text not null,
                title text not null default '', started real not null, updated real not null, turns integer not null default 0);
            create table node (id integer primary key, convo integer not null, project text not null, parent integer,
                kind text not null, seq integer not null, ts real not null, title text not null, text text not null,
                mark text not null default '', vec blob, summary text not null default '', tries integer not null default 0);
            create virtual table node_fts using fts5(title, text, tokenize='porter unicode61');
            insert into convo values (1, 'old', 's', 'p', 'old', 1, 1, 1);
            insert into node (id, convo, project, kind, seq, ts, title, text) values
                (1, 1, 'p', 'leaf', 0, 1, 'Bake sourdough', 'Asked: bake sourdough with rye flour');
            insert into node_fts (rowid, title, text) values (1, 'Bake sourdough', 'Asked: bake sourdough with rye flour');
        """)
        old.commit()
        old.close()
        memory = Memory(Store(path))
        self.addCleanup(memory.store.close)
        self.assertEqual(memory.dense_counts("fake-1"), (0, 1))
        self.assertEqual(memory.embed_pending(), 1)
        self.assertEqual([h["title"] for h in memory.search("baguette recipe", project="p")], ["Bake sourdough"])
        self.assertEqual(memory.counts()["exchanges"], 1)
        Memory(memory.store)  # opening it again is harmless

    def test_a_new_summary_makes_the_vector_again_from_the_summary(self):
        self.add("l1", "Add a helper to the fetch module")
        self.memory.embed_pending()
        before = self.leaf("Add a helper")["dvec"]
        node = self.leaf("Add a helper")["id"]
        self.memory.set_summary(node, "Retry helper", "Added backoff and retry to fetch.")
        row = self.store.run("select dvec, dname from node where id = ?", (node,))[0]
        self.assertEqual((row["dvec"], row["dname"]), (None, ""))
        self.assertEqual(self.memory.embed_pending(), 1)
        after = self.store.run("select dvec from node where id = ?", (node,))[0]["dvec"]
        self.assertEqual(after, fake_vector("Retry helper\nAdded backoff and retry to fetch."))
        self.assertNotEqual(after, before)

    def test_a_vector_made_from_text_that_changed_meanwhile_is_dropped(self):
        self.add("l1", "Add a helper to the fetch module")
        node = self.leaf("Add a helper")["id"]

        def encode(texts):
            self.memory.set_summary(node, "Retry helper", "Added backoff and retry to fetch.")  # lands during the call
            return [fake_vector(t) for t in texts]

        self.model = semantic.Embedder("fake-1", encode)
        self.memory.embed_pending()
        self.assertIsNone(self.store.run("select dvec from node where id = ?", (node,))[0]["dvec"])

    def test_a_pass_embeds_the_newest_first_and_stops_at_its_limit(self):
        for i in range(5):
            self.add(f"l{i}", f"{BREAD} number {i}")
        self.assertEqual(self.memory.embed_pending(limit=2), 2)
        self.assertEqual(self.memory.dense_counts("fake-1"), (2, 3))
        done = [r["title"] for r in self.store.run("select title from node where kind = 'leaf' and dvec is not null")]
        self.assertEqual(sorted(done), [f"{BREAD} number 3", f"{BREAD} number 4"])
        self.assertEqual(self.memory.embed_pending(limit=10), 3)
        self.assertEqual(self.memory.embed_pending(), 0)

    def test_a_failing_model_leaves_the_exchanges_waiting(self):
        self.add("l1", BREAD)

        def broken(texts):
            raise RuntimeError("no")

        self.model = semantic.Embedder("fake-1", broken)
        self.assertEqual(self.memory.embed_pending(), 0)
        self.assertEqual(self.memory.dense_counts("fake-1"), (0, 1))
        self.assertEqual(self.search("bake sourdough"), [BREAD])  # words still work

    def test_no_model_does_nothing(self):
        self.add("l1", BREAD)
        self.model = None
        self.assertEqual(self.memory.embed_pending(), 0)

    def test_the_topic_vector_is_still_the_hashed_one(self):
        self.add("l1", BREAD)
        self.memory.embed_pending()
        topic = self.store.run("select vec, dvec from node where kind = 'topic'")[0]
        self.assertEqual(len(embed.unpack(topic["vec"])), embed.DIM)
        self.assertIsNone(topic["dvec"])


class RecallTest(Case):
    def test_a_later_vector_never_changes_a_recall_note_already_sent(self):
        self.add("old-lane", BREAD, "Mix flour, water and starter.")
        self.memory.embed_pending()
        rig = Rig(layers=[ProjectLayer(self.memory), MemoryLayer(self.memory)], store=self.store)
        self.addCleanup(rig.close)
        env = f"# Environment\n - Primary working directory: {self.alpha}\n"
        body = {"model": "m", "tools": [{"name": "Bash", "description": "x", "input_schema": {"type": "object"}}],
                "messages": [human("a baguette recipe for dinner"), {"role": "system", "content": [{"type": "text", "text": env}]}]}
        rig.messages(body)
        rig.records()
        first = rig.upstream.requests[-1]["body"]
        self.assertIn("sourdough", json.loads(first)["messages"][0]["content"][-2]["text"])  # found by meaning
        self.add("later", "Proof the loaf overnight", "Cold proof for twelve hours.")
        self.memory.embed_pending()
        self.model = make_fake("fake-2")
        self.memory.embed_pending()
        rig.messages(body)
        rig.records()
        self.assertEqual(rig.upstream.requests[-1]["body"], first)


class SummaryVectorTest(ts.Case):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="almanac-sem-")
        for patch in (mock.patch.dict(os.environ, {"ALMANAC_HOME": home}), mock.patch.object(semantic, "load", lambda: FAKE)):
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()

    def test_the_worker_embeds_the_exchange_and_again_after_its_summary(self):
        self.ask("Add a retry helper to the fetch module")
        leaf = self.leaves()[0]
        self.assertEqual(leaf["summary"], ts.SUMMARY)
        self.assertEqual(leaf["dname"], "fake-1")
        self.assertEqual(leaf["dvec"], fake_vector(f"{ts.TITLE}\n{ts.SUMMARY}"))


class CommandTest(Case):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["semantic", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_status_index_and_the_switch(self):
        for i in range(3):
            self.add(f"l{i}", f"{BREAD} number {i}")
        with mock.patch.object(semantic, "missing", lambda: []), mock.patch.object(semantic, "present", lambda: True), \
                mock.patch.object(semantic, "NAME", "fake-1"):
            code, out, _ = self.run_cli("status")
            self.assertEqual(code, 0)
            self.assertIn("0 exchange(s) have one, 3 waiting", out)
            self.assertIn("by meaning (fake-1) and words", out)
            code, out, _ = self.run_cli("index")
            self.assertEqual(code, 0)
            self.assertIn("embedded 3 of 3 exchange(s)", out)
            self.assertIn("3 exchange(s) have one, 0 waiting", self.run_cli("status")[1])
            self.assertEqual(self.run_cli("off")[0], 0)
            self.assertFalse(config.load()["memory"]["semantic"])
            self.assertIn("by words only", self.run_cli("status")[1])
            self.assertEqual(self.run_cli("index")[0], 1)
            self.assertEqual(self.run_cli("on")[0], 0)
            self.assertTrue(config.load()["memory"]["semantic"])
        self.assertEqual(self.run_cli("nonsense")[0], 2)

    def test_install_names_the_packages_and_says_what_it_contacts(self):
        with mock.patch.object(semantic, "missing", lambda: ["numpy"]):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 1)
        self.assertIn('pip install "getalmanac[semantic]"', err)
        with mock.patch.object(semantic, "missing", lambda: []), mock.patch.object(semantic, "present", lambda: False), \
                mock.patch.object(semantic, "download", lambda: 90_000_000):
            code, out, _ = self.run_cli("install")
        self.assertEqual(code, 0)
        self.assertIn("huggingface.co", out)
        self.assertIn("No conversation text is sent", out)
        with mock.patch.object(semantic, "missing", lambda: []), mock.patch.object(semantic, "present", lambda: False), \
                mock.patch.object(semantic, "download", mock.Mock(side_effect=OSError("offline"))):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 1)
        self.assertIn("the download failed", err)


class LoadTest(unittest.TestCase):
    def test_the_core_never_imports_the_optional_packages(self):
        code = ("import sys, getalmanac.cli, getalmanac.memory, getalmanac.proxy, getalmanac.semantic\n"
                "assert not {'numpy', 'onnxruntime', 'tokenizers', 'huggingface_hub'} & set(sys.modules), sorted(sys.modules)")
        subprocess.run([sys.executable, "-c", code], check=True, env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))

    def test_without_the_packages_or_the_model_nothing_loads(self):
        with mock.patch.object(semantic, "missing", lambda: ["numpy"]), mock.patch.dict(semantic._loaded, clear=True):
            self.assertIsNone(semantic.load())
        with mock.patch.object(semantic, "missing", lambda: []), mock.patch.object(semantic, "present", lambda: False), \
                mock.patch.dict(semantic._loaded, clear=True):
            self.assertIsNone(semantic.load())


@unittest.skipIf(semantic.missing() or not semantic.present(), "needs the semantic packages and the downloaded model")
class RealModelTest(Case):
    fake = False

    def test_a_paraphrase_with_no_shared_word_ranks_above_an_unrelated_text(self):
        invoice = "Invoice totals come out a cent short after we round every line item first."
        dark = "Add a dark mode toggle to the settings page"
        self.add("l1", invoice, "Rounding each line drops fractions; round once at the end.")
        self.add("l2", dark, "Added the toggle.")
        query = "why is the bill off by a penny"
        self.assertEqual(self.memory.embed_pending(), 2)
        model = semantic.load()
        near, far, asked = model.encode([invoice, dark, query])
        close, distant = semantic.cosines(asked, [near, far])
        self.assertGreater(close, distant + 0.2)
        self.assertEqual(self.search(query), [invoice])
