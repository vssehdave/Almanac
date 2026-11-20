"""Put Claude Code's saved transcripts into project memory: `almanac memory import`.

Claude Code keeps every session as a JSONL file under ~/.claude/projects/<folder>/. The proxy only
remembers conversations that went through it; this reads the older ones and stores them the same way,
through Memory.index, so they get the same cleaning, redaction, status tags and search as live ones.

The folder a session was started in is not the project: one folder is often used to work on several
repositories. So the project is decided for each exchange, from where its work happened (the files its
tool calls touch, the folders of `cd` and `git -C`, a deleted worktree's name), and a session is cut into
runs of the same project; each run is stored as its own conversation (lane `import:<session>:<n>`).

Not done here, and why:
  * Model summaries. This command makes no model call and never reads a login; imported exchanges keep
    their rule-written text until `almanac memory summarize` (summarize.refine) writes them.
  * Subagent transcripts (`<session>/subagents/`) and lines marked `isSidechain`.
  * Rewound branches: a transcript is read in file order.
"""
import os
import json
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import config, request as rq
from .memory import OPEN_SECONDS, Memory
from .project import describe, normalize, repo_root, tool_paths
from .status import aged, combine, rule
from .store import Store
from . import summarize

_ARG = r"""("[^"]+"|'[^']+'|[^\s;&|<>()]+)"""
_CD = re.compile(r"(?<![\w./-])(?:cd|pushd)\s+(?:/d\s+)?" + _ARG)
_GIT_C = re.compile(r"\bgit\s+(?:-[\w-]+\s+)*?-C\s+" + _ARG)


def transcript_dirs(cfg, purpose) -> list:
    """The Claude Code config folders (each with a projects/ folder of session files) one reader may use:
      "autoimport": ~/.claude only. It runs unasked every hour, so another config folder (a work account, a sandbox)
                    is never imported, whatever CLAUDE_CONFIG_DIR says;
      "import":     what `almanac memory import` looks in for a folder name it was given: $CLAUDE_CONFIG_DIR, else ~/.claude.
    (The playbook miner's folders are getplaybook's: config.transcript_dirs.)"""
    home, env = Path.home() / ".claude", os.environ.get("CLAUDE_CONFIG_DIR") or ""
    if purpose == "autoimport":
        return [home]
    return [Path(env).expanduser() if env else home]


def transcripts(words) -> list:
    """The session files named by FOLDER_OR_FILE words: a file, a folder, or a folder name under ~/.claude/projects.
    A subfolder (a session's subagents) is not entered."""
    found = []
    for word in words:
        path = Path(word)
        if not path.exists() and not path.is_absolute():
            path = transcript_dirs(None, "import")[0] / "projects" / word
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(path.glob("*.jsonl")))
        else:
            raise FileNotFoundError(f"no transcript folder or file {word!r} (looked in {transcript_dirs(None, 'import')[0] / 'projects'})")
    return found


