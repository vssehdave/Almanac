import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "src", "getalmanac", "bundle", "guard", "scripts")


def run_raw(script, text):
    done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], input=text, capture_output=True, text=True)
    return done.returncode, done.stderr


def tool_use(tool_id, name, tool_input):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}})


def tool_result(tool_use_id):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id}]}})


class PlanGuard(unittest.TestCase):
    def run_guard(self, lines, with_path=True):
        with tempfile.TemporaryDirectory() as folder:
            payload = {"tool_name": "ExitPlanMode", "tool_input": {}}
            if with_path:
                path = os.path.join(folder, "transcript.jsonl")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                payload["transcript_path"] = path
            return run_raw("plan_guard.py", json.dumps(payload))

    def blocked(self, lines):
        code, err = self.run_guard(lines)
        self.assertEqual(code, 2, lines)
        self.assertIn("EGM", err)

    def allowed(self, lines):
        self.assertEqual(self.run_guard(lines)[0], 0, lines)

    def test_no_egm_call_blocks(self):
        self.blocked([tool_use("t0", "Bash", {"command": "ls"}), tool_result("t0")])

    def test_egm_skill_allows(self):
        self.allowed([tool_use("t1", "Skill", {"skill": "egm:egm-review"}), tool_result("t1")])

    def test_egm_agent_allows(self):
        self.allowed([tool_use("t1", "Agent", {"subagent_type": "egm:egm-design"}), tool_result("t1")])

    def test_egm_call_before_completed_exit_plan_mode_blocks(self):
        self.blocked([
            tool_use("t1", "Skill", {"skill": "egm:egm-review"}), tool_result("t1"),
            tool_use("t2", "ExitPlanMode", {}), tool_result("t2"),
        ])

    def test_pending_exit_plan_mode_does_not_reset(self):
        self.allowed([
            tool_use("t1", "Skill", {"skill": "egm:egm-review"}), tool_result("t1"),
            tool_use("t2", "ExitPlanMode", {}),
        ])

    def test_other_plugin_skill_blocks(self):
        self.blocked([tool_use("t1", "Skill", {"skill": "almanac:lean-review"}), tool_result("t1")])

    def test_missing_transcript_path_allows(self):
        self.assertEqual(self.run_guard([], with_path=False)[0], 0)

    def test_non_json_line_is_skipped(self):
        self.allowed(["not json", tool_use("t1", "Skill", {"skill": "egm:egm-review"}), tool_result("t1")])


if __name__ == "__main__":
    unittest.main()
