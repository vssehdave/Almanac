"""Shared by the guard hooks: read the hook payload, and block with exit code 2 and a reason on stderr."""
import json
import sys


def run(check, payload=False):
    """Call check(tool_name, tool_input) -> reason, {"ask": reason} or None; payload=True passes the whole hook payload as a third argument. Exit 2 on a reason; {"ask": reason} asks the user on stdout; exit 0 otherwise, on bad input or on any error."""
    try:
        p = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if payload:
            reason = check(p["tool_name"], p.get("tool_input", {}), p)
        else:
            reason = check(p["tool_name"], p["tool_input"])
    except Exception:  # a broken guard must never stop Claude Code
        return 0
    if isinstance(reason, dict) and "ask" in reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": reason["ask"]}}))
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
