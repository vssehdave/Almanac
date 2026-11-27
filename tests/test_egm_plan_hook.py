import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "egm" / "scripts" / "plan_mode.py"
HOOKS = SCRIPT.parent.parent / "hooks" / "hooks.json"

PROMPT = {"type": "user", "message": {"content": "plan this"}}
EGM = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "egm:egm-review"}}]}}
BRAINSTORM = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "egm:egm-brainstorm"}}]}}
AGENT = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {"subagent_type": "egm:egm-design"}}]}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}
NUDGE = ("Plan mode EGM (egm-review): layers run in order zoom-out, implementation-readiness, design, codebase-details. "
         "One layer per owner message, never two at once, never pass a model. "
         "Show findings in chat with a proposed answer to each, and rewrite the plan file. "
         "Use egm-brainstorm or egm-draft first only if the idea has no shape yet.")
SID = "6f1c2a3e-4b5d-4e6f-8a9b-0c1d2e3f4a5b"
SID2 = "7a2d3b4f-5c6e-4f70-9b0c-1d2e3f4a5b6c"


def run(stdin, data):
    env = dict(os.environ, CLAUDE_PLUGIN_DATA=data)
    result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True, env=env)
    return result


def prompt(mode, session_id=None, text=None):
    data = {"hook_event_name": "UserPromptSubmit", "permission_mode": mode}
    if session_id is not None:
        data["session_id"] = session_id
    if text is not None:
        data["prompt"] = text
    return json.dumps(data)


def exit_plan(session_id, mode=None):
    data = {"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "session_id": session_id}
    if mode is not None:
        data["permission_mode"] = mode
    return json.dumps(data)


def transcript(folder, lines):
    path = os.path.join(folder, "transcript.jsonl")
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def stop(path, mode="plan", active=None, session_id=None):
    data = {"hook_event_name": "Stop", "permission_mode": mode, "transcript_path": path}
    if active is not None:
        data["stop_hook_active"] = active
    if session_id is not None:
        data["session_id"] = session_id
    return json.dumps(data)


def pre_exit_plan(path, session_id=None, mode=None):
    data = {"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode", "transcript_path": path}
    if session_id is not None:
        data["session_id"] = session_id
    if mode is not None:
        data["permission_mode"] = mode
    return json.dumps(data)


def pre_agent(session, subagent_type, model=None, mode="plan"):
    tool_input = {"subagent_type": subagent_type, "prompt": "review the plan", "description": "EGM layer"}
    if model is not None:
        tool_input["model"] = model
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input,
                       "session_id": session, "permission_mode": mode})


def post_agent(session, subagent_type, status="completed", mode="plan"):
    tool_input = {"subagent_type": subagent_type, "prompt": "review the plan", "description": "EGM layer"}
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_input": tool_input,
                       "tool_response": {"status": status}, "session_id": session, "permission_mode": mode})


def chain(data, session):
    with open(os.path.join(data, "egm-plans", f"egm_chain-{session}.json"), encoding="utf-8") as f:
        return json.load(f)


def claims(data, session):
    return glob.glob(os.path.join(data, "egm-plans", f"egm_claim-{session}-*"))


def deny_reason(result):
    """The deny reason the hook printed, or None when it did not deny."""
    if not result.stdout:
        return None
    out = json.loads(result.stdout).get("hookSpecificOutput", {})
    return out.get("permissionDecisionReason") if out.get("permissionDecision") == "deny" else None


def commands(entries):
    return [hook["command"] for entry in entries for hook in entry["hooks"]]


class PlanModeHook(unittest.TestCase):
    def test_plan_mode_prompt_adds_review_brainstorm_draft_context(self):
        with tempfile.TemporaryDirectory() as data:
            result = run(prompt("plan"), data)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "UserPromptSubmit")
            self.assertEqual(out["additionalContext"], NUDGE)
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
            path = transcript(data, [PROMPT, BRAINSTORM])
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
            path = transcript(data, [PROMPT, BRAINSTORM, RESULT])
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


