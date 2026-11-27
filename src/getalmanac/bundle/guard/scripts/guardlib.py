"""Shared by the guard hooks: read the hook payload, and block with exit code 2 and a reason on stderr."""
import json
import sys


def run(check):
    """Call check(tool_name, tool_input) -> reason, {"ask": reason} or None. Exit 2 on a reason; {"ask": reason} asks the user on stdout; exit 0 otherwise, on bad input or on any error."""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        reason = check(payload["tool_name"], payload["tool_input"])
    except Exception:  # a broken guard must never stop Claude Code
        return 0
    if isinstance(reason, dict) and "ask" in reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": reason["ask"]}}))
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
