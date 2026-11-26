"""Command hook for plan mode: nudge an EGM critique on prompts, and hand approved plans to the planner."""
import json
import os
import re
import sys
import tempfile

NUDGE = ("Plan mode: treat the person's prompt as the plan and critique it with EGM before answering. "
         "Pick the step: egm-brainstorm when the idea has no shape yet, egm-draft when the direction is settled "
         "but not written down, otherwise egm-review on the prompt and the plan so far. "
         "If the person named a step, use that one. Report the critique in the chat; edit no files.")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")


def plans_dir():
    return os.path.join(os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir(), "egm-plans")


def handoff(path):
    return (f"The plan was approved. Before any edit: load the egm-handoff skill and write the plan brief to {path}. "
            "Then send that path, and any concepts_brief.md or design doc written in plan mode, to the almanac:planner agent. "
            "Show the person the planner's checklist; pin it with memory_pin_add only after they approve it.")


def _lines(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _transcript(event):
    path = event.get("transcript_path")
    if not isinstance(path, str):
        return None
    try:
        return _lines(path)
    except OSError:
        return None  # unreadable transcript: fail open


def _content(line):
    message = line.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _is_prompt(line):
    if line.get("type") != "user" or line.get("isMeta"):  # injected notices are not the person's prompt
        return False
    content = _content(line)
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        not (isinstance(b, dict) and b.get("type") == "tool_result") for b in content)


def _is_egm(line):
    content = _content(line)
    if line.get("type") != "assistant" or not isinstance(content, list):
        return False
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        if b.get("name") == "Skill" and isinstance(inp.get("skill"), str) and inp["skill"].startswith("egm:"):
            return True
        if b.get("name") == "Agent" and isinstance(inp.get("subagent_type"), str) and inp["subagent_type"].startswith("egm:"):
            return True
    return False


def respond(event):
    """The hook output dict for this event, or None when there is nothing to add."""
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit" and event.get("permission_mode") == "plan":
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": NUDGE}}
    if name == "PostToolUse" and event.get("tool_name") == "ExitPlanMode":
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            return None  # the id goes into a path, so only plain ids are allowed
        os.makedirs(plans_dir(), exist_ok=True)
        path = os.path.join(plans_dir(), f"{session_id}.md")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": handoff(path)}}
    if name == "Stop" and event.get("permission_mode") == "plan" and event.get("stop_hook_active") is not True:
        lines = _transcript(event)
        if lines is None:
            return None
        last = max((i for i, line in enumerate(lines) if _is_prompt(line)), default=-1)
        if any(_is_egm(line) for line in lines[last + 1:]):
            return None
        return {"decision": "block",
                "reason": "Plan mode: run EGM (egm-review, egm-draft or egm-brainstorm) on this prompt before answering."}
    if name == "PreToolUse" and event.get("tool_name") == "ExitPlanMode":
        lines = _transcript(event)
        if lines is None or any(_is_egm(line) for line in lines):
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "Run EGM on the plan before leaving plan mode."}}
    return None


def main():
    try:
        out = respond(json.loads(sys.stdin.buffer.read().decode("utf-8")))
    except Exception:  # a broken hook must never stop Claude Code
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
