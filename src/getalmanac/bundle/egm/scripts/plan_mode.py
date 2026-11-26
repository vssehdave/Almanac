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
