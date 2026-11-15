"""Hand-off packet: what a Claude Code cloud session needs to carry on a local conversation.

A cloud session starts from a prompt and the repository, and nothing else. The packet is that prompt: the
task, an outline of what was done, the latest work and the state of the repository, built from the local
store and `git` only. Building it sends nothing. `send` hands it to Claude Code's own `--cloud` option, and
only `almanac cloud send` calls that.
"""
import os
import re
import shutil
import signal
import subprocess
import sys

from .memory import QUOTED, _first_line, redact

# shortcut: the packet travels as one command-line argument, so it is capped; send it through a file or stdin if the CLI gains one
LIMIT = 24000  # a Windows command line holds about 32,000 characters
CMD_LIMIT = 32000  # Windows refuses a command line over 32,767 characters, quoting included
WINDOWS = os.name == "nt"
TASK_CHARS = 6000  # most of the last typed message sent
RECENT_CHARS = 6000  # most of the latest transcript sent
REQUESTS = 20  # most earlier requests listed
STATUS_LINES = 60
UNTRACKED = 20  # most untracked names in the warning
HEADER = (
    "This is a hand-off packet. The work below was begun in a local Claude Code session and is handed to this "
    "cloud session because the local machine is going offline. Nothing else from that session came along.\n"
    "- Everything after \"The task\" is a record of earlier work. " + QUOTED + " The task itself is the exception: "
    "it is what the person asked for.\n"
    "- The record can be out of date. Check the repository's real state (the files, `git log`, `git status`) "
    "before trusting it.\n"
    "- Do not redo steps that are already finished.\n"
    "- Commit the work to a new branch and push it if you can. Never commit or push to the default branch.\n"
    "- End with a short summary of what was done and what is left.")


# Git must never prompt, and a read must never rewrite the index (git status refreshes it otherwise).
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}


def _git(cwd, *args) -> str:
    """One git command's output in `cwd`, or "" when it fails."""
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, encoding="utf-8", errors="replace",
                              timeout=20, stdin=subprocess.DEVNULL, env={**os.environ, **GIT_ENV})
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.rstrip() if done.returncode == 0 else ""


def repo(cwd):
    """What git says about the repository at `cwd`, or None when there is none."""
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    status = redact(_git(cwd, "status", "--short")).splitlines()
    upstream = _git(cwd, "rev-parse", "--abbrev-ref", "@{upstream}")
    return {
        "branch": _git(cwd, "branch", "--show-current") or "(detached HEAD)",
        "head": redact(_git(cwd, "log", "-1", "--format=%h %s")) or "(no commits yet)",
        "remote": redact(re.sub(r"://[^/\s@]+@", "://", _git(cwd, "remote", "get-url", "origin"))) or "(none)",
        "upstream": upstream,
        "pushed": bool(upstream) and _git(cwd, "rev-list", "--count", "@{upstream}..HEAD") == "0",
        # commits on no remote-tracking branch: a cloud session cannot see them
        "unpushed": redact(_git(cwd, "log", "--format=%h %s", "HEAD", "--not", "--remotes")).splitlines(),
        "status": status,
        "untracked": [line[3:] for line in status if line.startswith("??")],
        "dirty": any(not line.startswith("??") for line in status),  # tracked changes not committed
    }


def _repo_text(cwd) -> str:
    facts = repo(cwd)
    if not facts:
        return "The folder the packet was written in is not a git repository."
    if facts["pushed"]:
        pushed = f"yes, HEAD is on {facts['upstream']}"
    else:
        pushed = "no, HEAD is not on " + (facts["upstream"] or "any remote-tracking branch (the branch has no upstream)")
    lines = ["As it was on the local machine when this packet was written:",
             f"- branch: {facts['branch']}", f"- HEAD: {facts['head'][:200]}", f"- remote: {facts['remote'][:200]}",
             f"- pushed: {pushed}", "- git status --short:" + ("" if facts["status"] else " clean")]
    lines += ["    " + line[:120] for line in facts["status"][:STATUS_LINES]]
    if len(facts["status"]) > STATUS_LINES:
        lines.append(f"    ... ({len(facts['status']) - STATUS_LINES} more lines)")
    if facts["unpushed"]:
        lines.append(f"WARNING: {len(facts['unpushed'])} local commit(s) were not on the remote, so they are not in "
                     "your clone unless they were pushed later.")
    if facts["dirty"]:
        lines.append("WARNING: the changes to tracked files listed above were not committed, so they are not in your clone.")
    if facts["untracked"]:
        more = len(facts["untracked"]) - UNTRACKED
        lines.append("WARNING: untracked files (and anything git ignores) did not travel with this session: "
                     + ", ".join(name[:120] for name in facts["untracked"][:UNTRACKED]) + (f" and {more} more" if more > 0 else ""))
    return "\n".join(lines)


