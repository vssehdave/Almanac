import http.client
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from helpers import Upstream
from test_cheatsheet import Fresh, claude_system, ctx, transform
from test_routing import LARGE, body, convo

from getalmanac import config, lessons
from getalmanac.memory import Memory
from getalmanac.store import State, Store

OLD = {"cause": "Edits made before reading the file", "sign": "An Edit fails because the text was guessed"}
NEW = {"cause": "Tests run against the main checkout", "sign": "A fix passes without PYTHONPATH set to the worktree"}


class Proxy:
    def __init__(self, url):
        self.url, self.base = url, ""

    def connect(self):
        return http.client.HTTPConnection(self.url.split("//")[1].split(":")[0], int(self.url.rsplit(":", 1)[1]), timeout=5)


class DistilTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="almanac-lessons-")
        self.store = Store(os.path.join(tmp, "a.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, State(self.store.path.with_name("state.db")))
        self.state = self.memory.state
        parent = self.memory._convo("main-lane", "s", "p", "t")
        child = self.memory._convo("sub-lane", "s", "p", "t", parent=parent["id"], agent_type="coder")
        self.store.run("update convo set review = 'reviewed', review_note = ? where id = ?",
                       ("Edited files it had not read; two Edits failed.", child["id"]))
        self.parent = parent["id"]
        self.state.put("lesson:coder", [OLD])
        reply = {"content": [{"type": "text", "text": json.dumps({"lessons": [OLD, dict(OLD, sign="Sharper sign"), NEW]})}],
                 "usage": {"input_tokens": 5, "output_tokens": 5}}
        self.upstream = Upstream(lambda req: (200, {"content-type": "application/json"}, json.dumps(reply).encode()))
        self.addCleanup(self.upstream.close)

    def distil(self, model="claude-haiku-5-5"):
        cfg = config.load()
        cfg["memory"]["summary_model"] = model
        there = SimpleNamespace(cfg=cfg, state=self.state, headers={"authorization": "Bearer t"}, proxy=Proxy(self.upstream.url))
        lessons.distil(self.memory, there, {"system": "You are Claude Code."}, self.parent)

    def test_a_repeat_merges_into_the_existing_lesson(self):
        self.distil()
        self.assertEqual(len(self.upstream.requests), 1)
        sent = json.loads(self.upstream.requests[0]["body"])["messages"][0]["content"]
        self.assertIn(OLD["cause"], sent)  # the current lessons go out, so a repeat can be merged into them
        self.assertIn("two Edits failed", sent)
        self.assertEqual(self.state.get("lesson:coder"), [OLD, NEW])
        self.distil()  # each verdict is taken once
        self.assertEqual(len(self.upstream.requests), 1)

    def test_nothing_is_distilled_without_a_summary_model(self):
        self.distil(model="")
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.state.get("lesson:coder"), [OLD])


class FreezeTest(Fresh):
    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp(prefix="almanac-lessons-")
        self.state = State(os.path.join(tmp, "state.db"))
        self.addCleanup(self.state.close)
        self.state.put("lesson:coder", [OLD])

    def system(self, lane, turns=1):
        work = body(convo([{"text": f"step {n}"} for n in range(turns)]), LARGE, system=claude_system())
        there = ctx(klass="subagent", agent="a1", agent_type="coder")
        there.state, there.lane = self.state, lane
        transform(work, there)
        return json.dumps(work["system"])

    def test_a_thread_keeps_its_text_and_a_new_thread_gets_the_new_lesson(self):
        first = self.system("lane-1")
        self.assertIn(OLD["cause"], first)
        self.state.put("lesson:coder", [OLD, NEW])
        self.assertEqual(self.system("lane-1", turns=2), first)  # byte for byte: the cached prefix holds
        self.assertIn(NEW["cause"], self.system("lane-2"))


if __name__ == "__main__":
    unittest.main()
