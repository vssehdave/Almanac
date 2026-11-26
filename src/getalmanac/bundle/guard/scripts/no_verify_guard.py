"""PreToolUse: block commands that skip git hooks or signing, or that destroy work or data, until the user confirms."""
import re
import sys

import guardlib

REASON = ("Blocked: this command skips git hooks or commit signing ({what}). Fix what the hook or signing "
          "reports instead of bypassing it. If it really must be skipped, ask the user first.")

DESTRUCTIVE_REASON = ("Blocked: this command destroys work or data ({what}). Ask the user first and run it only after "
                      "they confirm.")

_destructive = re.compile(
    r"^\s*(?:(?:sudo|env|command)(?:\s+-\S+)*\s+)?(?:"
    r"rm\b(?=.*(?:\s-[a-zA-Z]*[rR]|\s--recursive\b))(?=.*(?:\s-[a-zA-Z]*f|\s--force\b)).*"
    r"|git\s+reset\s+--hard\b.*"
    r"|git\s+clean\b(?=.*(?:\s-[a-zA-Z]*f|\s--force\b)).*"
    r"|git\s+checkout\s+(?:--\s+)?\.(?!\S).*"
    r"|git\s+restore\s+\.(?!\S).*"
    r"|git\s+branch\s+-D\b.*"
    r"|git\s+push\b(?=.*\s(?:--force\S*|-f)(?!\S)).*"
    r"|(?i:remove-item)\b(?=.*\s-(?i:recurse)\b)(?=.*\s-(?i:force)\b).*"
    r")")
_sql = re.compile(r"(?i:DROP\s+(?:TABLE|DATABASE|SCHEMA)\b|ALTER\s+TABLE\b.*\bDROP\s+COLUMN\b)")
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
        if re.search(r"\bgit\b", part):
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
        found = _destructive.search(part)
        if found:
            return DESTRUCTIVE_REASON.format(what=found.group().strip())
    for part in _segment.split(command):  # SQL sits inside quotes, so it is checked on the original text
        found = _sql.search(part)
        if found and not re.match(r"\s*git\b", _quoted.sub('""', part)):
            return DESTRUCTIVE_REASON.format(what=found.group().strip())
    return ""


if __name__ == "__main__":
    sys.exit(guardlib.run(check))