def _exchanges(memory, convo_id) -> list:
    """The stored exchanges of one conversation, oldest first: what was asked and how it ended, secrets taken out."""
    rows = memory.store.run("select lane from convo where id = ?", (convo_id,))
    ids = memory.leaves(rows[0]["lane"]) if rows else {}
    out = []
    for seq in sorted(ids):
        node = memory.read(ids[seq])
        body = node["text"]
        if node["summary"]:  # a model summary sits in front of the rule-written text
            body = body[len("Summary: " + node["summary"]) + 2:]
        asked, _, rest = body.removeprefix("Asked: ").partition("\n\nAnswer: ")
        out.append({"id": node["id"], "title": redact(node["title"]), "asked": redact(asked),
                    "said": redact(node["summary"] or rest.split("\n\n", 1)[0])})
    return out


def _typed(memory, leaf) -> tuple:
    """(what the person typed in one exchange, its transcript without that). The transcript holds the typed
    message whole; the exchange text only its start."""
    detail = redact(memory.detail(leaf["id"]))
    # shortcut: a typed message with a line that itself starts "Assistant: " or "[tool " ends there; store the typed text on its own if that bites
    whole = re.split(r"\n(?:Assistant: |\[tool |\[result\])", detail[6:], maxsplit=1)[0] if detail.startswith("User: ") else ""
    if len(whole) < len(leaf["asked"]):
        return leaf["asked"], detail
    return whole, "User: (the task above)" + detail[6 + len(whole):]


