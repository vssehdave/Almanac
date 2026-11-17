import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import test_compact as tc
from helpers import Rig, sse
from test_memory import make_repo

from getalmanac import compact, memory as memory_mod, summarize
from getalmanac.memory import Memory, MemoryLayer, ProjectLayer
from getalmanac.status import combine
from getalmanac.store import Store
from getalmanac.summarize import SummaryLayer

MODEL = "claude-haiku-5-5"
IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
TITLE = "Retry helper added to fetch"
SUMMARY = "Added exponential backoff with jitter to retry.py. Decided to cap retries at five."
JSON_HEADERS = {"content-type": "application/json"}


def message(text, usage=None):
    body = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": text}],
            "usage": usage or {"input_tokens": 50, "output_tokens": 20}}
    return 200, JSON_HEADERS, json.dumps(body).encode()


GOOD = message(json.dumps({"title": TITLE, "summary": SUMMARY}))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-sum-")
        self.db_path = os.path.join(self.tmp, "a.db")
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.alpha = make_repo(self.tmp, "alpha", "https://github.com/Someone/Alpha.git")
        self.key = self.memory.projects.see(self.alpha)
        self.verdict, self.calls = GOOD, []
        self.start()

    def start(self, **overrides):
        layers = [ProjectLayer(self.memory), MemoryLayer(self.memory), SummaryLayer(self.memory)]
        self.rig = Rig(respond=self.respond, layers=layers, store=self.store, overrides=overrides)
        self.addCleanup(self.rig.close)

    def respond(self, req):
        if json.loads(req["body"]).get("model") == MODEL:
            self.calls.append(req)
            return self.verdict
        return 200, {"content-type": "text/event-stream"}, sse("Added the retry helper in retry.py.")

    def ask(self, prompt, extra=None, tools=True, earlier=0):
        """Send `prompt`; with `earlier`, as the end of a conversation that already has that many finished exchanges."""
        messages = []
        for n in range(earlier):
            messages += [tc.human(prompt if n == 0 else f"{prompt} again {n}"),
                         {"role": "assistant", "content": [{"type": "text", "text": "Added the retry helper in retry.py."}]}]
        messages.append(tc.human(prompt if not earlier else f"{prompt} again {earlier}"))
        body = {"model": "m", "system": [{"type": "text", "text": IDENTITY, "cache_control": {"type": "ephemeral"}},
                                         {"type": "text", "text": f"# Environment\n - Primary working directory: {self.alpha}\n"}],
                "messages": messages}
        if tools:
            body["tools"] = tc.TOOLS
        self.rig.messages(body, extra=extra)
        self.rig.records()

    def leaves(self):
        return [dict(r) for r in self.store.run("select * from node where kind = 'leaf' order by id")]


