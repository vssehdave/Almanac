"""Playbooks: recorded ways of doing a task the owner repeats.

A playbook is a folder, `<ALMANAC_HOME>/playbooks/<name>/`, holding `playbook.json` (when it
applies, an ordered decision list, the skills and agents used) and, optionally, `run.py`, a
script that does the whole job with no model in the loop. The proxy matches each new request
against them with plain code and tells Claude which one applies. A script runs through the
`playbook_run` tool only after the owner approved the exact text with `almanac playbook approve`.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from . import request as rq
from .memory import MemoryLayer
from .routing import CONTINUE, _digest, handwritten, person_text, person_turns
from .startup import _insert_before_last

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
WORD = re.compile(r"[a-z0-9]+")
JSON_LIMIT = 20 * 1024
SCRIPT_LIMIT = 200 * 1024
MAX_STEPS = 30
NOTE_LIMIT = 1500
MIN_REQUEST = 12  # a shorter request is never matched
RUN_TIMEOUT = 120  # seconds
RUN_OUTPUT = 20000  # characters kept of each of stdout and stderr
AUTO_TIMEOUT = 60  # seconds an automatic script may run before the request is sent
AUTO_OUTPUT = 3000  # characters of stdout and stderr together that go into the request
AUTO_NOTE_LIMIT = 5000
SOURCES = ("claude", "miner", "owner")
STATUSES = ("draft", "approved")


def root() -> Path:
    return config.home() / "playbooks"


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME.match(name))


def folder(name) -> Path:
    if not valid_name(name):
        raise ValueError(f"a playbook name is lowercase letters, digits and hyphens, 2 to 49 characters: {str(name)[:60]!r}")
    return root() / name


def _text(data, key, low, high, required=True) -> str:
    value = data.get(key, "" if not required else None)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    value = value.strip()
    if len(value) > high or (required and len(value) < low):
        raise ValueError(f"{key} must be {low} to {high} characters")
    return value


def _list(data, key, count, size, required=False) -> list:
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > count or (required and not value):
        raise ValueError(f"{key} must be a list of at most {count} items" + (" and not empty" if required else ""))
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > size:
            raise ValueError(f"every item of {key} must be text of at most {size} characters")
        out.append(item.strip())
    return out


def clean(data) -> dict:
    """The playbook as it is stored, or ValueError. Types and sizes are checked here, on every load and save."""
    if not isinstance(data, dict):
        raise ValueError("a playbook is a JSON object")
    triggers = [t.lower() for t in _list(data, "triggers", 10, 60, required=True)]
    if not all(WORD.search(t) for t in triggers):
        raise ValueError("every trigger needs at least one word")
    args = data.get("args", [])
    if not isinstance(args, list) or len(args) > 10:
        raise ValueError("args must be a list of at most 10 items")
    for arg in args:
        if not isinstance(arg, dict) or not isinstance(arg.get("name"), str) or not 0 < len(arg["name"]) <= 40 \
                or not isinstance(arg.get("help", ""), str) or len(arg.get("help", "")) > 200:
            raise ValueError("each arg is {name, help}, with a name of at most 40 characters and help of at most 200")
    uses, script, auto = data.get("uses", 0), data.get("script", False), data.get("auto", False)
    if type(uses) is not int or uses < 0 or type(script) is not bool or type(auto) is not bool:
        raise ValueError("uses must be a whole number and script and auto true or false")
    status, source = data.get("status", "draft"), data.get("source", "claude")
    if status not in STATUSES or source not in SOURCES:
        raise ValueError(f"status is one of {STATUSES} and source one of {SOURCES}")
    out = {"name": data.get("name"), "title": _text(data, "title", 1, 120), "when": _text(data, "when", 1, 300),
           "triggers": triggers, "project": _text(data, "project", 0, 200, required=False),
           "steps": _list(data, "steps", MAX_STEPS, 400, required=True),
           "skills": _list(data, "skills", 20, 80), "agents": _list(data, "agents", 20, 80),
           "script": script, "args": [{"name": a["name"], "help": a.get("help", "")} for a in args],
           "auto": auto, "status": status, "hash": _text(data, "hash", 0, 64, required=False), "uses": uses,
           "created": _text(data, "created", 0, 32, required=False), "source": source}
    if not valid_name(out["name"]):
        raise ValueError("bad name")
    return out


def _read(path: Path, limit: int):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        return None
    return path.read_bytes()


def load(name):
    """The stored playbook, or None when it is missing or bad. Never raises."""
    try:
        raw = _read(folder(name) / "playbook.json", JSON_LIMIT)
        pb = clean(json.loads(raw.decode("utf-8"))) if raw is not None else None
        if pb is None or pb["name"] != name:
            return None
        if pb["script"] and _read(folder(name) / "run.py", SCRIPT_LIMIT) is None:
            return None
        return pb
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None


def names() -> list:
    try:
        return sorted(p.name for p in root().iterdir() if valid_name(p.name) and p.is_dir() and not p.is_symlink())
    except OSError:
        return []


def listing() -> list:
    return [pb for pb in map(load, names()) if pb]


def script_path(name) -> Path:
    return folder(name) / "run.py"


def script_hash(name) -> str:
    try:
        raw = _read(script_path(name), SCRIPT_LIMIT)
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(raw).hexdigest() if raw is not None else ""


def runnable(pb) -> bool:
    """An approved script whose text still matches the approved hash."""
    return bool(pb["script"] and pb["status"] == "approved" and pb["hash"] and pb["hash"] == script_hash(pb["name"]))


def _write(pb):
    text = json.dumps(pb, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > JSON_LIMIT:
        raise ValueError(f"a playbook's JSON is at most {JSON_LIMIT} bytes")
    folder(pb["name"]).mkdir(parents=True, exist_ok=True)
    (folder(pb["name"]) / "playbook.json").write_bytes(text.encode("utf-8"))


def save(data, script=None):
    """Validate and store a playbook. Returns the stored dict.

    status, hash, auto, uses and created are not taken from the caller: no script means nothing to
    approve, a script is approved only while its text is the one the owner approved, and only
    `almanac playbook approve --auto` makes it run by itself (kept here only while that approval holds).
    """
    if not isinstance(data, dict) or not valid_name(data.get("name")):
        raise ValueError("a playbook needs a valid name")
    name = data["name"]
    old = load(name)
    if script is not None and (not isinstance(script, str) or len(script.encode("utf-8")) > SCRIPT_LIMIT):
        raise ValueError(f"script must be text of at most {SCRIPT_LIMIT} bytes")
    wants = script is not None or (data.get("script") is not False and old is not None and old["script"])
    pb = clean(dict(data, script=bool(wants), status="draft", hash="", auto=False, uses=old["uses"] if old else 0,
                    created=old["created"] if old else time.strftime("%Y-%m-%d")))
    if script is not None:
        pb["script"] = True
        folder(name).mkdir(parents=True, exist_ok=True)
        script_path(name).write_bytes(script.encode("utf-8"))
    elif not pb["script"] and script_path(name).exists():
        script_path(name).unlink()
    if not pb["script"]:
        pb["status"] = "approved"
    elif old and old["status"] == "approved" and old["hash"] and old["hash"] == script_hash(name):
        pb["status"], pb["hash"] = "approved", old["hash"]
        pb["auto"] = old["auto"] and not pb["args"]
    _write(pb)
    return pb


def approve(name, auto=False):
    """Record the hash of run.py as it is now; `auto` also lets the proxy run it. Returns (hash, text of run.py)."""
    pb = load(name)
    if pb is None or not pb["script"]:
        raise ValueError(f"no playbook called {name!r} with a script")
    if auto and pb["args"]:
        raise ValueError(f"{name} takes arguments, and a script that needs inputs cannot run from the words of a request")
    raw = _read(script_path(name), SCRIPT_LIMIT)
    pb["status"], pb["hash"], pb["auto"] = "approved", hashlib.sha256(raw).hexdigest(), bool(auto)
    _write(pb)
    return pb["hash"], raw.decode("utf-8", "replace")


def remove(name) -> bool:
    path = folder(name)
    if not path.is_dir() or path.is_symlink():
        return False
    shutil.rmtree(path)
    return True


class NotRun(Exception):
    """The script is not approved any more; the message is what Claude is told."""


def _execute(name, args, cwd=None, timeout=None) -> tuple:
    """Run an approved script. Returns (head, stdout, stderr, exit code or None on a timeout)."""
    pb = load(name) if valid_name(name) else None
    if pb is None or not pb["script"]:
        raise ValueError(f"no playbook called {name!r} has a script")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("args must be a list of strings")
    if not runnable(pb):
        why = "its script changed after approval" if pb["hash"] else "its script is a draft"
        raise NotRun(f"Not run: {why}. The owner approves it with `almanac playbook approve {name}`. "
                     f"Meanwhile run it yourself with Bash: python \"{script_path(name)}\" --help")
    cwd = cwd if cwd and Path(cwd).is_dir() else None
    try:  # shortcut: the hash is checked just before the start, not held across it; fine for a folder only the owner writes to
        done = subprocess.run([sys.executable, "-I", str(script_path(name)), *args], shell=False, cwd=cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout or RUN_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        out, err = (x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "") for x in (exc.stdout, exc.stderr))
        return f"timed out after {timeout or RUN_TIMEOUT} seconds and was stopped", out, err, None
    return f"exit code {done.returncode}", done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"), done.returncode


def run(name, args, cwd=None, timeout=None) -> str:
    """Run an approved script. Returns the report for Claude; raises ValueError when it must not run."""
    try:
        head, out, err, _ = _execute(name, args, cwd, timeout)
    except NotRun as why:
        return str(why)
    return _report(head, out, err)


def _report(head, out, err) -> str:
    def cap(text):
        return text if len(text) <= RUN_OUTPUT else text[:RUN_OUTPUT] + f"\n[cut: {len(text) - RUN_OUTPUT} more characters]"
    return f"{head}\n--- stdout ---\n{cap(out)}\n--- stderr ---\n{cap(err)}"


# ---- matching ----------------------------------------------------------------------------------

def score(pb, request: str) -> int:
    """Words of the triggers that hit: a trigger hits when every one of its words is in the request."""
    have = set(WORD.findall(request.lower()))
    hits = (WORD.findall(t) for t in pb["triggers"])
    return sum(len(w) for w in hits if w and have.issuperset(w))


def find(request: str, project="", limit=5) -> list:
    """Usable playbooks for this request, best first: most trigger words, then most used, then name."""
    scored = [(score(pb, request), pb) for pb in listing() if pb["project"] in ("", project)]
    scored = [(s, pb) for s, pb in scored if s]
    scored.sort(key=lambda sp: (-sp[0], -sp[1]["uses"], sp[1]["name"]))
    return [pb for _, pb in scored[:limit]]


def _script_line(pb) -> str:
    if runnable(pb):
        names_ = ", ".join(f"<{a['name']}>" for a in pb["args"])
        return f'Script: call the tool `playbook_run` with name "{pb["name"]}" and args [{names_}]\n'
    return f'Script (draft, not yet approved): run `python "{script_path(pb["name"])}" --help` with Bash\n'


def full(pb) -> str:
    """The whole playbook as plain text, for a tool reply: nothing cut, no reminder tags."""
    lines = [f'{pb["name"]}: {pb["title"]} ({pb["status"]}, used {pb["uses"]}x)', pb["when"], "Steps:"]
    lines += [f"{i}. {step}" for i, step in enumerate(pb["steps"], 1)]
    lines += [f'{label}: {", ".join(pb[key])}' for label, key in (("Skills", "skills"), ("Agents", "agents")) if pb[key]]
    return "\n".join(lines + ([_script_line(pb).rstrip()] if pb["script"] else []))


def _step_lines(pb, room) -> str:
    steps = []
    for i, step in enumerate(pb["steps"], 1):
        line = f"{i}. {step[:240]}\n"
        if len(line) > room:
            steps.append("...\n")
            break
        steps.append(line)
        room -= len(line)
    return "".join(steps)


TAG = re.compile(r"<\s*/?\s*system-reminder[^>]*>", re.I)


def _untag(text) -> str:
    """Script output goes into a request: it must not be able to open or close a reminder."""
    while TAG.search(text):
        text = TAG.sub("", text)
    return text


def ran_note(pb, head, out, err) -> str:
    """The reminder for a script Almanac already ran: its result, and the steps to fall back on. At most AUTO_NOTE_LIMIT characters."""
    out, err = out.replace("\r\n", "\n"), err.replace("\r\n", "\n")
    keep_err = min(len(err), AUTO_OUTPUT // 3)
    keep_out = min(len(out), AUTO_OUTPUT - keep_err)
    keep_err = min(len(err), AUTO_OUTPUT - keep_out)

    def cut(text, keep):
        return _untag(text[:keep]) + (f"\n[cut: {len(text) - keep} more characters]" if len(text) > keep else "")

    top = (f'<system-reminder>\nAlmanac has already run the script of playbook "{pb["name"]}" ({pb["title"]}) for this request. '
           f"Do not run it again and do not redo its steps by hand.\nResult: {head}\n--- stdout ---\n{cut(out, keep_out)}\n"
           f"--- stderr ---\n{cut(err, keep_err)}\n")
    tail = ("Use this result to answer the request. If the exit code is not 0 or the script timed out, say so to the owner "
            "and carry on with these steps by hand:\n")
    end = "</system-reminder>"
    text = top + tail + _step_lines(pb, AUTO_NOTE_LIMIT - len(top) - len(tail) - len(end) - 8) + end
    if len(text) > AUTO_NOTE_LIMIT:
        text = text[:AUTO_NOTE_LIMIT - len(end) - 2].rstrip() + "…\n" + end
    return text


def note(pb) -> str:
    """The reminder added to the request, at most NOTE_LIMIT characters."""
    uses = pb["uses"]
    head = (f'<system-reminder>\nAlmanac playbook "{pb["name"]}" matches this request ({pb["title"]}; '
            f'used {uses} time{"" if uses == 1 else "s"}). {pb["when"]}\nSteps:\n')
    extra = []
    lists = [(label, ", ".join(pb[key])) for label, key in (("Skills", "skills"), ("Agents", "agents")) if pb[key]]
    if lists:
        extra.append(" ".join(f"{label}: {text[:300]}." for label, text in lists) + "\n")
    if pb["script"]:
        extra.append(_script_line(pb))
    tail = ("Follow it unless this request differs. If you change the approach or finish a task that will come up again, "
            "record it with `playbook_save`.\n</system-reminder>")
    fixed = head + "".join(extra) + tail
    text = head + _step_lines(pb, NOTE_LIMIT - len(fixed) - 8) + "".join(extra) + tail
    if len(text) > NOTE_LIMIT:  # a very long title or when: keep the closing tag and cut inside
        text = text[:NOTE_LIMIT - len(tail) - 2].rstrip() + "…\n" + tail
    return text


def bump(name):
    pb = load(name)
    if pb:
        pb["uses"] += 1
        _write(pb)
    return pb


class PlaybookLayer:
    """Tell Claude which playbook applies to the newest typed request of the main conversation thread.

    The note is frozen per (lane, turn, digest of the typed text), an empty one included, so every
    later request of the conversation carries the same bytes whatever happens to the playbook.
    """

    @staticmethod
    def _key(ctx, turn, text) -> str:
        return f"playbook:{ctx.lane}:{turn}:{_digest(text)}"

    def transform(self, body, ctx):
        cfg = ctx.cfg.get("playbooks") or {}
        if not (cfg.get("enabled") and cfg.get("inject")) or ctx.store is None or ctx.kind != "messages":
            return
        if not MemoryLayer._main_thread(body, ctx):
            return
        try:
            self._apply(body, ctx)
        except Exception:  # fail open: a bad playbook folder must never block a turn
            return

    def _apply(self, body, ctx):
        messages, orig = body["messages"], (ctx.orig or body)["messages"]
        seen, mine = person_turns(orig), person_turns(messages)
        if not seen:
            return
        last = len(seen) - 1
        newest = person_text(orig[seen[last]])
        key = self._key(ctx, last, newest)
        text = ctx.store.get(key)
        if text is None:
            low = handwritten(newest)
            if len(low) < MIN_REQUEST or CONTINUE.match(low):
                text = ""  # decided by the words alone, so nothing is stored
            else:
                text = ctx.store.frozen(key, lambda: self._first(ctx, low, key))
        # Earlier turns got their note when they were new: put it back, byte for byte, so the cached prefix holds.
        # When compaction changed the history the turns no longer line up, and there is nothing to put back.
        if len(mine) == len(seen):
            for turn in range(last):
                old = ctx.store.get(self._key(ctx, turn, person_text(orig[seen[turn]])))
                if old:
                    _insert_before_last(messages[mine[turn]], old)
        for turn in range(last):  # a reminder that went into an earlier turn's tool loop stays where it was put
            earlier = self._key(ctx, turn, person_text(orig[seen[turn]]))
            self._place(messages, orig, ctx.store.get("playbookskip" + earlier[len("playbook"):]))
        if text:
            _insert_before_last(messages[mine[-1]], text)
            ctx.stats.setdefault("playbook", {})["chars"] = len(text)
            self._place(messages, orig, self._skipped(ctx, orig, seen[last], key))

    @staticmethod
    def _place(messages, orig, kept):
        """Put a stored reminder back after the tool results of the message it first went into, or nowhere
        when the history no longer lines up (compacted or rewound)."""
        if not kept or len(messages) != len(orig) or not 0 <= kept["at"] < len(messages):
            return
        msg = messages[kept["at"]]
        if isinstance(msg, dict) and rq.is_tool_result(msg) and isinstance(msg.get("content"), list):
            msg["content"].append({"type": "text", "text": kept["text"]})

    @staticmethod
    def _skipped(ctx, orig, start, key):
        """The reminder for this turn: decided once, when the model has made 2 tool calls without taking an offered script."""
        suffix = key[len("playbook"):]
        kept = ctx.store.get("playbookskip" + suffix)
        name = ctx.store.get("playbookoffer" + suffix)
        if kept or not name:
            return kept
        calls = [b.get("name") or "" for m in orig[start + 1:] if isinstance(m, dict) and m.get("role") == "assistant"
                 for b in rq.blocks(m) if b.get("type") == "tool_use"]
        calls = [c for c in calls if c != "ToolSearch"]
        if len(calls) < 2 or any(c.endswith("playbook_run") for c in calls) or not rq.is_tool_result(orig[-1]):
            return None
        pb = load(name)
        if pb is None or not runnable(pb):
            return None
        args = ", ".join(f"<{a['name']}>" for a in pb["args"])
        text = (f'<system-reminder>\nAlmanac playbook "{name}" has an approved script that does this job. Call the tool '
                f'`playbook_run` with name "{name}" and args [{args}] now instead of continuing by hand, '
                "unless this request really differs from the playbook.\n</system-reminder>")
        return ctx.store.frozen("playbookskip" + suffix, lambda: {"at": len(orig) - 1, "text": text})

    @staticmethod
    def _first(ctx, low, key) -> str:
        best = find(low, ctx.project, limit=1)
        if not best:
            return ""
        done = bump(best[0]["name"]) or best[0]
        if (ctx.cfg.get("playbooks") or {}).get("auto_run") and done["auto"] and not done["args"] and runnable(done) \
                and ctx.cwd and Path(ctx.cwd).is_dir():
            try:  # holds the store's lock for at most AUTO_TIMEOUT seconds, once per typed turn
                head, out, err, code = _execute(done["name"], [], ctx.cwd, AUTO_TIMEOUT)
                text = ran_note(done, head, out, err)
            except Exception:  # a script that cannot run leaves the ordinary note
                pass
            else:
                ctx.stats["playbook"] = {"ran": done["name"], **({"exit": code} if code is not None else {"timeout": True})}
                return text
        if runnable(done):  # offered, not run: remember it, so a skipped offer can be called back
            ctx.store.put("playbookoffer" + key[len("playbook"):], done["name"])
        return note(done)
