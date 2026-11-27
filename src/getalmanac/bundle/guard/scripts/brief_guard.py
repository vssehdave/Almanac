"""PreToolUse: block a builder brief that is missing its required parts (no-git line, closing report line, test command)."""
import re
import sys

import guardlib

REASON = "Blocked: this builder brief is missing: {missing}."

_test_command = re.compile(r"-p\s+test_\S+\.py|unittest\s+tests?\.\S+")


def check(tool_name, tool_input, payload):
    if tool_input.get("subagent_type") != "almanac:builder":
        return None
    prompt = tool_input.get("prompt", "")
    low = prompt.lower()
    missing = []
    if "no git" not in low:
        missing.append("a 'no git, no commit' line")
    if "your last message must be the report" not in low:
        missing.append("the closing line 'Your last message must be the report.'")
    if not _test_command.search(prompt):
        missing.append("one test command for the file, e.g. PYTHONPATH=src python -m unittest discover -s tests -p test_x.py")
    if not missing:
        return None
    return REASON.format(missing="; ".join(missing))


if __name__ == "__main__":
    sys.exit(guardlib.run(check, payload=True))
