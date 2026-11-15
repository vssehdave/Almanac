"""Which project a conversation belongs to.

Claude Code keys its own memory to the folder a session started in, so work on
one repository done from another's folder lands in the wrong place. Here a
project is a git repository, known by its remote URL when it has one, and a
conversation is assigned from evidence in this order:

1. the files its tool calls touch (each path votes for the repository above it),
2. a path or a known project's name in what the person typed,
3. the folder the session started in.

Everything is read from the filesystem. No git command is run.
"""
import os
import re
import time
from pathlib import Path

from . import request as rq

_ABS = re.compile(r"""(?<![\w.~])(?:[A-Za-z]:[\\/]|/(?:[a-z]/)?(?:Users|home|mnt|opt|srv|var|tmp|workspace|workspaces|data|code|src)/)[^\s"'`<>|;,)(*?]+""")
_PATH_KEYS = ("file_path", "path", "notebook_path", "cwd", "directory")
_roots = {}  # directory -> (repository root or "", checked at)
ROOT_TTL = 120


def normalize(path: str) -> str:
    """One spelling per path: Git Bash `/c/x` becomes `C:/x`, slashes forward, no trailing slash."""
    path = (path or "").strip().strip("\"'")
    drive = re.match(r"^/([a-zA-Z])/(.*)$", path)
    if drive and os.name == "nt":
        path = f"{drive.group(1).upper()}:/{drive.group(2)}"
    path = path.replace("\\", "/").rstrip("/")
    if re.match(r"^[a-z]:/", path):
        path = path[0].upper() + path[1:]
    return path


def is_unc(path: str) -> bool:
    """A network path (`//host/share` or `\\\\host\\share`). Looking at one makes Windows contact that host."""
    return path.replace("\\", "/").startswith("//")


def repo_root(path: str) -> str:
    """The git repository containing `path`, or "". A worktree resolves to its main checkout."""
    if is_unc(path):
        return ""
    start = Path(normalize(path))
    now = time.time()
    try:
        probe = start if start.is_dir() else start.parent
    except OSError:
        return ""
    trail = []
    for folder in (probe, *probe.parents):
        key = str(folder)
        cached = _roots.get(key)
        if cached and now - cached[1] < ROOT_TTL:
            root = cached[0]
            break
        trail.append(key)
        try:
            marker = folder / ".git"
            if marker.is_dir():
                root = normalize(key)
                break
            if marker.is_file():  # a worktree or submodule: .git is a pointer file
                pointer = marker.read_text(encoding="utf-8", errors="replace").strip()
                target = pointer.split(":", 1)[1].strip() if pointer.startswith("gitdir:") else ""
                target = normalize(str((folder / target).resolve())) if target else ""
                cut = target.find("/.git/worktrees/")
                root = target[:cut] if cut > 0 else normalize(key)
                break
        except OSError:
            continue
    else:
        root = ""
    for key in trail:
        _roots[key] = (root, now)
    return root


