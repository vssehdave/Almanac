import unittest
from unittest import mock

import test_import as ti
import test_semantic as tsem

from getalmanac import config, semantic, summarize
from getalmanac.memory import FACTS


class Base(ti.ImportBase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load()
        self.cfg["memory"]["summary_model"] = ""  # no summary call unless a test turns it on

    def leaf(self, n=0):
        return self.store.run("select id from node where kind = 'leaf' order by seq, id")[n]["id"]

    def stored(self, n=1, **kw):
        t = self.session(**kw)
        for i in range(n):
            t.turn(ti.stamp(2, i), f"question {i} about the parser", f"answer {i}", tool=("Edit", {"file_path": self.a + "/a.py"}))
        self.run_import(t.write())


class Facts(Base):
    def test_facts_are_stored_cleaned_capped_and_found(self):
        self.stored()
        node = self.leaf()
        self.memory.set_summary(node, "Parser work", "Changed the parser.", "done",
                                ["Rotate the staging credential every 90 days", "x" * 400, "Rotate the staging credential every 90 days",
                                 7, "api_key = 'abcdefghijklmnop12345'", "f3", "f4", "f5"])
        rows = [r["text"] for r in self.store.run("select text from fact where node = ?", (node,))]
        self.assertLessEqual(len(rows), FACTS)
        self.assertEqual(rows.count("Rotate the staging credential every 90 days"), 1)
        self.assertTrue(all(len(t) <= 140 for t in rows))
        self.assertFalse(any("abcdefghijklmnop" in t for t in rows))
        hit = self.memory.search("rotate staging credential", limit=3)[0]
        self.assertEqual(hit["id"], node)
        self.assertTrue(hit["snippet"].startswith("Facts: "))
        self.assertIn("staging credential", hit["snippet"])

    def test_a_fact_adds_no_score_to_an_exchange_found_another_way(self):
        t = self.session()
        for i in range(3):
            t.turn(ti.stamp(2, i), f"question {i} about the parser", f"answer {i}", tool=("Edit", {"file_path": self.a + "/a.py"}))
        t.turn(ti.stamp(2, 3), "an unrelated chore", "done", tool=("Edit", {"file_path": self.a + "/b.py"}))
        self.run_import(t.write())
        ids = [r["id"] for r in self.store.run("select id from node where kind = 'leaf' order by seq, id")]
        before = [h["id"] for h in self.memory.search("question about the parser", limit=8)]
        self.assertEqual(sorted(before), sorted(ids[:3]))
        self.memory.set_summary(before[-1], "T", "S", "done", ["question about the parser"])  # a fact on the last-ranked hit
        self.memory.set_summary(ids[3], "T", "S", "done", ["question about the parser"])  # a fact on an exchange nothing else finds
        after = [h["id"] for h in self.memory.search("question about the parser", limit=8)]
        self.assertEqual(after, before + [ids[3]])  # the order holds; the fact-only exchange comes last
        shown = {h["id"]: h["snippet"] for h in self.memory.search("question about the parser", limit=8)}
        self.assertIn(" | Facts: ", shown[before[-1]])  # a text match is kept and the fact added
        self.assertTrue(shown[ids[3]].startswith("Facts: "))  # no text match: the fact is the snippet

    def test_a_summary_without_facts_still_works(self):
        self.stored()
        node = self.leaf()
        self.memory.set_summary(node, "Parser work", "Changed the zebra module.", "done")
        self.assertEqual(self.store.run("select count(*) n from fact")[0]["n"], 0)
        self.assertEqual(self.store.run("select facts from node where id = ?", (node,))[0]["facts"], 0)
        self.assertEqual(self.memory.search("zebra module", limit=3)[0]["id"], node)

    def test_facts_go_with_their_conversation(self):
        self.stored()
        self.memory.set_summary(self.leaf(), "Parser work", "Changed it.", "done", ["a quokka fact"])
        self.assertEqual(len(self.memory.search("quokka fact")), 1)
        self.memory.forget(project=self.key(self.a), force=True)
        self.assertEqual(self.store.run("select count(*) n from fact")[0]["n"], 0)
        self.assertEqual(self.store.run("select count(*) n from fact_fts where fact_fts match 'quokka'")[0]["n"], 0)

    def test_the_search_stays_inside_its_project(self):
        self.stored()
        self.memory.set_summary(self.leaf(), "Parser work", "Changed it.", "done", ["a quokka fact"])
        self.assertEqual(self.memory.search("quokka fact", project="git:github.com/me/beta"), [])

    def test_parse_reads_facts(self):
        body = {"content": [{"type": "text", "text": '{"title": "T", "summary": "S", "status": "done", "facts": ["a", 3, "b"]}'}]}
        self.assertEqual(summarize._parse(body), ("T", "S", "done", ["a", "b"]))
        old = {"content": [{"type": "text", "text": '{"title": "T", "summary": "S"}'}]}
        self.assertEqual(summarize._parse(old), ("T", "S", "", None))


class Vectors(Base):
    def setUp(self):
        super().setUp()
        self.model = tsem.make_fake()
        patch = mock.patch.object(semantic, "load", lambda: self.model)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg["memory"]["semantic"] = True

    def test_embed_all_leaves_nothing_without_a_vector(self):
        self.stored(n=3)
        self.memory.set_summary(self.leaf(), "T", "S", "done", ["retry backoff jitter", "invoice refund"])
        self.assertEqual(self.memory.waiting(self.model.name)[1], 2)
        with mock.patch.object(config, "load", lambda: self.cfg):
            self.assertEqual(self.memory.embed_all(batch=2), 0)
        self.assertEqual(self.memory.waiting(self.model.name), (0, 0))

    def test_embed_all_reports_that_no_model_is_installed(self):
        self.stored()
        with mock.patch.object(semantic, "load", lambda: None), mock.patch.object(config, "load", lambda: self.cfg):
            self.assertIsNone(self.memory.embed_all())
        self.assertEqual(self.memory.waiting(self.model.name)[0], 1)

    def test_a_fact_is_found_by_meaning(self):
        self.stored()
        self.memory.set_summary(self.leaf(), "T", "S", "done", ["backoff and jitter on each attempt"])
        with mock.patch.object(config, "load", lambda: self.cfg):
            self.memory.embed_all()
            found, near = self.memory._facts("retry", ["retry"], {"retri"}, 1, None, None)
        self.assertEqual(found, [])  # no shared word
        self.assertEqual([t for _, t in near], ["backoff and jitter on each attempt"])


class Recall(Base):
    def test_decisions_then_open_work_then_hits_inside_the_budget(self):
        t = self.session()
        t.turn(ti.stamp(1, 0), "From now on never touch the legacy exporter", "Noted.")
        for i in range(1, 12):
            t.turn(ti.stamp(1, i), f"Please continue with the refactor part {i} of the parser " + "x" * 200, "Done part. " * 20, tool=("Edit", {"file_path": self.a + "/a.py"}))
        t.turn(ti.stamp(0, 0), "Finish the parser rewrite, then check the exporter", "Partly done, what next?")
        self.run_import(t.write())
        note = self.memory.recall(self.key(self.a), "parser rewrite", "new-lane", 2400)
        self.assertIn("Standing decisions", note)
        self.assertIn("never touch the legacy exporter", note)
        self.assertLess(note.index("Standing decisions"), note.index("Open work:") if "Open work:" in note else len(note))
        self.assertLessEqual(len(note), 2400 + len("<system-reminder>\n\n</system-reminder>"))
        for limit in (600, 1200):
            small = self.memory.recall(self.key(self.a), "parser rewrite", "new-lane", limit)
            self.assertLessEqual(len(small), limit + len("<system-reminder>\n\n</system-reminder>"))

    def test_open_work_is_listed_and_a_decision_is_not_repeated_as_a_hit(self):
        t = self.session().turn(ti.stamp(0, 0), "Always keep the zebra cache small", "Noted.")
        t.turn(ti.stamp(0, 5), "Start the zebra refactor now", "I began; shall I go on?")
        self.run_import(t.write())
        note = self.memory.recall(self.key(self.a), "zebra", "other", 2400)
        self.assertIn("Open work:", note)
        self.assertEqual(len([line for line in note.splitlines() if line.startswith("- [") and "zebra cache small" in line]), 1)

if __name__ == "__main__":
    unittest.main()