def _time(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _local_command(content, state) -> bool:
    """Whether a user line is part of a local command (/clear, /model, /login): the caveat line before it, its
    `<command-name>` line and its output. The model never sees these, and merged into the next typed prompt they
    would make it look like the body of a command. `state` is [a caveat was just seen]."""
    if not isinstance(content, str):
        content = next((b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"), "")
    head = (content or "").lstrip()
    if head.startswith("<local-command-caveat>"):
        state[0] = True
        return True
    if head.startswith("<local-command-stdout>"):
        return True
    if head.startswith("<command-name>") and state[0]:
        return True
    state[0] = False
    return False


def load(path) -> dict:
    """One transcript as the messages the API saw: session, start folder, messages, and per message its time and folder.

    Claude Code writes one line per content block; blocks of one reply (and the results of parallel tool calls)
    are joined into one message. Thinking blocks, meta lines and sidechains are dropped."""
    messages, stamps, cwds, session, start, local = [], [], [], "", "", [False]
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or line.get("type") not in ("user", "assistant"):
                continue
            if line.get("isSidechain") or line.get("isCompactSummary"):
                continue
            body = line.get("message")
            if not isinstance(body, dict) or body.get("role") not in ("user", "assistant"):
                continue
            if line["type"] == "user" and _local_command(body.get("content"), local) or line.get("isMeta"):
                continue
            origin = line.get("origin")
            if line["type"] == "user" and isinstance(origin, dict) and origin.get("kind") not in (None, "human") and not any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in body.get("content") or []):
                continue  # a task notification, an agent's message, a plugin's or an auto-continue: not the person's words
            session = session or str(line.get("sessionId") or "")
            start = start or str(line.get("cwd") or "")
            content = body.get("content")
            content = [{"type": "text", "text": content}] if isinstance(content, str) else [
                b for b in content or [] if isinstance(b, dict) and b.get("type") not in ("thinking", "redacted_thinking")]
            if not content:
                continue
            role = body["role"]
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(content)
            else:
                messages.append({"role": role, "content": list(content)})
                stamps.append(_time(line.get("timestamp")))
                cwds.append(str(line.get("cwd") or ""))
    return {"session": session or Path(path).stem, "cwd": start, "messages": messages, "stamps": stamps, "cwds": cwds}


def locate(path: str) -> str:
    """The repository root a path belongs to, or "". A path that no longer exists is matched by its name: the
    first folder under the nearest existing one, with its `-suffix` parts taken off (`Almanac-sec2` -> `Almanac`).
    shortcut: a deleted folder is only matched to a repository that still sits beside it."""
    return repo_root(path) or _by_name(path)


def _by_name(path: str) -> str:
    """The repository a deleted folder's name points to (see locate), or ""."""
    here = Path(normalize(path))
    try:
        for parent in here.parents:
            if parent.exists():
                name = here.relative_to(parent).parts[0]
                while "-" in name:
                    name = name.rsplit("-", 1)[0]
                    if (parent / name / ".git").exists():
                        return repo_root(str(parent / name))
                return ""
    except (OSError, ValueError, IndexError):
        pass
    return ""


def _folders(group, cwds) -> list:
    """The folders a Bash command moves into: `cd X`, `pushd X`, `git -C X`. A relative one is read against the
    folder the command is in at that point (the line's folder, or the last `cd` before it). A folder that is gone
    counts only when a deleted worktree's name still points to a repository."""
    found = []
    for msg, cwd in zip(group, cwds):
        for block in rq.blocks(msg):
            command = (block.get("input") or {}).get("command") if block.get("type") == "tool_use" else None
            if not isinstance(command, str):
                continue
            here = cwd
            for match in sorted([*_CD.finditer(command), *_GIT_C.finditer(command)], key=lambda m: m.start()):
                arg = match.group(1).strip("\"'")
                arg = os.path.expanduser(arg) if arg[:2] in ("~", "~/", "~\\") else arg  # `cd ~/GitHub/X`: the owner's home
                if not arg or arg[0] in "$~-%" or "$" in arg:
                    continue
                if re.match(r"^(?:[A-Za-z]:[\\/]|/)", arg):
                    path = normalize(arg)
                elif here:
                    path = normalize(os.path.normpath(os.path.join(here, arg)))
                else:
                    continue
                if match.re is _CD:
                    here = path
                if os.path.isdir(path) or _by_name(path):
                    found.append(path)
    return found


def scratch_dirs() -> list:
    """Folders whose repositories are throwaway clones (the temp folder, Claude's own): they never get a vote."""
    return [normalize(tempfile.gettempdir()), normalize(str(Path.home() / ".claude"))]


def _scratch(root) -> bool:
    low = normalize(root).lower()
    return any(low == d.lower() or low.startswith(d.lower() + "/") for d in scratch_dirs())


def exchange_root(group, cwds, start_cwd) -> str:
    """The repository one exchange worked in, or "" when nothing says. Each path or folder is one vote; a session
    folder that never changed says nothing (Claude Code starts there whatever the work turns out to be)."""
    votes = Counter()
    roots = {}
    for path in [*tool_paths(group), *_folders(group, cwds)]:
        root = locate(path)
        if root and not _scratch(root):
            key = describe(root)[0]
            votes[key] += 1
            roots.setdefault(key, root)
    if not votes:
        for cwd in cwds:
            root = locate(cwd) if cwd and normalize(cwd) != normalize(start_cwd) else ""
            if root and not _scratch(root):
                key = describe(root)[0]
                votes[key] += 1
                roots.setdefault(key, root)
    return roots[max(sorted(votes), key=lambda k: votes[k])] if votes else ""


def plan(loaded) -> list:
    """The runs of one session: [{"root", "first", "last"}] over its exchanges (indexes of human turns)."""
    messages = loaded["messages"]
    turns = rq.human_turns(messages)
    home = loaded["cwd"]
    home_root = locate(home) or (describe(home)[2] if home else "")
    ends = [turns[n + 1] if n + 1 < len(turns) else len(messages) for n in range(len(turns))]
    found = [exchange_root(messages[s:e], loaded["cwds"][s:e], home) for s, e in zip(turns, ends)]
    # leading exchanges with no signal take the first real project, not the folder Claude Code happened to start in
    last = next((r for r in found if r), home_root)
    runs = []
    for n, root in enumerate(found):
        root = root or last
        last = root
        if runs and runs[-1]["root"] == root:
            runs[-1]["last"] = n
        else:
            runs.append({"root": root, "first": n, "last": n})
    return runs


def _store_run(memory, lane, session, key, messages, stamps):
    """Index one run. A run longer than memory.BACKFILL is fed in growing pieces, since one call stores only the newest."""
    from . import memory as memory_mod
    turns = rq.human_turns(messages)
    written = 0
    cut = memory_mod.BACKFILL - 1
    for end in range(cut, len(turns), cut) if len(turns) > memory_mod.BACKFILL else ():
        written += memory.index(lane, session, key, messages[:turns[end]], finished=True, assign=lambda g, p: key, stamps=stamps)
    written += memory.index(lane, session, key, messages, finished=True, assign=lambda g, p: key, stamps=stamps)
    _record_library(memory, lane, messages)
    return written


def _record_library(memory, lane, messages):
    """The prompt-library row of each finished exchange, as the live path writes them, so an imported session counts
    for usage pinning and library_find. The last exchange is skipped while it still ends on a tool call (the file is
    still being written): a row is written once and never updated."""
    from . import library
    turns, low = rq.human_turns(messages), library.min_tools()
    for seq, start in enumerate(turns):
        group = messages[start:turns[seq + 1] if seq + 1 < len(turns) else len(messages)]
        if seq + 1 == len(turns) and (group[-1].get("role") != "assistant" or any(b.get("type") == "tool_use" for b in rq.blocks(group[-1]))):
            continue
        library.record(memory, lane, group, seq, low)


def _restatus_last(memory, lane, messages, following, newest):
    """The last exchange of a run is judged by the next run's first message (a project switch hides it from the
    lane's own tagging) and, for the session's last run, as the newest one."""
    turns = rq.human_turns(messages)
    rows = memory.store.run("select n.id, n.mstatus, n.status, n.ts from node n join convo c on c.id = n.convo "
                            "where c.lane = ? and n.kind = 'leaf' and n.seq = ?", (lane, len(turns) - 1))
    if rows:
        status = aged(combine(rule(messages[turns[-1]:], following, newest=newest), rows[0]["mstatus"]), rows[0]["ts"])
        if status != rows[0]["status"]:
            memory.store.run("update node set status = ? where id = ?", (status, rows[0]["id"]))


def _held(memory, lanes, messages, turns) -> bool:
    """Whether one stored conversation (not this session's own lanes) already holds every exchange of the session.
    A conversation that shares only a prefix with it does not count: the rest would be lost."""
    from .memory import exchange
    ends = [turns[i + 1] if i + 1 < len(turns) else len(messages) for i in range(len(turns))]
    marks = [exchange(messages[t:e])["mark"] for t, e in zip(turns, ends)]
    for row in memory.store.run("select c.id, c.lane from convo c join node n on n.convo = c.id and n.kind = 'leaf' "
                                "and n.seq = 0 where n.mark = ?", (marks[0],)):
        have = {r["mark"] for r in memory.store.run("select mark from node where convo = ? and kind = 'leaf'", (row["id"],))}
        if row["lane"] not in lanes and set(marks) <= have:
            return True
    return False


def _import(memory, files, days, idle, now, only=None) -> dict:
    """`only`: a set of project keys; a run of any other project is left out (see autoimport.py)."""
    report = {"projects": {}, "conversations": 0, "exchanges": 0, "statuses": {},
              "skipped": Counter({"already stored": 0, "still open": 0, "older than --days": 0, "no exchanges": 0,
                                  "project not stored yet": 0})}
    before = memory.status_counts()
    loaded = []
    for path in files:
        if now - os.path.getmtime(path) < idle:
            report["skipped"]["still open"] += 1
            continue
        item = load(path)
        item["file"] = path
        turns = rq.human_turns(item["messages"])
        if not turns:
            report["skipped"]["no exchanges"] += 1
        elif days is not None and max(item["stamps"]) < now - days * 86400:
            report["skipped"]["older than --days"] += 1
        else:
            loaded.append(item)
    for item in sorted(loaded, key=lambda i: i["stamps"][0]):  # oldest first
        messages, session = item["messages"], item["session"]
        turns = rq.human_turns(messages)
        runs = plan(item)
        pieces = []
        for n, run in enumerate(runs):
            start = turns[run["first"]]
            end = turns[run["last"] + 1] if run["last"] + 1 < len(turns) else len(messages)
            pieces.append((f"import:{session}:{n}", run["root"], messages[start:end], [item["stamps"][turns[i]] for i in range(run["first"], run["last"] + 1)]))
        if _held(memory, [lane for lane, *_ in pieces], messages, turns):
            report["skipped"]["already stored"] += 1  # the proxy (or an earlier import) kept all of it already
            continue
        for n, (lane, root, msgs, stamps) in enumerate(pieces):
            if only is not None and (not root or memory.projects.resolve(describe(root)[0]) not in only):
                report["skipped"]["project not stored yet"] += 1
                continue
            new = not memory.store.run("select 1 from convo where lane = ?", (lane,))
            key = memory.projects.see(root, touch=False) if root else ""
            written = _store_run(memory, lane, session, key, msgs, stamps)
            following = rq.human_text(pieces[n + 1][2][0]) if n + 1 < len(pieces) else None
            _restatus_last(memory, lane, msgs, following, newest=n + 1 == len(pieces))
            if written:
                name = describe(root)[1] if root else "(none)"
                entry = report["projects"].setdefault(name, {"conversations": 0, "exchanges": 0})
                entry["conversations"] += new
                entry["exchanges"] += written
                report["conversations"] += new
                report["exchanges"] += written
    after = memory.status_counts()
    report["statuses"] = {name: after[name] - before[name] for name in after}
    report["skipped"] = dict(report["skipped"])
    pending = memory.store.run("select count(*) n from node where kind = 'leaf' and summary = '' and tries < ?", (summarize.MAX_TRIES,))
    report["pending"] = pending[0]["n"]
    return report


def run(memory, words, days=None, dry_run=False, idle=OPEN_SECONDS, only=None) -> dict:
    """Import the transcripts named by `words`. With `dry_run` the same work is done on a copy of the store and
    thrown away, so the report is exactly what a real run would store."""
    files = transcripts(words)
    now = time.time()
    if not dry_run:
        return _import(memory, files, days, idle, now, only)
    scratch = tempfile.mkdtemp(prefix="almanac-dry-")
    store = None
    try:
        target = sqlite3.connect(os.path.join(scratch, "almanac.db"))
        memory.store.db.backup(target)
        target.close()
        store = Store(os.path.join(scratch, "almanac.db"))
        return _import(Memory(store), files, days, idle, now, only)
    finally:
        if store:
            store.db.close()
        shutil.rmtree(scratch, ignore_errors=True)


def render(report, dry_run=False) -> str:
    verb = "would store" if dry_run else "stored"
    lines = [f"{verb} {report['conversations']} conversation(s), {report['exchanges']} exchange(s)"]
    for name, entry in sorted(report["projects"].items(), key=lambda kv: -kv[1]["exchanges"]):
        lines.append(f"  {name}: {entry['conversations']} conversation(s), {entry['exchanges']} exchange(s)")
    lines.append("statuses: " + ", ".join(f"{k} {v}" for k, v in report["statuses"].items()))
    skipped = ", ".join(f"{v} {k}" for k, v in report["skipped"].items() if v)
    if skipped:
        lines.append(f"sessions skipped: {skipped}")
    lines.append(f"summaries pending: {report['pending']} exchange(s) have no model summary; "
                 "imported ones keep their rule-written text until `almanac memory summarize`")
    return "\n".join(lines)
