"""PreToolUse: block commands that skip git hooks or signing, or ask first for commands that destroy work or data."""
import re
import sys

import guardlib

REASON = ("Blocked: this command skips git hooks or commit signing ({what}). Fix what the hook or signing "
          "reports instead of bypassing it. If it really must be skipped, ask the user first.")

DESTRUCTIVE_REASON = ("This command destroys work or data ({what}). Claude Code will ask the user to approve it "
                      "before it runs.")

_prefix = (r"(?:(?:[({!]|then\b|do\b|\w+=\S*|(?:sudo|env|command|time|nohup|nice|xargs|eval|exec)\b"
           r"(?:\s+-\S+(?:\s+(?!-)\S+)?)*|find\b[^|;&]*?\s-exec(?:dir)?\b)\s*)*(?:\S*/)?[\\]?")
_git = r"git(?:\s+-\S+(?:\s+(?!-)\S+)?)*\s+"
_force = r"(?=.*(?:\s-[a-zA-Z]*f|\s--force\b))"
_destructive = re.compile(
    r"^\s*" + _prefix + r"(?:"
    r"rm\b(?=.*(?:\s-[a-zA-Z]*[rR]|\s--recursive\b))" + _force + r".*"
    r"|(?i:remove-item|ri|rd|rmdir|del|erase|rm)\b(?=.*\s-(?i:r(?:e(?:c(?:u(?:r(?:s(?:e)?)?)?)?)?)?)\b)(?=.*\s-(?i:fo(?:r(?:c(?:e)?)?)?)\b).*"
    r"|(?i:(?:rd|rmdir)\b(?=.*\s/s\b)).*"
    r"|" + _git + r"(?:"
    r"reset\s+--hard\b"
    r"|clean\b" + _force +
    r"|checkout\s+(?:(?!-)\S+\s+)?(?:--\s+)?\.(?!\S)"
    r"|restore\b(?:(?!.*\s(?:--staged|-S)\b)|(?=.*\s(?:--worktree|-W)\b))\s+(?:-\S+\s+)*(?:\.|:/)(?!\S)"
    r"|stash\s+(?:drop|clear)\b"
    r"|branch\b(?:(?=.*\s-[a-zA-Z]*D)|(?=.*\s(?:-[a-zA-Z]*d|--delete)(?!-))(?=.*\s(?:-[a-zA-Z]*f|--force)(?!-)))"
    r"|push\b(?:(?=.*\s(?:--force|-f)(?!\S))|(?=.*\s\+\S))"
    r").*"
    r")")
_sql_cmd = re.compile(r"^\s*" + _prefix + r"(?:sqlite3|psql|mysql|sqlcmd|duckdb)(?:\.exe)?\b")
_sql = re.compile(r"(?i:DROP\s+(?:TABLE|DATABASE|SCHEMA)\b|ALTER\s+TABLE\b.*\bDROP\s+COLUMN\b)")
_shell = re.compile(r"\b(?:(?:ba|z|da|k)?sh\s+(?:-\S+\s+)*?-\w*c|(?i:pwsh|powershell)(?:\.exe)?\s+(?:-\S+\s+)*?-(?i:c|command)"
                    r"|(?i:cmd)(?:\.exe)?\s+(?:/\S+\s+)*?/(?i:c|k)|eval|exec)\s+([\"'])(.*?)\1", re.S)
_heredoc = re.compile(r"(<<-?\s*([\"'])(\w+)\2[^\n]*\n)(.*?)(?=\n\s*\3\s*(?:\n|$)|\Z)", re.S)
_lead = re.compile(r"""^\s*([\"'])[\w./\-]+\1""")
_subshell = re.compile(r"\$\((.*?)\)|`(.*?)`", re.S)
_quoted = re.compile(r"\"[^\"]*\"|'[^']*'")
_segment = re.compile(r"&&|\|\||[;|\n]|(?<![&>])&(?![&>])")
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


def _spans(text):
    """Split on ; | && || newline outside quotes. Return (original, quote-masked) for each segment."""
    masked = _quoted.sub(lambda m: m.group()[0] + "_" * (len(m.group()) - 2) + m.group()[-1], text)
    start, spans = 0, []
    for cut in list(_segment.finditer(masked)) + [re.search(r"$", masked)]:
        spans.append((text[start:cut.start()], masked[start:cut.start()]))
        start = cut.end()
    return spans


def _destroys(text):
    text = _heredoc.sub(lambda m: m.group(1) + re.sub(r"[`$]", "_", m.group(4)), text)  # quoted heredoc is data
    text = text.replace(chr(92) + "`", "__")
    for part, masked in _spans(text):
        lead = _lead.match(part)
        if lead:  # "rm" -rf x is rm
            masked = masked[:lead.start(1)] + " " + part[lead.start(1) + 1:lead.end() - 1] + " " + masked[lead.end():]
        found = _destructive.search(masked)
        if found:
            return part[found.start():found.end()].strip()
        found = _sql.search(part) if _sql_cmd.search(masked) else None
        if found:
            return found.group().strip()
        inner = [m.group(2) for m in _shell.finditer(part)] + [m.group(1) or m.group(2) for m in _subshell.finditer(part)]
        for body in inner:
            found = _destroys(body)
            if found:
                return found
    return ""


def check(tool, args):
    if tool not in ("Bash", "PowerShell"):
        return ""
    original = args.get("command", "")
    command = re.sub(r"(-c\s+)[\"']", r"\1", original)  # -c "core.hooksPath=x" is still an override
    for part, _ in _spans(command):
        part = _quoted.sub('""', part)  # quotes removed so a commit message cannot trigger it
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
    found = _destroys(original)  # after every segment is cleared of --no-verify, which blocks outright
    if found:
        return {"ask": DESTRUCTIVE_REASON.format(what=found)}
    return ""


if __name__ == "__main__":
    sys.exit(guardlib.run(check))