def _remote(root: str) -> str:
    """`host/owner/repo` from the repository's origin URL, or ""."""
    try:
        text = (Path(root) / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = re.search(r'\[remote "origin"\](.*?)(?:\n\[|\Z)', text, re.S)
    url = re.search(r"^\s*url\s*=\s*(\S+)", section.group(1), re.M) if section else None
    if not url:
        return ""
    value = url.group(1).strip()
    if "://" in value:
        value = value.split("://", 1)[1]
        if "@" in value.split("/", 1)[0]:  # drop any user or token in the URL
            value = value.split("@", 1)[1]
    elif re.match(r"^(?:[\w.-]+@)?[\w.-]{2,}:(?![\/])", value):  # scp form: user@host:owner/repo
        value = value.split("@", 1)[-1].replace(":", "/", 1)
    else:
        return ""  # a local path remote says nothing about identity
    return re.sub(r"\.git$", "", value).strip("/").lower()


def describe(path: str):
    """(key, name, root) for the project at `path`. A folder outside git is its own project."""
    root = repo_root(path)
    if root:
        remote = _remote(root)
        if remote:
            return "git:" + remote, remote.rsplit("/", 1)[-1], root
        return "dir:" + root.lower(), root.rsplit("/", 1)[-1], root
    folder = normalize(path)
    return ("dir:" + folder.lower(), folder.rsplit("/", 1)[-1] or folder, folder) if folder else ("", "", "")


def paths_in(text: str) -> list:
    return [p for p in (normalize(m.group(0)) for m in _ABS.finditer(text or "")) if not is_unc(p)]


def tool_paths(messages) -> list:
    """Absolute paths named in the tool calls of these messages."""
    found = []
    for msg in messages:
        for block in rq.blocks(msg):
            if block.get("type") != "tool_use" or not isinstance(block.get("input"), dict):
                continue
            for key, value in block["input"].items():
                if not isinstance(value, str):
                    continue
                if key in _PATH_KEYS and re.match(r"^(?:[A-Za-z]:[\\/]|/)", value) and not is_unc(value):
                    found.append(normalize(value))
                elif key in ("command", "pattern", "prompt"):
                    found.extend(paths_in(value))
    return found


def vote(paths) -> dict:
    """project key -> how many of `paths` sit inside a git repository of that project."""
    tally = {}
    for path in paths:
        if repo_root(path):
            key = describe(path)[0]
            tally[key] = tally.get(key, 0) + 1
    return tally


class Projects:
    """The projects this machine has seen, kept in the store."""

    SCHEMA = """
    create table if not exists project (
        key text primary key, name text not null, root text not null default '',
        alias_of text not null default '', first_seen real not null, last_seen real not null
    );
    """

    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript(self.SCHEMA)
        self._scanned = set()

    def resolve(self, key: str) -> str:
        """Follow merges: the project `key` now belongs to."""
        for _ in range(8):
            rows = self.store.run("select alias_of from project where key = ?", (key,))
            if not rows or not rows[0]["alias_of"]:
                return key
            key = rows[0]["alias_of"]
        return key

    def see(self, path: str, touch=True) -> str:
        """Register the project at `path` and return its (merged) key."""
        key, name, root = describe(path)
        if not key:
            return ""
        now = time.time()
        self.store.run(
            "insert into project (key, name, root, first_seen, last_seen) values (?, ?, ?, ?, ?) "
            "on conflict(key) do update set root = excluded.root"
            + (", last_seen = excluded.last_seen" if touch else ""), (key, name, root, now, now if touch else 0))
        return self.resolve(key)

    def discover(self, root: str):
        """Learn the repositories next to `root`, so one can be picked up by name alone."""
        parent = os.path.dirname(root)
        if not parent or is_unc(parent) or parent in self._scanned:
            return
        self._scanned.add(parent)
        try:
            names = sorted(os.listdir(parent))[:300]
        except OSError:
            return
        for name in names:
            child = os.path.join(parent, name)
            if os.path.exists(os.path.join(child, ".git")):
                self.see(child, touch=False)

    def named_in(self, text: str) -> str:
        """The known project whose name appears in `text` as a word, if exactly one does."""
        low = (text or "").lower()
        hits = set()
        for row in self.store.run("select key, name from project"):
            name = row["name"].lower()
            if len(name) >= 4 and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", low):
                hits.add(self.resolve(row["key"]))
        return hits.pop() if len(hits) == 1 else ""

    def for_start(self, cwd: str, first_text: str) -> str:
        """The project of a new conversation, before any tool has run."""
        home = self.see(cwd) if cwd else ""
        if home:
            self.discover(describe(cwd)[2])
        tally = vote(paths_in(first_text))
        if tally:
            best = max(sorted(tally), key=lambda k: tally[k])
            root = next(p for p in paths_in(first_text) if repo_root(p) and describe(p)[0] == best)
            return self.see(root)
        return self.named_in(first_text) or home

    def for_turn(self, messages, fallback: str) -> str:
        """The project one exchange worked on: where its tool calls point, else `fallback`."""
        paths = tool_paths(messages)
        tally = vote(paths)
        if not tally:
            return fallback
        best = max(sorted(tally), key=lambda k: tally[k])
        root = next(p for p in paths if repo_root(p) and describe(p)[0] == best)
        return self.see(root)

    def find(self, word: str) -> str:
        """A project key from a key, a name or a path, or ""."""
        rows = self.store.run("select key from project where key = ? or lower(name) = lower(?)", (word, word))
        if len(rows) == 1:
            return self.resolve(rows[0]["key"])
        if os.path.exists(word):
            return self.resolve(describe(word)[0])
        return ""

    def name(self, key: str) -> str:
        rows = self.store.run("select name from project where key = ?", (key,))
        return rows[0]["name"] if rows else key

    def merge(self, source: str, target: str):
        """Fold `source` into `target`: its memory follows, and it resolves to `target` from now on."""
        self.store.run("update project set alias_of = ? where key = ?", (target, source))
        self.store.run("update project set alias_of = ? where alias_of = ?", (target, source))

    def rename(self, key: str, name: str):
        self.store.run("update project set name = ? where key = ?", (name, key))

    def listing(self) -> list:
        return self.store.run("select * from project where alias_of = '' order by last_seen desc, name")
