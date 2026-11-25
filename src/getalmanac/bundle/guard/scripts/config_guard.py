"""PreToolUse: block edits to lint, format, type-check and test config files."""
import os
import re
import sys

import guardlib

# The one list of protected file names (matched on the base name). Add a pattern here to protect another file.
NAMES = [
    r"\.eslintrc(?:\.\w+)?", r"eslint\.config\.\w+", r"\.prettierrc(?:\.\w+)?", r"prettier\.config\.\w+",
    r"ruff\.toml", r"\.ruff\.toml", r"mypy\.ini", r"\.flake8", r"tox\.ini", r"pytest\.ini", r"setup\.cfg",
    r"tsconfig(?:\.[\w-]+)?\.json", r"\.coveragerc",
]
# pyproject.toml is guarded only inside these sections, because it also holds the package's own metadata.
SECTION = re.compile(r"^\[\[?tool\.(?:ruff|mypy|pytest|coverage)\b[^\]]*\]", re.M)

REASON = ("Blocked: {what} is a lint, format, type-check or test config. Fix the code so the check passes "
          "instead of loosening the check. If the config really must change, ask the user first.")

_name = "|".join(NAMES)
_in_command = rf"(?<![\w.-])(?:{_name}|pyproject\.toml)(?![\w-])"
_writes = re.compile(
    r"\b(?:tee|sed\s+-\w*i|perl\s+-\w*i|rm|del|truncate|Set-Content|Add-Content|Out-File|Remove-Item|"
    r"Rename-Item|New-Item|write_text)\b", re.I)
_copies = re.compile(r"\b(?:cp|mv|Copy-Item|Move-Item)\b", re.I)
_redirect = re.compile(r"""\d?>>?\s*("[^"]*"|'[^']*'|[^\s;|&]+)""")
_split = re.compile(r"&&|\|\||[;|\n]")


def tool_sections(text):
    """Each [tool.ruff|mypy|pytest|coverage...] section header with its body."""
    out, heads = {}, list(re.finditer(r"^\[.*\]\s*$", text, re.M))
    for i, head in enumerate(heads):
        if SECTION.match(head.group()):
            out[head.group().strip()] = text[head.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
    return out


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def file_edit(path, edits):
    """The reason to block a file edit, or "". edits: (old, new) pairs, or None for a whole-file write."""
    base = os.path.basename(path.replace("\\", "/"))
    if any(re.fullmatch(n, base, re.I) for n in NAMES):
        return REASON.format(what=base)
    if base == "pyproject.toml":
        before = read(path)
        after = before
        for old, new in edits:
            after = new if old is None else after.replace(old, new, 1)
        if tool_sections(before) != tool_sections(after):
            return REASON.format(what="the tool sections of pyproject.toml")
    return ""


def _is_config(path):
    base = os.path.basename(path.strip("\"'").replace("\\", "/"))
    return base.lower() == "pyproject.toml" or any(re.fullmatch(n, base, re.I) for n in NAMES)


def command_edit(command):
    for part in _split.split(command):  # each piece of a compound command on its own
        names = re.findall(_in_command, part, re.I)
        if not names:
            continue
        writes = (_writes.search(part) or any(_is_config(t) for t in _redirect.findall(part))
                  or (_copies.search(part) and _is_config(part.split()[-1])))  # only the last argument is written
        if writes:
            if all(n.lower() == "pyproject.toml" for n in names) and "[tool." not in part:
                continue  # pyproject.toml is guarded only for its tool sections
            return REASON.format(what=names[0])
    return ""


def check(tool, args):
    if tool in ("Bash", "PowerShell"):
        return command_edit(args.get("command", ""))
    if tool == "Write":
        return file_edit(args["file_path"], [(None, args["content"])])
    if tool == "Edit":
        return file_edit(args["file_path"], [(args["old_string"], args["new_string"])])
    if tool == "MultiEdit":
        return file_edit(args["file_path"], [(e["old_string"], e["new_string"]) for e in args["edits"]])
    return ""


if __name__ == "__main__":
    sys.exit(guardlib.run(check))