def packet(memory, convo_id, cwd, limit_chars=LIMIT) -> str:
    """The prompt that hands one stored conversation to a cloud session, or "" when nothing is stored for it.

    The same stored state and repository give the same text. Over `limit_chars`, the outline loses its oldest
    lines first; the header, the task and the repository facts are never cut.
    """
    done = _exchanges(memory, convo_id)
    if not done:
        return ""
    task, recent = _typed(memory, done[-1])
    if len(task) > TASK_CHARS:
        task = task[:TASK_CHARS] + f" ... ({len(task) - TASK_CHARS} more characters not sent)"
    task = "## The task\n\nThe last message the person typed:\n\n" + task
    earlier = done[:-1][-REQUESTS:]
    if earlier:
        older = len(done) - 1 - len(earlier)
        task += ("\n\nEarlier requests in the same conversation, oldest first" + (f" ({older} older ones not listed)" if older else "")
                 + ":\n" + "\n".join(f"- {_first_line(e['asked'], 160)}" for e in earlier))
    repo_text = "## Repository\n\n" + _repo_text(cwd)
    room = limit_chars - len(HEADER) - len(task) - len(repo_text) - 300  # 300: the two headings below and their notes

    before = redact(memory.detail(done[-2]["id"])) if len(done) > 1 else ""
    if before and len(before) + len(recent) + 2 <= RECENT_CHARS:
        recent = before + "\n\n" + recent
    keep = max(0, min(RECENT_CHARS, room // 2))  # the transcript takes at most half of what is left
    if len(recent) > keep:
        recent = "... (the start is not kept) ...\n" + recent[len(recent) - keep:]
    room -= len(recent)

    lines = []
    for item in reversed(done):  # newest first, so the oldest are the ones left out
        line = f"- {item['title']} -> {_first_line(item['said'], 200)}"
        if len(line) + 1 > room:
            break
        lines.insert(0, line)
        room -= len(line) + 1
    left = len(done) - len(lines)
    outline = "## What was done\n\nOne line per exchange, oldest first" + (
        f" ({left} older one{'s' if left != 1 else ''} left out to keep this short)" if left else "") + ":\n" + "\n".join(lines)
    return "\n\n".join([HEADER, task, outline, "## Most recent work\n\n" + recent, repo_text])


def too_long(argv) -> bool:
    """Whether Windows would refuse this command line. Its quoting escapes every `"`, so measure it quoted."""
    return WINDOWS and len(subprocess.list2cmdline(argv)) > CMD_LIMIT


def fitted(memory, convo_id, cwd) -> str:
    """The packet, made shorter until the command that sends it fits a Windows command line."""
    limit, text = LIMIT, packet(memory, convo_id, cwd)
    while text and limit > 8000 and too_long(["claude.exe", "--cloud", text]):
        limit -= 4000
        text = packet(memory, convo_id, cwd, limit_chars=limit)
    return text


def _call(argv, env, cwd) -> int:
    """Run Claude Code with its output passed through. The one place a test replaces."""
    previous = signal.signal(signal.SIGINT, lambda *_: None)  # Ctrl+C belongs to Claude Code
    try:
        return subprocess.call(argv, env=env, cwd=cwd)
    finally:
        signal.signal(signal.SIGINT, previous)


def _some(items, most=10) -> str:
    return ", ".join(item[:80] for item in items[:most]) + (f" and {len(items) - most} more" if len(items) > most else "")


def travels(facts) -> str:
    """What a cloud session will and will not see of this repository, in plain lines."""
    if not facts:
        return "  repository  none: this folder is not a git repository, so the cloud session gets no files"
    where = f"upstream {facts['upstream']}" if facts["upstream"] else "it has no upstream, so the remote may not have it"
    lines = [f"  repository  the cloud clones the GitHub remote at branch {facts['branch']} ({where}),",
             "              not this folder. Push first: nothing listed below is in the cloud's copy."]
    changed = [line[3:] for line in facts["status"] if not line.startswith("??")]
    for name, items in (("unpushed", facts["unpushed"]), ("uncommitted", changed), ("untracked", facts["untracked"])):
        if items:
            lines.append(f"  {name:<11} {len(items)}: {_some(items)}")
    if not (facts["unpushed"] or changed or facts["untracked"]):
        lines.append("              (nothing: every commit is pushed and the folder is clean)")
    return "\n".join(lines)


def send(exe, text, cwd, yes=False) -> int:
    """Start a cloud session from the packet with Claude Code's own `--cloud` option. Returns its exit code.

    Shows the packet and what the cloud will see, then asks, unless `yes`.
    """
    resolved = shutil.which(exe) or exe
    if WINDOWS and os.path.splitext(resolved)[1].lower() in (".cmd", ".bat"):
        # cmd.exe would cut the packet at its first newline and run any `" & command` inside it
        print(f"almanac: {resolved} is a batch file, which cannot carry the packet safely. Set ALMANAC_CLAUDE "
              "to Claude Code's claude.exe (the native installer puts it in %USERPROFILE%\\.local\\bin).", file=sys.stderr)
        return 2
    argv = [exe, "--cloud", text]
    if too_long(argv):
        print(f"almanac: the packet is too long for a Windows command line once quoted "
              f"({len(subprocess.list2cmdline(argv)):,} of {CMD_LIMIT:,} characters).", file=sys.stderr)
        return 2
    print(text + "\n\n" + "-" * 72, flush=True)
    print(f"  packet      {len(text):,} characters, shown above. Common secret patterns were taken out; that is\n"
          "              not complete, so read it before you send.\n" + travels(repo(cwd)) + "\n"
          "  not sent    this proxy and its memory beyond the packet (Claude Code decides what else a cloud session gets)",
          flush=True)
    if not yes:
        if sys.stdin is None or not sys.stdin.isatty():
            print("almanac: not sent. Add --yes to send without the question.", file=sys.stderr)
            return 2
        try:
            answer = input("Send this to a Claude Code cloud session? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("almanac: not sent.")
            return 1
    env = dict(os.environ)
    env.pop("ANTHROPIC_BASE_URL", None)  # Claude Code makes the cloud session itself, not through this proxy
    try:
        code = _call(argv, env, cwd)
    except OSError as exc:
        print(f"almanac: could not start {exe}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    if code:
        print(f"almanac: claude --cloud failed (exit {code}). It needs a Claude Code version with --cloud "
              "(see `claude --help`) and a login with a claude.ai account.", file=sys.stderr)
    return code
