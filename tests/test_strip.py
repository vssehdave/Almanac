import copy
import json
import unittest
from unittest import mock

from helpers import Rig

from getalmanac import config, startup
from getalmanac.strip import StripLayer

CFG = copy.deepcopy(config.DEFAULTS)
MEM = "mcp__plugin_claude-mem_mcp-search__"


def request():
    skills = {"logbook": "Read and write the shared memory.", "claude-mem:build": "Build it.", "pocketbook:note": "Notes.",
              "council": "Convene a council.", "keeper:core": "Lazy."}
    agents = {"editor": "Edits. (Tools: Read)", "claude-mem:helper": "Helps. (Tools: *)"}
    hook = ("SessionStart:startup hook success: KEPT TEXT\n\nSessionStart hook additional context: # claude-mem status\n\n"
            "No memory yet.\n\nLive activity: http://localhost:1\n\n# Environment\n - cwd: /r\n\n"
            "The following deferred tools are now available via ToolSearch. Use it:\nWebFetch\n" + MEM + "search\n" + MEM + "timeline\nmcp__other__x\n\n"
            + startup.SKILL_MARK + "\n\n" + "\n".join(f"- {n}: {d}" for n, d in skills.items()) + "\n\n"
            + startup.AGENT_MARK + "\n" + "\n".join(f"- {n}: {d}" for n, d in agents.items()) + "\n\nToday is Monday.")
    prompt = ("UserPromptSubmit hook additional context: <logbook-notes>\nnote one\n</logbook-notes>\n\nfix the bug")
    return {"model": "m",
            "system": [{"type": "text", "text": "You are Claude.\n\n# MCP Server Instructions\n\n## plugin:claude-mem:mcp-search\n# Work state\nuse it\n\n## other\nkeep me\n"}],
            "tools": [{"name": "Bash", "input_schema": {}}, {"name": MEM + "search", "input_schema": {}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": hook},
                                                       {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {"name": MEM + "smart_search"}}},
                                                       {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {"name": "mcp__other__x"}}},
                                                       {"type": "text", "text": prompt}]}]}


class StripTest(unittest.TestCase):
    def setUp(self):
        self.body = request()
        ctx = mock.Mock(cfg=CFG)
        StripLayer().transform(self.body, ctx)
        self.text = self.body["messages"][0]["content"][0]["text"]

    def test_off_skills_and_agents_leave_the_listings(self):
        listing = startup.parse_listing(self.text, startup.SKILL_MARK)[2]
        self.assertEqual([n for n, _ in listing], ["council", "keeper:core"])
        self.assertEqual([n for n, _ in startup.parse_listing(self.text, startup.AGENT_MARK)[2]], ["editor"])

    def test_off_tools_leave_the_tool_list_the_deferred_list_and_the_additions(self):
        self.assertEqual([t["name"] for t in self.body["tools"]], ["Bash"])
        self.assertNotIn("mcp__plugin_claude-mem", json.dumps(self.body))
        self.assertIn("mcp__other__x", self.text)
        self.assertEqual([b.get("tool", {}).get("definition", {}).get("name") for b in self.body["messages"][0]["content"][1:2]],
                         ["mcp__other__x"])

    def test_hook_text_of_an_off_plugin_goes_and_the_text_around_it_stays(self):
        self.assertNotIn("claude-mem", self.text)
        self.assertIn("KEPT TEXT", self.text)
        self.assertIn("# Environment\n - cwd: /r", self.text)
        self.assertNotIn("logbook", json.dumps(self.body["messages"][0]["content"][-1]))
        self.assertIn("fix the bug", self.body["messages"][0]["content"][-1]["text"])

    def test_mcp_instructions_of_an_off_plugin_go(self):
        system = self.body["system"][0]["text"]
        self.assertNotIn("claude-mem", system)
        self.assertIn("## other\nkeep me", system)

    def test_a_clean_request_is_left_alone_and_the_result_is_stable(self):
        clean = {"messages": [{"role": "user", "content": "hello"}], "tools": [{"name": "Bash"}]}
        before = json.dumps(clean)
        StripLayer().transform(clean, mock.Mock(cfg=CFG))
        self.assertEqual(json.dumps(clean), before)
        again = request()
        StripLayer().transform(again, mock.Mock(cfg=CFG))
        self.assertEqual(again, self.body)

    def test_switched_off_in_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["enabled"] = False
        body = request()
        StripLayer().transform(body, mock.Mock(cfg=cfg))
        self.assertEqual(body, request())


