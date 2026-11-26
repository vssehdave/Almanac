"""PreToolUse on ExitPlanMode: the first time a plan is presented, ask for an egm-review first."""
import hashlib
import json
import os
import sys
import tempfile

REASON = ("Before presenting this plan, run the egm-review skill on the plan text, fold the findings into the plan, "
          "then call ExitPlanMode again. Small plans need no review: say so and call ExitPlanMode again.")


def seen_dir():
    return os.path.join(os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir(), "egm-plan-review")


def check(tool_input):
    """The reason to block, or "" when this plan was already sent back once."""
    plan = tool_input.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return ""
    mark = os.path.join(seen_dir(), hashlib.sha256(plan.encode("utf-8")).hexdigest())
    if os.path.exists(mark):
        return ""
    os.makedirs(seen_dir(), exist_ok=True)
    with open(mark, "w"):  # written before blocking, so a failed write allows the call instead of looping
        pass
    return REASON


def main():
    try:
        reason = check(json.loads(sys.stdin.buffer.read().decode("utf-8"))["tool_input"])
    except Exception:  # a broken hook must never stop Claude Code
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
