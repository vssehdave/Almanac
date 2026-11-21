import copy
import unittest
from types import SimpleNamespace

from getalmanac import config
from getalmanac.startup import AGENT_MARK, ForceAgentsLayer

LISTING = (AGENT_MARK + "\n- claude: Catch-all. (Tools: *)\n- editor: Mechanical edits. (Tools: Read, Edit)\n"
           "- Explore: Read-only search. (Tools: Read)\n- general-purpose: General agent. (Tools: *)\n")


def body(listing=LISTING, desc='Launch an agent. subagent_type: "fork" forks yourself.'):
    tool = {"name": "Agent", "description": desc, "input_schema": {"type": "object", "required": ["prompt"],
            "properties": {"prompt": {"type": "string"}, "subagent_type": {"type": "string"}}}}
    return {"tools": [{"name": "Read", "input_schema": {}}, tool],
            "messages": [{"role": "user", "content": [{"type": "text", "text": listing}, {"type": "text", "text": "hi"}]}]}


def ctx(**startup):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["startup"].update(startup)
    return SimpleNamespace(cfg=cfg, kind="messages", stats={}, state=None)


class ForceAgents(unittest.TestCase):
    def run_layer(self, b, c=None):
        c = c or ctx()
        ForceAgentsLayer().transform(b, c)
        return b["tools"][1], c

    def test_only_specialists_and_required(self):
        tool, c = self.run_layer(body())
        self.assertEqual(tool["input_schema"]["properties"]["subagent_type"]["enum"], ["editor", "Explore", "fork"])
        self.assertEqual(tool["input_schema"]["required"], ["prompt", "subagent_type"])
        self.assertIn("must be one of the listed specialist", tool["description"])
        self.assertEqual(c.stats["force_agents"], ["editor", "Explore", "fork"])

    def test_same_bytes_every_request(self):
        a, b = body(), body()
        self.run_layer(a); self.run_layer(a); self.run_layer(b)
        self.assertEqual(a, b)

    def test_no_specialists_leaves_tool_alone(self):
        b = body(AGENT_MARK + "\n- general-purpose: G. (Tools: *)\n")
        before = copy.deepcopy(b)
        self.run_layer(b)
        self.assertEqual(b, before)

    def test_off_and_no_listing(self):
        for b, c in ((body(), ctx(force_agents=False)), (body("no listing here"), ctx())):
            before = copy.deepcopy(b)
            self.run_layer(b, c)
            self.assertEqual(b, before)

    def test_a_thread_whose_first_request_has_no_listing_is_never_changed(self):
        state = {}
        c = ctx(); c.state = SimpleNamespace(frozen=lambda k, f: state.setdefault(k, f())); c.lane, c.frozen_lane = "s1", ""
        first = body("no listing yet")
        ForceAgentsLayer().transform(first, c)
        later = body()
        before = copy.deepcopy(later)
        ForceAgentsLayer().transform(later, c)
        self.assertEqual(later, before)  # the tools stay as the first request sent them, so the cache holds

    def test_no_fork_without_mention(self):
        tool, _ = self.run_layer(body(desc="Launch an agent."))
        self.assertEqual(tool["input_schema"]["properties"]["subagent_type"]["enum"], ["editor", "Explore"])


class AgentEffort(unittest.TestCase):
    def test_reads_front_matter_and_finds_custom_agents_by_body(self):
        import tempfile, os
        from pathlib import Path
        from unittest import mock
        from getalmanac import routing
        with tempfile.TemporaryDirectory() as d:
            agents = Path(d, "agents"); agents.mkdir()
            (agents / "a.md").write_text("---\nname: a\neffort: high\n---\nYou are agent A. Review every change for bugs and report them.\neffort: low\n", encoding="utf-8")
            (agents / "b.md").write_text("---\nname: b\neffort: huge\n---\n", encoding="utf-8")
            routing._agent_efforts.clear()
            try:
                with mock.patch("getalmanac.profile.user_dir", return_value=Path(d)), mock.patch.dict(os.environ, {"ALMANAC_HOME": d}):
                    self.assertEqual([routing.agent_effort(n) for n in ("a", "b", "c", "../a", None)], ["high", "", "", "", ""])
                    # Claude Code sends `custom` for a user agent: it is found by its body in the system prompt
                    self.assertEqual(routing.agent_effort("custom", "intro\nYou are agent A. Review every change\n for bugs and report them.\neffort: low"), "high")
                    self.assertEqual(routing.agent_effort("custom", "something else entirely"), "")
            finally:
                routing._agent_efforts.clear()


if __name__ == "__main__":
    unittest.main()
