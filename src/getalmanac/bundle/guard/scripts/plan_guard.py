"""PreToolUse on ExitPlanMode: block leaving plan mode until the plan was critiqued with an EGM skill."""
import json
import os
import sys

import guardlib

REASON = ("Blocked: critique this plan with EGM before leaving plan mode (Skill egm:egm-brainstorm, egm:egm-draft "
          "or egm:egm-review), then call ExitPlanMode again.")


def check(tool_name, tool_input, payload):
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return None  # fail open: no transcript to judge from
    exit_ids = set()
    critiqued = False
    with open(path, encoding="utf-8", errors="replace") as transcript:
        for line in transcript:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            msg = record.get("message") if isinstance(record, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                kind, name = item.get("type"), item.get("name")
                inp = item.get("input") if isinstance(item.get("input"), dict) else {}
                if kind == "tool_use" and name == "ExitPlanMode":
                    exit_ids.add(item.get("id"))
                elif kind == "tool_result" and item.get("tool_use_id") in exit_ids:
                    critiqued = False  # an earlier plan already exited; a new plan needs its own critique
                elif kind == "tool_use" and name == "Skill" and inp.get("skill", "").startswith("egm:"):
                    critiqued = True
                elif kind == "tool_use" and name in ("Agent", "Task") and inp.get("subagent_type", "").startswith("egm:"):
                    critiqued = True
    return None if critiqued else REASON


if __name__ == "__main__":
    sys.exit(guardlib.run(check, payload=True))