class PlanModeChain(unittest.TestCase):
    def assertDenied(self, result, needle):
        self.assertEqual(result.returncode, 0)
        reason = deny_reason(result)
        self.assertIsNotNone(reason, result.stdout)
        self.assertIn(needle, reason)

    def assertAllowed(self, result):
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(deny_reason(result), result.stdout)

    def test_model_override_denied(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertDenied(run(pre_agent(SID, "egm-zoom-out", model="opus"), data), "no model override")

    def test_empty_model_allowed(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            self.assertAllowed(run(pre_agent(SID, "egm-zoom-out", model=""), data))

    def test_second_pre_without_post_denied(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            self.assertAllowed(run(pre_agent(SID, "egm-zoom-out"), data))
            self.assertDenied(run(pre_agent(SID, "egm-zoom-out"), data), "one layer per owner message")

    def test_two_processes_started_together_allow_one(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            env = dict(os.environ, CLAUDE_PLUGIN_DATA=data)
            procs = [subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, env=env) for _ in range(2)]
            for proc in procs:
                proc.stdin.write(pre_agent(SID, "egm-zoom-out"))
                proc.stdin.close()
            outs = [proc.stdout.read() for proc in procs]
            for proc in procs:
                self.assertEqual(proc.wait(), 0)
            denied = [out for out in outs if out]
            self.assertEqual(len(denied), 1, outs)
            self.assertIn("one layer per owner message", denied[0])

    def test_design_before_readiness_denied(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            self.assertAllowed(run(pre_agent(SID, "egm-zoom-out"), data))
            run(post_agent(SID, "egm-zoom-out"), data)
            run(prompt("plan", SID), data)
            self.assertDenied(run(pre_agent(SID, "egm-design"), data), "next is egm-implementation-readiness")

    def test_rerun_last_layer_after_new_prompt_allowed(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            run(prompt("plan", SID), data)
            self.assertAllowed(run(pre_agent(SID, "egm-zoom-out"), data))

    def test_egm_prefix_and_bare_name_behave_the_same(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            bare = run(pre_agent(SID, "egm-design"), data)
            prefixed = run(pre_agent(SID, "egm:egm-design"), data)
            self.assertDenied(bare, "next is egm-zoom-out")
            self.assertDenied(prefixed, "next is egm-zoom-out")
            self.assertEqual(bare.stdout, prefixed.stdout)

    def test_egm_synthesizer_denied(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            self.assertDenied(run(pre_agent(SID, "egm-synthesizer"), data), "only the 4 layers")

    def test_non_egm_agent_passes(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            result = run(pre_agent(SID, "general-purpose"), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_nothing_enforced_outside_plan_mode(self):
        with tempfile.TemporaryDirectory() as data:
            for event in (pre_agent(SID, "egm-design", model="opus", mode="default"),
                          pre_agent(SID, "egm-synthesizer", mode="acceptEdits")):
                result = run(event, data)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_post_adds_no_duplicates(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            self.assertEqual(chain(data, SID)["layers_done"], ["egm-zoom-out"])

    def test_async_launched_counts(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out", status="async_launched"), data)
            self.assertEqual(chain(data, SID)["layers_done"], ["egm-zoom-out"])
            run(prompt("plan", SID), data)
            self.assertAllowed(run(pre_agent(SID, "egm-implementation-readiness"), data))

    def test_bad_response_adds_nothing_and_deletes_claim(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            self.assertEqual(len(claims(data, SID)), 1)
            run(post_agent(SID, "egm-zoom-out", status="failed"), data)
            self.assertEqual(chain(data, SID)["layers_done"], [])
            self.assertEqual(claims(data, SID), [])

    def test_done_and_skip_close_the_chain(self):
        for text in ("Done", " skip "):
            with tempfile.TemporaryDirectory() as data:
                result = run(prompt("plan", SID, text), data)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                result = run(prompt("plan", SID, "next"), data)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_task_notification_does_not_bump_turn(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID, "plan this"), data)
            result = run(prompt("plan", SID, "<task-notification>done</task-notification>"), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(chain(data, SID)["turn"], 1)

    def test_stop_blocks_on_open_empty_chain(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            path = transcript(data, [PROMPT])
            result = run(stop(path, session_id=SID), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_stop_passes_after_one_layer(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            path = transcript(data, [PROMPT])
            result = run(stop(path, session_id=SID), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_stop_passes_after_brainstorm_skill(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            path = transcript(data, [PROMPT, BRAINSTORM])
            result = run(stop(path, session_id=SID), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_bare_review_skill_does_not_satisfy_stop(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            path = transcript(data, [PROMPT, EGM])
            result = run(stop(path, session_id=SID), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_skip_then_exit_plan_mode_passes(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID, "skip"), data)
            path = transcript(data, [PROMPT])
            result = run(pre_exit_plan(path, SID, "plan"), data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_exit_plan_deletes_files_and_handoff_is_unchanged(self):
        with tempfile.TemporaryDirectory() as data:
            before = run(exit_plan(SID, "plan"), data)
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            after = run(exit_plan(SID, "plan"), data)
            self.assertNotEqual(before.stdout, "")
            self.assertEqual(after.stdout, before.stdout)
            self.assertFalse(os.path.exists(os.path.join(data, "egm-plans", f"egm_chain-{SID}.json")))
            self.assertEqual(claims(data, SID), [])

    def test_prompt_after_exit_opens_a_chain(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            run(exit_plan(SID, "plan"), data)
            result = run(prompt("plan", SID), data)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"], NUDGE)
            self.assertAllowed(run(pre_agent(SID, "egm-zoom-out"), data))

    def test_two_sessions_stay_separate(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            run(prompt("plan", SID), data)
            run(prompt("plan", SID2), data)
            self.assertDenied(run(pre_agent(SID2, "egm-implementation-readiness"), data), "next is egm-zoom-out")
            self.assertAllowed(run(pre_agent(SID, "egm-implementation-readiness"), data))

    def test_cleanup_removes_seven_day_old_files(self):
        with tempfile.TemporaryDirectory() as data:
            folder = os.path.join(data, "egm-plans")
            os.makedirs(folder)
            old = time.time() - 8 * 86400
            stale = [os.path.join(folder, f"egm_chain-{SID2}.json"), os.path.join(folder, f"egm_claim-{SID2}-1")]
            for path in stale:
                with open(path, "w") as f:
                    f.write("{}")
                os.utime(path, (old, old))
            fresh = os.path.join(folder, f"egm_claim-{SID2}-2")
            open(fresh, "w").close()
            run(prompt("plan", SID), data)
            for path in stale:
                self.assertFalse(os.path.exists(path), path)
            self.assertTrue(os.path.exists(fresh))

    def test_default_mode_prompt_then_plan_prompt_resets_state(self):
        with tempfile.TemporaryDirectory() as data:
            run(prompt("plan", SID), data)
            run(pre_agent(SID, "egm-zoom-out"), data)
            run(post_agent(SID, "egm-zoom-out"), data)
            run(prompt("default", SID), data)
            run(prompt("plan", SID), data)
            self.assertEqual(chain(data, SID)["layers_done"], [])
            self.assertDenied(run(pre_agent(SID, "egm-implementation-readiness"), data), "next is egm-zoom-out")

    def test_missing_state_file_falls_back_to_session_less(self):
        with tempfile.TemporaryDirectory() as data:
            path = transcript(data, [PROMPT])
            self.assertEqual(run(stop(path, session_id=SID), data).stdout, run(stop(path), data).stdout)

    def test_garbage_state_file_falls_back_to_session_less(self):
        with tempfile.TemporaryDirectory() as data:
            folder = os.path.join(data, "egm-plans")
            os.makedirs(folder)
            with open(os.path.join(folder, f"egm_chain-{SID}.json"), "w") as f:
                f.write("garbage")
            self.assertEqual(run(prompt("plan", SID), data).stdout, run(prompt("plan"), data).stdout)

    def test_unsafe_session_id_falls_back_to_session_less(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(run(prompt("plan", "../x"), data).stdout, run(prompt("plan"), data).stdout)

    def test_non_string_prompt_falls_back_to_session_less(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(run(prompt("plan", SID, 5), data).stdout, run(prompt("plan"), data).stdout)

    def test_unwritable_plugin_data_denies_egm_agent(self):
        with tempfile.TemporaryDirectory() as root:
            blocked = os.path.join(root, "not-a-dir")
            open(blocked, "w").close()
            self.assertDenied(run(pre_agent(SID, "egm-zoom-out"), blocked), "EGM state unreadable")

    def test_hooks_json_wires_agent_matcher_pre_and_post(self):
        hooks = json.loads(HOOKS.read_text())["hooks"]
        for event in ("PreToolUse", "PostToolUse"):
            entries = [entry for entry in hooks.get(event, []) if entry.get("matcher") == "Agent"]
            self.assertTrue(entries, event)
            for cmd in commands(entries):
                self.assertIn("scripts/plan_mode.py", cmd)


if __name__ == "__main__":
    unittest.main()