class SummaryTest(Case):
    def test_the_summary_is_stored_and_searchable(self):
        self.ask("Add a retry helper to the fetch module")
        leaf = self.leaves()[0]
        self.assertEqual((leaf["title"], leaf["summary"]), (TITLE, SUMMARY))
        hit = self.memory.search("exponential backoff jitter", project=self.key)[0]  # words only the summary has
        self.assertEqual(hit["title"], TITLE)
        self.assertTrue(self.memory.read(hit["id"])["text"].startswith("Summary: Added exponential"))
        topic = self.memory.read(leaf["parent"])
        self.assertEqual(topic["title"], TITLE)
        self.assertIn(SUMMARY[:40], topic["text"])
        self.assertEqual([c["title"] for c in self.memory.outline(leaf["convo"])[0]["children"]], [TITLE])
        note = self.memory.recall(self.key, "retry helper backoff", "another-lane", 2400)
        self.assertIn(f"{TITLE}: ", note)

    def test_the_call_is_one_plain_request_made_as_the_same_user(self):
        self.ask("Add a retry helper to the fetch module", extra={"user-agent": "claude-cli/9"})
        self.assertEqual(len(self.calls), 1)
        call, sent = self.calls[0], json.loads(self.calls[0]["body"])
        self.assertEqual(call["headers"]["authorization"], "Bearer test-token")
        self.assertEqual(call["headers"]["anthropic-beta"], "a,b,c")
        self.assertNotIn("user-agent", call["headers"])
        self.assertFalse([k for k in call["headers"] if k.startswith("x-claude-code")])
        self.assertEqual(call["path"], "/v1/messages?beta=true")
        self.assertEqual(sent["system"], [{"type": "text", "text": IDENTITY}, {"type": "text", "text": summarize.INSTRUCTION}])
        self.assertEqual((sent["model"], sent["max_tokens"], "stream" in sent, "tools" in sent), (MODEL, 1000, False, False))
        self.assertIn("retry helper", sent["messages"][0]["content"])
        used = summarize.today(self.store)
        self.assertEqual((used["calls"], used["in"], used["out"]), (1, 50, 20))
        self.assertIn("1 exchange(s) summarized by " + MODEL, summarize.line(self.memory, self.rig.cfg))

    def test_credentials_never_reach_the_database_or_the_log(self):
        self.ask("Add a retry helper to the fetch module", extra={"x-api-key": "sk-ant-secret-key-value"})
        self.assertEqual(self.calls[0]["headers"]["x-api-key"], "sk-ant-secret-key-value")
        self.store.run("pragma wal_checkpoint(truncate)")
        data = b"".join(Path(p).read_bytes() for p in (self.db_path, self.db_path + "-wal", self.rig.log_path) if os.path.exists(p))
        self.assertIn(b"retry helper", data)
        for secret in (b"test-token", b"secret-key-value", b"Bearer"):
            self.assertNotIn(secret, data)

    def test_rule_text_stays_when_the_call_fails_and_each_exchange_gets_three_tries(self):
        q = "A question about the retry helper"  # one conversation, resumed: each request sees the exchanges so far
        self.verdict = (500, JSON_HEADERS, b"{}")
        self.ask(q)
        self.verdict = message("sorry, no json here")
        self.ask(q, earlier=1)
        self.assertEqual([(r["summary"], r["title"].startswith("A question")) for r in self.leaves()], [("", True)] * 2)
        self.assertFalse(summarize.paused_until(self.store))
        self.ask(q, earlier=2)
        self.assertEqual(max(r["tries"] for r in self.leaves()), 3)
        self.assertTrue(all(r["summary"] == "" for r in self.leaves()))
        before = len(self.calls)
        self.verdict = GOOD
        self.ask(q, earlier=3)
        self.assertEqual(len(self.calls) - before, 3)  # the exchange that used up its tries is left alone
        self.assertEqual(len([r for r in self.leaves() if r["summary"]]), 3)

    def test_a_login_only_carries_its_own_conversations_text(self):
        self.verdict = (500, JSON_HEADERS, b"{}")
        self.ask("Alice asked about the retry helper payroll", extra={"authorization": "Bearer alice"})
        self.assertEqual(len(self.calls), 1)
        self.verdict = GOOD
        self.ask("Bob asked about something else entirely", extra={"authorization": "Bearer bob"})
        self.assertEqual(len(self.calls), 2)  # Bob's exchange only; Alice's waits for her own conversation
        self.assertEqual(self.calls[-1]["headers"]["authorization"], "Bearer bob")
        self.assertNotIn("payroll", self.calls[-1]["body"].decode())
        self.assertEqual([bool(r["summary"]) for r in self.leaves()], [False, True])

    def test_a_thinking_block_before_the_text_still_stores_the_summary(self):
        body = {"type": "message", "role": "assistant", "usage": {"input_tokens": 5, "output_tokens": 9},
                "content": [{"type": "thinking", "thinking": "hmm", "signature": "s"},
                            {"type": "text", "text": json.dumps({"title": TITLE, "summary": SUMMARY})}]}
        self.verdict = (200, JSON_HEADERS, json.dumps(body).encode())
        self.ask("Add a retry helper to the fetch module")
        self.assertEqual(self.leaves()[0]["summary"], SUMMARY)
        self.assertFalse([r for r in self.rig.records() if r["kind"] == "summary"])  # a stored summary logs nothing

    def test_a_call_that_stores_nothing_leaves_one_line_in_the_log(self):
        self.verdict = (500, JSON_HEADERS, b"{}")
        self.ask("Add a retry helper to the fetch module")
        self.assertEqual(self.leaves()[0]["summary"], "")
        lines = [r for r in self.rig.records() if r["kind"] == "summary"]
        self.assertEqual([(r["status"], r["why"]) for r in lines], [(500, "status")])
        self.assertEqual(set(lines[0]), {"ts", "kind", "status", "why"})

    def test_a_failing_upstream_is_asked_once_not_once_per_exchange(self):
        self.verdict = (500, JSON_HEADERS, b"{}")
        self.ask("A question about the retry helper", earlier=3)  # four exchanges are indexed at once
        self.assertEqual(len(self.calls), 1)

    def test_an_allowance_error_pauses_for_an_hour(self):
        self.verdict = (429, JSON_HEADERS, b'{"type":"error","error":{"type":"rate_limit_error"}}')
        self.ask("Add a retry helper to the fetch module")
        self.assertEqual(len(self.calls), 1)
        until = summarize.paused_until(self.store)
        self.assertAlmostEqual(until, time.time() + 3600, delta=60)
        self.assertIn("paused until", summarize.line(self.memory, self.rig.cfg))
        self.verdict = GOOD
        self.ask("A second retry helper question")
        self.assertEqual(len(self.calls), 1)
        self.store.put("summary:pause", time.time() - 1)  # the hour is over
        self.ask("A third retry helper question")
        self.assertGreater(len(self.calls), 1)

    def test_a_refused_login_pauses_too(self):
        self.verdict = (401, JSON_HEADERS, b"{}")
        self.ask("Add a retry helper to the fetch module")
        self.assertTrue(summarize.paused_until(self.store))

    def test_the_daily_cap_holds(self):
        self.start(**{"memory.summary_daily_calls": 2})
        for n in range(4):
            self.ask(f"Question {n} about the retry helper")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len([r for r in self.leaves() if r["summary"]]), 2)
        self.store.put("summary:day", {"day": "2000-01-01", "calls": 99, "in": 0, "out": 0})  # a new day
        self.ask("Question 5 about the retry helper")
        self.assertEqual(len(self.calls), 3)  # each of these questions is its own conversation

    def test_the_models_status_refines_the_rule_and_a_decision_is_never_downgraded(self):
        def verdict(status):
            return message(json.dumps({"title": TITLE, "summary": SUMMARY, "status": status}))

        self.verdict = verdict("done")  # the rules call the newest exchange open
        self.ask("Add a retry helper to the fetch module")
        self.assertEqual(self.leaves()[0]["status"], "done")
        self.verdict = verdict("urgent")  # not a status: the rule's stays
        self.ask("A second retry helper question")
        self.assertEqual(self.leaves()[1]["status"], "open")
        self.verdict = verdict("noise")
        self.ask("From now on keep retries at five in the fetch module")
        self.assertEqual(self.leaves()[2]["status"], "decision")
        self.assertEqual(self.leaves()[2]["mstatus"], "noise")  # the model's word is kept, the rule's wins
        self.assertEqual(summarize._parse(json.loads(message('{"title": "a", "summary": "b"}')[2])), ("a", "b", ""))

    def test_the_model_cannot_make_an_exchange_a_standing_decision(self):
        self.verdict = message(json.dumps({"title": TITLE, "summary": SUMMARY, "status": "decision"}))
        self.ask("Add a retry helper to the fetch module")
        (leaf,) = self.leaves()
        self.assertNotEqual(leaf["status"], "decision")  # compaction lists only this column under Standing decisions
        self.assertEqual(leaf["status"], "open")  # what the rules said
        self.assertEqual(combine("done", "decision"), "done")
        self.assertEqual(combine("decision", "done"), "decision")  # the rules' own decision still stands

    def test_the_models_status_survives_the_next_exchange(self):
        self.verdict = message(json.dumps({"title": TITLE, "summary": SUMMARY, "status": "open"}))
        self.ask("Add a retry helper to the fetch module")
        self.verdict = GOOD
        self.ask("Add a retry helper to the fetch module", earlier=1)  # the first exchange is no longer the newest
        self.assertEqual([r["status"] for r in self.leaves()], ["open", "open"])

    def test_nothing_is_sent_when_the_model_is_not_set_or_the_request_is_not_the_main_thread(self):
        self.start(**{"memory.summary_model": ""})
        self.ask("Add a retry helper to the fetch module")
        self.assertEqual((len(self.calls), len(self.rig.upstream.requests)), (0, 1))
        self.assertIn("off", summarize.line(self.memory, self.rig.cfg))
        self.start()
        self.ask("A subagent question about the retry helper", extra={"x-claude-code-agent-id": "agent-7"})
        self.ask("A toolless question about the retry helper", tools=False)
        self.assertEqual(len(self.calls), 0)


