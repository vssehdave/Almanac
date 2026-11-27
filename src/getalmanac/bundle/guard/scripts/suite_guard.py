"""PreToolUse: block running the full test suite locally. CI runs it; one test file is fine."""
import re
import sys

import guardlib

REASON = ("Blocked: the full test suite runs in CI, not locally. Run the one test file you changed: "
          "PYTHONPATH=src python -m unittest discover -s tests -p test_x.py")

_rtk = r"^(?:rtk\s+)?(?:env\s+)?(?:\w+=\S*\s+)*"
_exe = r"(?:\S*[\\/])?(?:python3?|py)(?:\.exe)?(?:\s+-3(?:\.\d+)?)?"
_unittest = re.compile(_rtk + _exe + r"\s+-m\s+unittest\b(.*)$")
_pytest = re.compile(_rtk + r"(?:" + _exe + r"\s+-m\s+pytest|(?:\S*[\\/])?(?:pytest|py\.test)(?:\.exe)?)\b(.*)$")
_split = re.compile(r"&&|\|\||[;|\n]")


def _is_full_run(segment):
    unittest = _unittest.match(segment)
    if unittest:
        args = unittest.group(1).split()
        if not args or all(t.startswith("-") for t in args):
            return True
        return args[0] == "discover" and not any(t.startswith(("-p", "--pattern")) for t in args)
    pytest = _pytest.match(segment)
    if not pytest:
        return False
    args = pytest.group(1).split()
    if "-k" in args:
        return False
    skip_value = False
    for t in args:
        if skip_value:
            skip_value = False
        elif t in ("-m", "-p", "-c", "-o", "--rootdir"):
            skip_value = True
        elif not t.startswith("-") and (t.endswith(".py") or "::" in t):
            return False
    return True


def check(tool_name, tool_input, payload):
    if tool_name not in ("Bash", "PowerShell"):
        return None
    command = tool_input.get("command", "")
    command = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?^\1\s*$", "", command, flags=re.S | re.M)
    command = re.sub(r'"(?:\\.|[^"\\])*"|\'[^\']*\'', '""', command)
    if any(_is_full_run(segment.strip()) for segment in _split.split(command)):
        return REASON
    return None


if __name__ == "__main__":
    sys.exit(guardlib.run(check, payload=True))
