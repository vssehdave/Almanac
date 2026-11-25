"""PreToolUse: block commands that skip git hooks or commit signing."""
import re
import sys

import guardlib

REASON = ("Blocked: this command skips git hooks or commit signing ({what}). Fix what the hook or signing "
          "reports instead of bypassing it. If it really must be skipped, ask the user first.")

_quoted = re.compile(r"\"[^\"]*\"|'[^']*'")
_segment = re.compile(r"&&|\|\||[;|\n]")
_override = re.compile(r"-c\s+(?:core\.hooksPath|commit\.gpgsign\s*=\s*false)", re.I)
_config = re.compile(r"\bgit\b.*\bconfig\b.*\bcore\.hooksPath\b", re.I)
_commit = re.compile(r"\bgit(?:\s+-\S+(?:\s+\S+)?)*\s+commit\b(.*)", re.S)
_ARG_FLAGS = "mCcFuS"  # short flags that take a value; the rest of their group is that value


def _short_n(args):
    for group in re.findall(r"(?<!\S)-([a-zA-Z]+)", args):
        for letter in group:
            if letter == "n":
                return "-" + group
            if letter in _ARG_FLAGS:
                break
    return ""


def check(tool, args):
    if tool not in ("Bash", "PowerShell"):
        return ""
    command = re.sub(r"(-c\s+)[\"']", r"\1", args.get("command", ""))  # -c "core.hooksPath=x" is still an override
    for part in _segment.split(_quoted.sub('""', command)):  # quotes removed so a commit message cannot trigger it
        if not re.search(r"\bgit\b", part):
            continue
        found = _override.search(part)
        if not found and not re.search(r"--(?:get|unset)", part):
            found = _config.search(part)
        if not found:
            found = re.search(r"--no-verify\b|--no-gpg-sign\b", part)
        commit = _commit.search(part)
        if not found and commit:
            found = re.search(r"\S+", _short_n(commit.group(1)))
        if found:
            return REASON.format(what=found.group().strip())
    return ""


if __name__ == "__main__":
    sys.exit(guardlib.run(check))
