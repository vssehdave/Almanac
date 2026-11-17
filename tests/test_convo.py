import json
import time
import unittest
from unittest import mock

import test_compact as tc
import test_summarize as ts
from test_memory import Case as MemoryCase, human, reply, said

from getalmanac import mcp, summarize

CONVO = "CONVOSUMMARY the importer was rewritten and tabs were chosen."


def convo_reply(text=CONVO):
    return ts.message(json.dumps({"summary": text}))


def conversation(memory, lane, key, asks, final="All done."):
    """Index a conversation: each ask answered, the last one with `final`."""
    messages = []
    for n, ask in enumerate(asks):
        messages.append(human(ask))
        if n < len(asks) - 1:
            messages.append(said(f"Answer {n + 1}."))
    memory.index(lane, "s-" + lane, key, messages, reply(final))


class ConvoSummaryTest(ts.Case):
    def idle(self, wait=0.6):
        time.sleep(wait)  # the timer fires, then the worker runs the job
        self.rig.proxy.drain()

    def sent_to_claude(self):
        return [r["body"] for r in self.rig.upstream.requests if json.loads(r["body"])["model"] == "m"][-1]

    def convo_calls(self):
        return [c for c in self.calls if any(b["text"] == summarize.CONVO_INSTRUCTION for b in json.loads(c["body"])["system"])]

    def test_an_idle_conversation_gets_a_summary_that_a_new_conversation_sees(self):
        with mock.patch.object(summarize, "IDLE_SECONDS", 0.1):
            self.ask("Add a retry helper to the fetch module", earlier=2)
            self.verdict = convo_reply()
            self.idle()
        self.assertEqual(len(self.convo_calls()), 1)
        note = self.memory.recall(self.key, "retry helper", "another-lane", 2400)
        self.assertIn(CONVO, note)
        self.assertIn("Ended on: asked", note)  # how it ended: the last thing the person asked

    def test_no_model_means_a_rule_written_summary_and_no_call(self):
        self.start(**{"memory.summary_model": ""})
        self.ask("Add a retry helper to the fetch module", earlier=2)
        self.assertEqual(self.calls, [])
        note = self.memory.recall(self.key, "retry helper", "another-lane", 2400)
        self.assertIn("Add a retry helper", note)
        self.assertIn("Ended on: asked", note)
        self.assertIn("Added the retry helper in retry.py.", note)

    def test_the_daily_cap_and_the_pause_hold(self):
        with mock.patch.object(summarize, "IDLE_SECONDS", 0.1):
            self.start(**{"memory.summary_daily_calls": 3})
            self.ask("Add a retry helper to the fetch module", earlier=2)  # three exchange calls use the cap
            before = len(self.calls)
            self.idle()
            self.assertEqual(len(self.calls), before)
            self.start()
            self.store.put("summary:pause", time.time() + 3600)
            self.ask("Another helper question here", earlier=2)
            before = len(self.calls)
            self.idle()
            self.assertEqual(len(self.calls), before)

    def test_a_running_conversations_bytes_do_not_change_when_summaries_arrive(self):
        body = {"model": "m", "system": [{"type": "text", "text": ts.IDENTITY},
                                         {"type": "text", "text": f"# Environment\n - Primary working directory: {self.alpha}\n"}],
                "messages": [tc.human("Add a retry helper to the fetch module")], "tools": tc.TOOLS}
        with mock.patch.object(summarize, "IDLE_SECONDS", 0.1):
            self.ask("Add a retry helper to the fetch module", earlier=2)
            self.rig.messages(json.loads(json.dumps(body)), session="99999999-2222-3333-4444-555555555555")
            self.rig.records()
            first = self.sent_to_claude()
            self.verdict = convo_reply()
            self.idle()
            self.rig.messages(json.loads(json.dumps(body)), session="99999999-2222-3333-4444-555555555555")
            self.rig.records()
        self.assertEqual(self.sent_to_claude(), first)


class ConvoToolsTest(MemoryCase):
    def ask(self, name, args=None):
        out = mcp.handle(self.memory, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": args or {}}}, self.alpha)["result"]
        self.assertFalse(out["isError"], out["content"][0]["text"])
        return out["content"][0]["text"]

    def setUp(self):
        super().setUp()
        self.key = self.memory.projects.see(self.alpha)
        conversation(self.memory, "old", self.key, ["Fix the parser bug", "Check the lexer too"], "Lexer fixed.")
        conversation(self.memory, "new", self.key, ["Set up the importer", "From now on always use tabs here",
                                                    "Now wire up the exporter"], "Exporter wired.")
        self.store.run("update convo set updated = ? where lane = 'old'", (time.time() - 10 * 86400,))
        self.store.run("update node set ts = ? where convo = (select id from convo where lane = 'old')", (time.time() - 10 * 86400,))
        self.new_id = self.store.run("select id from convo where lane = 'new'")[0]["id"]

    def test_the_tool_list_ends_with_the_new_tools(self):
        names = [t["name"] for t in mcp.handle(self.memory, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.alpha)["result"]["tools"]]
        self.assertEqual(names[-1:], ["memory_list"])

    def test_list_filters_by_time_and_project(self):
        everything = self.ask("memory_list")
        self.assertIn("Fix the parser bug", everything)
        self.assertIn("Set up the importer", everything)
        recent = self.ask("memory_list", {"since": "3d"})
        self.assertIn("Set up the importer", recent)
        self.assertNotIn("Fix the parser bug", recent)
        old = self.ask("memory_list", {"until": "3d"})
        self.assertIn("Fix the parser bug", old)
        self.assertNotIn("Set up the importer", old)
        self.assertIn("Ended on: asked", everything)
        self.assertIn("No conversation", self.ask("memory_list", {"since": "2999-01-01"}))



if __name__ == "__main__":
    unittest.main()