class MigrationTest(unittest.TestCase):
    def test_a_database_from_before_model_summaries_is_upgraded(self):
        path = os.path.join(tempfile.mkdtemp(prefix="almanac-mig-"), "old.db")
        old = sqlite3.connect(path)
        old.executescript(memory_mod.SCHEMA.replace(", summary text not null default '', tries integer not null default 0", ""))
        old.execute("insert into convo (id, lane, session, project, started, updated, turns) values (1, 'l', 's', 'p', 0, 0, 1)")
        old.execute("insert into node (id, convo, project, kind, seq, ts, title, text) values (1, 1, 'p', 'topic', 0, 0, 'old', '')")
        old.execute("insert into node (id, convo, project, parent, kind, seq, ts, title, text) "
                    "values (2, 1, 'p', 1, 'leaf', 0, 0, 'old title', 'Asked: x\n\nAnswer: y')")
        old.execute("insert into node_fts (rowid, title, text) values (2, 'old title', 'Asked: x')")
        old.commit()
        old.close()
        store = Store(path)
        self.addCleanup(store.close)
        memory = Memory(store)
        Memory(store)  # opening it again changes nothing
        self.assertEqual(dict(store.run("select summary, tries from node where id = 2")[0]), {"summary": "", "tries": 0})
        memory.set_summary(2, "New title", "A summary about gizmos.")
        self.assertEqual(memory.search("gizmos", project="p")[0]["title"], "New title")
        self.assertEqual(memory.summaries("l"), {0: ("New title", "A summary about gizmos.")})

    def test_a_database_from_before_statuses_is_upgraded(self):
        path = os.path.join(tempfile.mkdtemp(prefix="almanac-mig-"), "old.db")
        old = sqlite3.connect(path)
        old.executescript(memory_mod.SCHEMA.replace(",\n    status text not null default 'done', mstatus text not null default ''", ""))
        old.execute("insert into convo (id, lane, session, project, started, updated, turns) values (1, 'l', 's', 'p', 0, 0, 1)")
        old.execute("insert into node (id, convo, project, kind, seq, ts, title, text) values (2, 1, 'p', 'leaf', 0, 0, 't', 'Asked: x')")
        old.commit()
        old.close()
        self.assertNotIn("status", [r[1] for r in sqlite3.connect(path).execute("pragma table_info(node)")])
        store = Store(path)
        self.addCleanup(store.close)
        memory = Memory(store)
        Memory(store)
        self.assertEqual(memory.statuses("l"), {0: "done"})
        self.assertEqual(memory.status_counts()["done"], 1)


class CompactionTest(tc.LayerCase):
    def test_a_later_outline_uses_the_summary_and_a_frozen_prefix_does_not_change(self):
        messages = tc.history(8)
        first = self.send(messages)
        lane = self.rig.records()[-1]["lane"]
        ids = self.memory.leaves(lane)
        self.memory.set_summary(ids[0], "Importer overview", "Part 1 reads rows and the codeword is zebra1.")
        grown = messages + [{"role": "assistant", "content": [{"type": "text", "text": "The exporter mirrors it."}]},
                            tc.human("Thanks, one more thing")]
        self.assertEqual(json.dumps(self.send(grown)[:3]), json.dumps(first[:3]))  # the frozen prefix is untouched
        later = compact.build_prefix(messages, len(messages) - 1, ids, 6000, self.memory.summaries(lane))
        line = [x for x in later[0]["content"][-1]["text"].splitlines() if x.startswith(f"- [{ids[0]}]")][0]
        self.assertTrue(line.startswith(f"- [{ids[0]}] Importer overview -> Part 1 reads rows"))
        self.assertNotIn("Importer overview", json.dumps(first))


if __name__ == "__main__":
    unittest.main()
