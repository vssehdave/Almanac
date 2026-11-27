"""PreToolUse: block running the full test suite locally. CI runs it; one test file is fine."""
import re
import sys

import guardlib

REASON = ("Blocked: the full test suite runs in CI, not locally. Run the one test file you changed: "
          "PYTHONPATH=src python -m unittest discover -s tests -p test_x.py")

_rtk = r"^(?:rtk\s+)?"
_exe = r"(?:\S*[\\/])?(?:python3?|py)(?:\.exe)?"
_unittest = re.compile(_rtk + _exe + r"\s+-m\s+unittest\b(.*)$")
_pytest = re.compile(_rtk + r"(?:" + _exe + r"\s+-m\s+pytest|(?:\S*[\\/])?(?:pytest|py\.test)(?:\.exe)?)\b(.*)$")
_split = re.compile(r"&&|\|\||[;|\n]")


def _is_full_run(segment):
    tokens = segment.split()
    if "unittest" in tokens and "discover" in tokens and not any(t == "-p" or t.startswith("--pattern") for t in tokens):
        return True
    unittest = _unittest.match(segment)
    if unittest and all(t.startswith("-") for t in unittest.group(1).split()):
        return True
    pytest = _pytest.match(segment)
    return bool(pytest) and all(t.startswith("-") for t in pytest.group(1).split())


def check(tool_name, tool_input, payload):
    if tool_name not in ("Bash", "PowerShell"):
        return None
    command = tool_input.get("command", "")
    if any(_is_full_run(segment.strip()) for segment in _split.split(command)):
        return REASON
    return None


if __name__ == "__main__":
    sys.exit(guardlib.run(check, payload=True))
