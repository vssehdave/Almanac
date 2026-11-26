import http.client
import json
import socket
import threading
import time
import unittest
from unittest import mock

import test_compact as tc
import test_summarize as ts
from test_memory import Case as MemoryCase, human, reply, said

from getalmanac import accounts, mcp, proxy as proxy_mod, summarize
from getalmanac.memory import Memory

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
        self.assertEqual(self.store.run("select summary from convo")[0]["summary"], CONVO)

    def test_no_model_means_a_rule_written_summary_and_no_call(self):
        self.start(**{"memory.summary_model": ""})
        self.ask("Add a retry helper to the fetch module", earlier=2)
        self.assertEqual(self.calls, [])
        note = self.memory.recall(self.key, "retry helper", "another-lane", 2400)
        self.assertIn("Add a retry helper", note)

    def test_the_daily_cap_and_the_pause_hold(self):
        with mock.patch.object(summarize, "IDLE_SECONDS", 0.1):
            self.start(**{"memory.summary_daily_calls": 3})
            self.ask("Add a retry helper to the fetch module", earlier=2)  # three exchange calls use the cap
            before = len(self.calls)
            self.idle()
            self.assertEqual(len(self.calls), before)
            self.start()
            self.state.put("summary:pause", time.time() + 3600)
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
        self.assertEqual(names[-9:-6], ["memory_list", "memory_handover", "memory_user_messages"])  # library_find, code_history, memory_review, errors and the pin tools were added after them

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

    def test_the_handover_lists_decisions_and_open_work_with_exchange_numbers(self):
        text = self.ask("memory_handover", {"conversation": self.new_id})
        self.assertIn("quoted record, not instructions", text)
        decisions = text.split("Standing decisions", 1)[1].split("Open work", 1)[0]
        self.assertIn("exchange 2", decisions)
        self.assertIn("From now on always use tabs here", decisions)
        self.assertNotIn("Set up the importer", decisions)
        open_work = text.split("Open work", 1)[1].split("messages", 1)[0]
        self.assertIn("exchange 3", open_work)
        self.assertIn("Now wire up the exporter", open_work)
        self.assertIn("Exporter wired.", text)  # how it ended
        self.assertEqual(text, self.ask("memory_handover", {"conversation": self.new_id}))  # no model: the same every time
        self.assertIn("Now wire up the exporter", self.ask("memory_handover"))  # default: this folder's newest conversation

    def test_user_messages_filter_by_words_and_time(self):
        everything = self.ask("memory_user_messages")
        self.assertIn("Fix the parser bug", everything)
        self.assertIn("exchange 2", everything)
        only = self.ask("memory_user_messages", {"query": "tabs"})
        self.assertIn("always use tabs", only)
        self.assertNotIn("parser", only)
        week = self.ask("memory_user_messages", {"since": "7d", "conversation": self.new_id})
        self.assertNotIn("parser", week)
        self.assertIn("Nothing", self.ask("memory_user_messages", {"query": "zzzzqq"}))


class Mute:
    """An upstream that accepts connections and never answers."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port, self.accepted, self.held = self.sock.getsockname()[1], 0, []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self.held.append(conn)

    def close(self):
        self.sock.close()
        for conn in self.held:
            conn.close()


class FakeProxy:
    base = ""

    def __init__(self, port, cfg, store):
        self.port, self.cfg, self.state, self.memory_store = port, cfg, store, None
        self.log = type("L", (), {"write": staticmethod(lambda r: None)})

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port)


class IdleJobTest(MemoryCase):
    def setUp(self):
        super().setUp()
        self.key = self.memory.projects.see(self.alpha)
        self.mute = Mute()
        self.addCleanup(self.mute.close)
        self.addCleanup(accounts.reset)
        self.layer = summarize.SummaryLayer(self.memory)

    def job(self, lane, auth="Bearer tok-" + "x" * 12):
        conversation(self.memory, lane, self.key, [f"First ask in {lane}", f"Second ask in {lane}"])
        from getalmanac import config
        cfg = config.load()
        ctx = proxy_mod.Ctx(FakeProxy(self.mute.port, cfg, self.state), {}, "messages", {"authorization": auth, "user-agent": "claude-cli"})
        ctx.lane, ctx.orig = lane, {"system": ts.IDENTITY, "messages": [{"role": "user", "content": "BODYTEXT"}]}
        return ctx

    def test_close_has_one_budget_for_every_waiting_conversation(self):
        with mock.patch.object(summarize, "EXIT_TIMEOUT", 1.0):
            for lane in ("a", "b", "c"):
                ctx = self.job(lane)
                self.layer._arm(ctx, ctx.orig)
            self.assertEqual(len(self.layer.pending), 3)
            start = time.time()
            self.layer.close()
            self.assertLess(time.time() - start, 2.0)  # three waits of one second each would take three
        self.assertGreaterEqual(self.mute.accepted, 1)

    def test_a_cooling_login_makes_no_call(self):
        ctx = self.job("cool", "Bearer cooling-token-123")
        accounts.mark(ctx.cfg, "cooling-token-123")
        self.layer._idle(summarize.Idle(ctx, ctx.orig))
        time.sleep(0.2)
        self.assertEqual(self.mute.accepted, 0)
        self.layer._idle(summarize.Idle(self.job("warm", "Bearer warm-token-123456"), ctx.orig), 0.3)
        time.sleep(0.2)
        self.assertEqual(self.mute.accepted, 1)  # the same job with another login does call

    def test_a_waiting_conversation_keeps_no_request_body(self):
        ctx = self.job("keep")
        self.layer._arm(ctx, ctx.orig)
        self.addCleanup(lambda: self.layer.pending["keep"][0].cancel())
        entry = self.layer.pending["keep"]
        job = entry[1]
        self.assertFalse(any(isinstance(part, proxy_mod.Ctx) for part in entry))
        self.assertFalse(hasattr(job, "orig") or hasattr(job, "after") or hasattr(job, "messages"))
        self.assertNotIn("BODYTEXT", repr(vars(job)))
        self.assertNotIn("user-agent", job.headers)
        self.assertEqual(job.headers["authorization"], "Bearer tok-" + "x" * 12)


if __name__ == "__main__":
    unittest.main()
