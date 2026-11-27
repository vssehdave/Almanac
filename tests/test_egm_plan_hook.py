import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "egm" / "scripts" / "plan_mode.py"
HOOKS = SCRIPT.parent.parent / "hooks" / "hooks.json"

PROMPT = {"type": "user", "message": {"content": "plan this"}}
EGM = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "egm:egm-review"}}]}}
AGENT = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {"subagent_type": "egm:egm-design"}}]}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}


def run(stdin, data):
    env = dict(os.environ, CLAUDE_PLUGIN_DATA=data)
    result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True, env=env)
    return result


def prompt(mode):
    return json.dumps({"hook_event_name": "UserPromptSubmit", "permission_mode": mode})


def exit_plan(session_id):
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "session_id": session_id})


def transcript(folder, lines):
    path = os.path.join(folder, "transcript.jsonl")
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def stop(path, mode="plan", active=None):
    data = {"hook_event_name": "Stop", "permission_mode": mode, "transcript_path": path}
    if active is not None:
        data["stop_hook_active"] = active
    return json.dumps(data)


def pre_exit_plan(path):
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode", "transcript_path": path})


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

    def test_stop_blocks_when_no_egm_call_after_last_prompt(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT])
            result = run(stop(path), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_stop_silent_when_egm_call_after_last_prompt(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT, EGM])
            result = run(stop(path), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_stop_blocks_when_egm_call_came_before_last_prompt(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [EGM, PROMPT])
            result = run(stop(path), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_stop_tool_result_is_not_a_prompt(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT, EGM, RESULT])
            result = run(stop(path), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_stop_silent_when_already_active_or_not_plan_mode(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT])
            for payload in (stop(path, active=True), stop(path, mode="default")):
                result = run(payload, data)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_stop_missing_transcript_prints_nothing(self):
        with tempfile.TemporaryDirectory() as data:
            result = run(stop(os.path.join(data, "missing.jsonl")), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_pre_tool_use_exit_plan_mode_denies_without_egm_call(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT])
            result = run(pre_exit_plan(path), data)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "PreToolUse")
            self.assertEqual(out["permissionDecision"], "deny")

    def test_pre_tool_use_exit_plan_mode_allows_with_egm_agent(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT, AGENT])
            result = run(pre_exit_plan(path), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_hooks_json_wires_stop_pre_and_post_tool_use(self):
        hooks = json.loads(HOOKS.read_text())["hooks"]
        self.assertIn("Stop", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PostToolUse", hooks)
        pre = [entry for entry in hooks.get("PreToolUse", []) if entry.get("matcher") == "ExitPlanMode"]
        self.assertTrue(pre)
        post = [entry for entry in hooks["PostToolUse"] if entry.get("matcher") == "ExitPlanMode"]
        self.assertTrue(post)
        for cmd in commands(hooks["Stop"]) + commands(pre) + commands(hooks["UserPromptSubmit"]) + commands(post):
            self.assertIn("scripts/plan_mode.py", cmd)


if __name__ == "__main__":
    unittest.main()