class BannerTest(unittest.TestCase):
    """A plugin that stays installed but whose rules Almanac now supplies: its mode banner is cut, its skills stay listed."""

    def test_a_mode_banner_and_its_rules_are_cut_from_every_kind_of_hook_text(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["off_hook_text"] = ["ZED MODE"]
        for header in ("SessionStart:startup hook success: ", "SubagentStart hook additional context: ",
                       "UserPromptSubmit hook success: "):
            text = ("Intro.\n\n" + header + "ZED MODE ACTIVE \u2014 level: full\n\n# Zed\n\nrules rules\n\nmore rules\n"
                    "SessionStart hook additional context: kept note\n\n# Environment\n - cwd: /r")
            body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
            StripLayer().transform(body, mock.Mock(cfg=cfg))
            out = body["messages"][0]["content"][0]["text"]
            self.assertNotIn("rules rules", out)
            self.assertNotIn("MODE ACTIVE", out)
            self.assertIn("Intro.", out)
            self.assertIn("kept note", out)
            self.assertIn("# Environment\n - cwd: /r", out)

    def test_the_owners_own_hooks_are_never_cut_by_a_generic_mode_word(self):
        owner = "SessionStart:startup hook success: DEBUG MODE ACTIVE, MODE CHANGED, MODE OFF\n\nowner rules\n\n# Environment\n - cwd: /r"
        for markers in (None, ["ZED MODE"]):  # the default settings, and a marker for another plugin
            cfg = copy.deepcopy(CFG)
            if markers:
                cfg["profile"]["off_hook_text"] = markers
            body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": owner}]}]}
            StripLayer().transform(body, mock.Mock(cfg=cfg))
            self.assertEqual(body["messages"][0]["content"][0]["text"], owner)

    def test_a_marker_must_open_the_hook_output(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["off_hook_text"] = ["ZED MODE"]
        text = "SessionStart:startup hook success: our notes\n\nthe ZED MODE banner is cut elsewhere\n\n# Environment\n - cwd: /r"
        body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
        StripLayer().transform(body, mock.Mock(cfg=cfg))
        self.assertEqual(body["messages"][0]["content"][0]["text"], text)

    def test_a_cut_banner_stops_at_the_next_hook_and_leaves_the_owners_hook_and_the_prompt(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["off_hook_text"] = ["ZED MODE"]
        text = ("UserPromptSubmit hook success: ZED MODE CHANGED level full\n\n# Zed\n\nrules rules\n\n"
                "UserPromptSubmit hook success: DEBUG MODE ACTIVE owner hook\n\n<system-reminder>kept</system-reminder>")
        body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}, {"type": "text", "text": "fix the bug"}]}]}
        StripLayer().transform(body, mock.Mock(cfg=cfg))
        out = body["messages"][0]["content"]
        self.assertNotIn("rules rules", out[0]["text"])
        self.assertIn("DEBUG MODE ACTIVE owner hook", out[0]["text"])
        self.assertIn("<system-reminder>kept</system-reminder>", out[0]["text"])
        self.assertEqual(out[1]["text"], "fix the bug")

    def test_other_hook_text_and_the_skill_listing_stay(self):
        text = ("SessionStart:startup hook success: hello from a hook\n\n" + startup.SKILL_MARK + "\n\n- zed:core: Does things.\n\nEnd.")
        body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
        StripLayer().transform(body, mock.Mock(cfg=CFG))
        self.assertEqual(body["messages"][0]["content"][0]["text"], text)

    def test_the_markers_are_settings(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["off_hook_text"] = ["HELLO FROM"]
        text = "SessionStart:startup hook success: hello from a hook\n\n# Environment\n - cwd: /r"
        body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
        StripLayer().transform(body, mock.Mock(cfg=cfg))
        self.assertEqual(body["messages"][0]["content"][0]["text"].strip(), "# Environment\n - cwd: /r")


class ThroughTheProxyTest(unittest.TestCase):
    def test_the_proxy_sends_the_clean_request(self):
        rig = Rig(layers=[StripLayer()], overrides={"profile.enabled": True})
        self.addCleanup(rig.close)
        rig.messages(request())
        self.assertNotIn("claude-mem", json.dumps(rig.sent()))
        self.assertIn("council", json.dumps(rig.sent()))


if __name__ == "__main__":
    unittest.main()
