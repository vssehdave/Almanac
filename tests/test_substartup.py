import json
import os
import tempfile
import unittest

from helpers import Rig

from getalmanac import startup
from getalmanac.memory import Memory
from getalmanac.store import State, Store
from test_startup import SKILLS, body

SUB = {"x-claude-code-agent-id": "a1", "x-claude-code-agent-type": "Explore", "x-claude-code-request-class": "subagent"}
NAMES = [f"mcp__big__tool{i}" for i in range(9)] + ["mcp__small__a", "mcp__small__b", "WebFetch"]


def with_names(b, names):
    text = b["messages"][1]["content"][0]["text"]
    b["messages"][1]["content"][0]["text"] = text.replace("WebFetch\nWebSearch", "\n".join(names))
    return b


class Case(unittest.TestCase):
    def setUp(self):
        store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-db-"), "a.db"))
        state = State(store.path.with_name("state.db"))
        self.rig = Rig(layers=[startup.SkillsLayer(), startup.ToolsLayer(), startup.SubTtlLayer()], store=store, state=state)
        Memory(store, state)  # the tables a real store has
        self.addCleanup(self.rig.close)
        self.addCleanup(store.close)

    def send(self, b, sub=True, **more):
        self.rig.messages(b, extra=dict(SUB, **more) if sub else None)
        return self.rig.sent()

    def deferred(self, sent):
        return sorted(t["name"] for t in sent["tools"] if t.get("defer_loading"))


class SkillTest(Case):
    def test_a_sub_agent_lists_names_only_and_the_main_thread_keeps_stubs(self):
        sub = startup.parse_listing(self.send(body("explore it"))["messages"][1]["content"][0]["text"])[2]
        self.assertEqual([n for n, _ in sub], list(SKILLS))
        self.assertTrue(all(d == "" for _, d in sub))
        main = dict(startup.parse_listing(self.send(body("explore it"), sub=False)["messages"][1]["content"][0]["text"])[2])
        self.assertTrue(main["slides"].endswith("…"))

    def test_config_restores_stubs_for_sub_agents(self):
        self.rig.cfg["startup"]["sub_skill_chars"] = 60
        sub = dict(startup.parse_listing(self.send(body("explore it"))["messages"][1]["content"][0]["text"])[2])
        self.assertTrue(sub["slides"].endswith("…"))

    def test_same_type_different_tasks_share_tools_system_and_listing(self):
        a = self.send(body("find the parser"), **{"x-claude-code-agent-id": "a1"})
        b = self.send(body("list the tests"), **{"x-claude-code-agent-id": "a2"})
        self.assertEqual(json.dumps([a["tools"], a["system"]]), json.dumps([b["tools"], b["system"]]))
        self.assertEqual(a["messages"][1], b["messages"][1])


class ToolTest(Case):
    TOOLS = ["Agent", "Bash", "PowerShell", "ToolSearch"]

    def test_a_sub_agent_defers_the_agent_tool_but_the_main_thread_does_not(self):
        self.assertEqual(self.deferred(self.send(body(tools=self.TOOLS))), ["Agent", "PowerShell"])
        self.assertEqual(self.deferred(self.send(body(tools=self.TOOLS), sub=False)), ["PowerShell"])

    def test_hot_keeps_it_loaded(self):
        self.rig.cfg["tools"]["hot"] = ["Agent"]
        self.assertEqual(self.deferred(self.send(body(tools=self.TOOLS))), ["PowerShell"])

    def test_a_big_mcp_server_collapses_to_one_line_for_a_sub_agent_only(self):
        sub = self.send(with_names(body(tools=self.TOOLS), NAMES))["messages"][1]["content"][0]["text"]
        self.assertIn("mcp__big__* (9 tools", sub)
        self.assertNotIn("mcp__big__tool3", sub)
        self.assertIn("mcp__small__a\nmcp__small__b", sub)  # under the threshold: untouched
        self.assertIn("WebFetch", sub)
        main = self.send(with_names(body(tools=self.TOOLS), NAMES), sub=False)["messages"][1]["content"][0]["text"]
        self.assertIn("mcp__big__tool3", main)

    def test_collapsing_is_repeatable(self):
        once = self.send(with_names(body(tools=self.TOOLS), NAMES))
        again = self.send(with_names(body(tools=self.TOOLS), NAMES))
        self.assertEqual(once["messages"][1], again["messages"][1])


def marked(ttl=None):
    b = body()
    mark = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    b["system"][0]["cache_control"] = dict(mark)
    b["tools"][-1]["cache_control"] = dict(mark)
    b["messages"][0]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return b


class TtlTest(Case):
    def test_sub_agent_prefix_marks_get_an_hour_and_message_marks_keep_theirs(self):
        sent = self.send(marked())
        self.assertEqual(sent["system"][0]["cache_control"]["ttl"], "1h")
        self.assertEqual(sent["tools"][-1]["cache_control"]["ttl"], "1h")
        self.assertNotIn("ttl", sent["messages"][0]["content"][-1]["cache_control"])
        self.assertEqual(self.rig.records()[-1]["sub_ttl"], 2)

    def test_main_thread_is_untouched(self):
        self.assertNotIn("ttl", self.send(marked(), sub=False)["system"][0]["cache_control"])

    def test_empty_setting_turns_it_off(self):
        self.rig.cfg["startup"]["sub_ttl"] = ""
        self.assertNotIn("ttl", self.send(marked())["system"][0]["cache_control"])

    def test_an_hour_mark_already_there_is_not_counted(self):
        self.send(marked("1h"))
        self.assertNotIn("sub_ttl", self.rig.records()[-1])


if __name__ == "__main__":
    unittest.main()
