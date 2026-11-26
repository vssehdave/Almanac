import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "egm" / "scripts" / "plan_mode.py"
HOOKS = SCRIPT.parent.parent / "hooks" / "hooks.json"


def run(stdin, data):
    env = dict(os.environ, CLAUDE_PLUGIN_DATA=data)
    result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True, env=env)
    return result


def prompt(mode):
    return json.dumps({"hook_event_name": "UserPromptSubmit", "permission_mode": mode})


def exit_plan(session_id):
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "session_id": session_id})


def commands(entries):
    return [hook["command"] for entry in entries for hook in entry["hooks"]]


class PlanModeHook(unittest.TestCase):
    def test_plan_mode_prompt_adds_review_brainstorm_draft_context(self):
        with tempfile.TemporaryDirectory() as data:
            result = run(prompt("plan"), data)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "UserPromptSubmit")
            for name in ("egm-review", "egm-brainstorm", "egm-draft"):
                self.assertIn(name, out["additionalContext"])

    def test_other_modes_print_nothing(self):
        with tempfile.TemporaryDirectory() as data:
            for mode in ("default", "acceptEdits"):
                result = run(prompt(mode), data)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_exit_plan_mode_adds_handoff_context(self):
        with tempfile.TemporaryDirectory() as data:
            result = run(exit_plan("abc"), data)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "PostToolUse")
            context = out["additionalContext"]
            for text in ("egm-handoff", "almanac:planner", "memory_pin_add", os.path.join(data, "egm-plans", "abc.md")):
                self.assertIn(text, context)

    def test_unsafe_session_id_prints_nothing(self):
        with tempfile.TemporaryDirectory() as data:
            result = run(exit_plan("../x"), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_other_tool_prints_nothing(self):
        with tempfile.TemporaryDirectory() as data:
            result = run(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "abc"}), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_bad_json_prints_nothing(self):
        with tempfile.TemporaryDirectory() as data:
            result = run("not json", data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_hooks_json_wires_prompt_and_exit_plan_mode(self):
        hooks = json.loads(HOOKS.read_text())["hooks"]
        self.assertNotIn("PreToolUse", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PostToolUse", hooks)
        post = [entry for entry in hooks["PostToolUse"] if entry.get("matcher") == "ExitPlanMode"]
        self.assertTrue(post)
        for cmd in commands(hooks["UserPromptSubmit"]) + commands(post):
            self.assertIn("scripts/plan_mode.py", cmd)


if __name__ == "__main__":
    unittest.main()
