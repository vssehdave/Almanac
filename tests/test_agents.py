import json
import unittest

from helpers import Rig, SESSION
from getalmanac import report


def rec(ts, agent="", lane="m", model="claude-sonnet-5", usage=None, **more):
    return {"ts": ts, "kind": "messages", "session": SESSION, "agent": agent, "lane": lane, "model": model,
            "usage": usage or {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 70, "output_tokens": 5},
            **more}


def sub(ts, lane, kind, model="claude-haiku-5", **more):
    return rec(ts, agent="a-" + lane, lane=lane, model=model, agent_type=kind, **more)


class AgentsReport(unittest.TestCase):
    def test_groups_by_lane_and_totals(self):
        recs = [rec(1), sub(2, "x", "Explore", task="find the retry helper"), sub(3, "x", "Explore"), sub(3, "y", "general", model="claude-sonnet-5"),
                rec(5), {"ts": 6, "kind": "messages", "session": "other", "agent": "z", "lane": "z", "agent_type": "old"}]
        out = report.agents(recs)
        self.assertEqual([t["agent"] for t in out["threads"]], ["Explore", "general"])
        first = out["threads"][0]
        self.assertEqual((first["requests"], first["input"], first["output"], first["model"]), (2, 200, 10, "claude-haiku-5"))
        self.assertEqual(first["task"], "find the retry helper")
        self.assertEqual(out["peak"], 2)
        text = report.render_agents(recs)
        self.assertIn("Agents in the latest session:", text)
        self.assertIn("find the retry helper", text)
        self.assertNotIn("Warning", text)

    def test_no_subagents_prints_nothing(self):
        self.assertEqual(report.render_agents([rec(1), rec(2)]), "")

    def test_more_than_four_at_once_warns(self):
        busy = [sub(t, f"l{i}", "general", model="claude-sonnet-5") for i in range(5) for t in (10 + i, 20 + i)]
        self.assertEqual(report.agents([rec(1)] + busy)["peak"], 5)
        self.assertIn("5 sub-agent threads were active at the same time", report.render_agents([rec(1)] + busy))
        one_after_another = [sub(t, f"l{i}", "general") for i in range(5) for t in (10 * i, 10 * i + 1)]
        self.assertEqual(report.agents([rec(1)] + one_after_another)["peak"], 1)
        self.assertNotIn("Warning", report.render_agents([rec(1)] + one_after_another))

    def test_opus_building_warns(self):
        recs = [rec(1), sub(2, "a", "general", model="claude-opus-4", task="Implement the retry helper"),
                sub(3, "b", "code-reviewer", model="claude-opus-4", task="Implement a review of the helper"),
                sub(4, "c", "general", model="claude-opus-4", task="Look up the retry helper"),
                sub(5, "d", "general", model="claude-sonnet-5", task="Build the thing")]
        self.assertEqual(report.agents(recs)["building"], ["general"])
        self.assertIn("Warning: Opus used for building: general", report.render_agents(recs))

    def test_report_prints_the_section(self):
        start = rec(1, usage={"input_tokens": 100}, sections={"system prompt": 400})
        text = report.render([start, sub(2, "x", "Explore", task="find it")])
        self.assertIn("Agents in the latest session:", text)
        self.assertIn("Explore", text)


class AgentsLogged(unittest.TestCase):
    def test_subagent_record_has_type_and_first_task_only(self):
        rig = Rig()
        self.addCleanup(rig.close)
        extra = {"x-claude-code-agent-id": "agent-7", "x-claude-code-agent-type": "Explore"}
        body = {"model": "claude-haiku-5", "stream": True, "messages": [{"role": "user", "content": "Find the retry helper. " + "x" * 200}]}
        rig.messages(body, extra=extra)
        body["messages"] += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "Find the retry helper. " + "x" * 200}]
        rig.messages(body, extra=extra)
        rig.messages({"model": "m", "stream": True, "messages": [{"role": "user", "content": "main thread task"}]})
        first, second, main = rig.records()
        self.assertEqual(first["agent_type"], "Explore")
        self.assertTrue(first["task"].startswith("Find the retry helper."))
        self.assertEqual(len(first["task"]), 80)
        self.assertNotIn("task", second)
        self.assertNotIn("task", main)
        self.assertNotIn("agent_type", main)


if __name__ == "__main__":
    unittest.main()
