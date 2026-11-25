"""Shared by the guard hooks: read the hook payload, and block with exit code 2 and a reason on stderr."""
import json
import sys


def run(check):
    """Call check(tool_name, tool_input) -> reason or "". Exit 2 on a reason; exit 0 on no reason, bad input or any error."""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        reason = check(payload["tool_name"], payload["tool_input"])
    except Exception:  # a broken guard must never stop Claude Code
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
