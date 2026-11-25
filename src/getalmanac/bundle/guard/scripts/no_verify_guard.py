"""PreToolUse: block commands that skip git hooks or commit signing."""
import re
import sys

import guardlib

REASON = ("Blocked: this command skips git hooks or commit signing ({what}). Fix what the hook or signing "
          "reports instead of bypassing it. If it really must be skipped, ask the user first.")

_quoted = re.compile(r"\"[^\"]*\"|'[^']*'")
_segment = re.compile(r"&&|\|\||[;|\n]")
_override = re.compile(r"-c\s+[\"']?(?:core\.hooksPath|commit\.gpgsign\s*=\s*false)", re.I)
_config = re.compile(r"\bgit\b.*\bconfig\b.*\bcore\.hooksPath\b", re.I)


def check(tool, args):
    if tool not in ("Bash", "PowerShell"):
        return ""
    command = args.get("command", "")
    match = _override.search(command) or _config.search(command)
    if match:
        return REASON.format(what=match.group().strip())
    for part in _segment.split(_quoted.sub('""', command)):  # quotes removed so a commit message cannot trigger it
        if not re.search(r"\bgit\b", part):
            continue
        found = re.search(r"--no-verify\b|--no-gpg-sign\b", part)
        if not found and re.search(r"\bcommit\b", part):
            found = re.search(r"\s-[a-zA-Z]*n[a-zA-Z]*(?=\s|$)", part)
        if found:
            return REASON.format(what=found.group().strip())
    return ""


if __name__ == "__main__":
    sys.exit(guardlib.run(check))
