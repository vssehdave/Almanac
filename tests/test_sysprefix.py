import copy
import json
import unittest
from types import SimpleNamespace

from helpers import config

from getalmanac import report
from getalmanac.sysprefix import SysPrefixLayer

MCP = "# MCP Server Instructions\n\nThe following MCP servers have provided instructions:\n\n## docs\nUse docs well.\n\n"
ADVISOR = "# Advisor Tool\n\nYou have an advisor.\n\n"
DEFER = "The following deferred tools are now available via ToolSearch:\nWebFetch\nWebSearch\n\n"
AGENTS = "Available agent types for the Agent tool:\n- claude: catch-all\n- Plan: planner\n\n"
SKILLS = "The following skills are available for use with the Skill tool:\n\n- pdf: pdf things\n- slides: decks\n"
UNKNOWN = "# Some Future Section\n\nNobody wrote a marker for this.\n\n"
HOOK = "SessionStart:startup hook success: one\n\n"
ALL = (MCP, ADVISOR, DEFER, AGENTS, SKILLS)


def request(typed="fix the bug", hook=HOOK, parts=ALL, cc=True, extra=()):
    last = {"type": "text", "text": "S" * 9600}
    if cc:
        last["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    tail = [{"type": "tool_addition", "name": f"t{i}"} for i in range(3)]
    tail.append({"type": "tool_addition", "name": "t3", "cache_control": {"type": "ephemeral"}})
    return {"model": "claude-test",
            "system": [{"type": "text", "text": "id" * 30},
                       {"type": "text", "text": "rules" * 12, "cache_control": {"type": "ephemeral", "ttl": "1h"}}, last],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>\nCodebase and user instructions are shown below.\n</system-reminder>"},
                {"type": "text", "text": "<system-reminder>\nAs you answer the questions, you can use the following context\n</system-reminder>"},
                {"type": "text", "text": typed}]},
                {"role": "system", "content": [{"type": "text", "text": hook + "".join(parts)}, *tail]}, *extra]}


def run(body, on=True, stats=None, **over):
    over = {"klass": "", "agent": "", **over}
    cfg = config.load()
    cfg["startup"]["system_prefix"] = on
    ctx = SimpleNamespace(cfg=cfg, kind="messages", agent_type="", stats=stats or {}, **over)
    out = copy.deepcopy(body)
    SysPrefixLayer().transform(out, ctx)
    return out, ctx


def dump(x):
    return json.dumps(x, sort_keys=True)


class SysPrefixTest(unittest.TestCase):
    def test_off_is_byte_identical(self):
        body = request()
        self.assertEqual(dump(run(body, on=False)[0]), dump(body))

    def test_moves_sections_in_fixed_order_to_a_new_last_block(self):
        out, ctx = run(request())
        self.assertEqual(len(out["system"]), 4)
        self.assertEqual(out["system"][3]["text"], "\n\n".join(s.rstrip("\n") for s in ALL))
        self.assertEqual(out["messages"][1]["content"][0]["text"], HOOK)
        self.assertEqual(ctx.stats["sysprefix"]["moved"],
                         ["MCP instructions", "advisor notes", "deferred tool names", "agent types", "skill listing"])

    def test_one_conversation_requests_are_identical(self):
        a, _ = run(request())
        b, _ = run(request(extra=[{"role": "assistant", "content": "x"}, {"role": "user", "content": "more"}]))
        self.assertEqual(dump(a["system"]), dump(b["system"]))
        self.assertEqual(dump(a["messages"][:2]), dump(b["messages"][:2]))

    def test_fresh_layer_same_bytes(self):
        body = request()
        self.assertEqual(dump(run(body)[0]), dump(run(body)[0]))

    def test_two_conversations_share_the_system_block(self):
        a, _ = run(request("fix the bug", hook="SessionStart:startup hook success: aaa\n\n"))
        b, _ = run(request("write a poem", hook="SessionStart:startup hook success: a much longer, different hook text\n\n"))
        self.assertEqual(dump(a["system"]), dump(b["system"]))

    def test_cache_control_moves_with_its_ttl(self):
        out, _ = run(request())
        self.assertNotIn("cache_control", out["system"][2])
        self.assertEqual(out["system"][3]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        count = sum("cache_control" in b for b in out["system"]) + sum("cache_control" in b for m in out["messages"] for b in m["content"])
        self.assertEqual(count, 3)

    def test_no_cache_control_is_added(self):
        out, _ = run(request(cc=False))
        self.assertEqual(len(out["system"]), 4)
        self.assertNotIn("cache_control", out["system"][3])
        self.assertNotIn("cache_control", out["system"][2])

    def test_unknown_section_stays_in_place(self):
        out, _ = run(request(parts=(MCP, UNKNOWN, ADVISOR, SKILLS)))
        left = out["messages"][1]["content"][0]["text"]
        self.assertEqual(left, HOOK + UNKNOWN.rstrip("\n") + "\n\n")
        self.assertNotIn("Nobody wrote", out["system"][3]["text"])
        self.assertIn("Advisor Tool", out["system"][3]["text"])

    def test_nothing_recognised_is_untouched(self):
        body = request(parts=())
        out, ctx = run(body)
        self.assertEqual(dump(out), dump(body))
        self.assertNotIn("sysprefix", ctx.stats)

    def test_empty_leftover_drops_the_block(self):
        out, _ = run(request(hook="", parts=(MCP, SKILLS)))
        self.assertEqual([b["type"] for b in out["messages"][1]["content"]], ["tool_addition"] * 4)
        self.assertEqual(len(out["system"]), 4)

    def test_message_left_empty_is_not_moved(self):
        body = request(hook="", parts=(MCP, SKILLS))
        body["messages"][1]["content"] = body["messages"][1]["content"][:1]
        self.assertEqual(dump(run(body)[0]), dump(body))

    def test_skill_shown_in_full_stays(self):
        out, ctx = run(request(), stats={"skills": {"first_full": 1, "full": 1}, "agents": {"first_full": 0, "full": 0}})
        self.assertIn("pdf things", out["messages"][1]["content"][0]["text"])
        self.assertNotIn("skill listing", ctx.stats["sysprefix"]["moved"])
        self.assertIn("agent types", ctx.stats["sysprefix"]["moved"])

    def test_later_turn_expansion_does_not_change_the_cut(self):
        a, _ = run(request(), stats={"skills": {"first_full": 0, "full": 0}})
        b, _ = run(request(), stats={"skills": {"first_full": 0, "full": 2}})
        self.assertEqual(dump(a), dump(b))

    def test_sub_agent_untouched(self):
        body = request()
        self.assertEqual(dump(run(body, klass="subagent", agent="a1")[0]), dump(body))

    def test_report_sections_unchanged(self):
        text = HOOK + "".join(ALL)
        self.assertEqual(report._text_parts(text), [("hook output", len(HOOK)), ("MCP instructions", len(MCP)), ("advisor notes", len(ADVISOR)),
                                                    ("deferred tool names", len(DEFER)), ("agent types", len(AGENTS)), ("skill listing", len(SKILLS))])


if __name__ == "__main__":
    unittest.main()
