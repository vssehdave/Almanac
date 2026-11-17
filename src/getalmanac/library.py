"""Prompt library: how each solved prompt was worked, written by code and kept current.

When an exchange of the main conversation ends after at least `library.min_tools` tool calls, one entry is
written from it: the typed request, the route (the miner's step signatures, repeats collapsed), the files
touched, the fixes (a failed step and what recovered it) and how it ended. No model is called. Text is
redacted and tag-stripped like the rest of memory. When a later entry in the same project asks for the same
thing by nearly the same route, it replaces the older one as the current entry, which is kept and linked
under `superseded_by`; the newer one lists what changed. Rows go with their conversation (forget, prune).
"""
import json
import re
import time

from . import mine
from . import request as rq
from .match import tokens
from .memory import _first_line, clean, exchange
from .status import rule

SCHEMA = """
create table if not exists library (
    id integer primary key, convo integer not null, seq integer not null, project text not null,
    prompt text not null, route text not null default '[]', skills text not null default '[]',
    agents text not null default '[]', files text not null default '[]', fixes text not null default '[]',
    outcome text not null default '', changes text not null default '[]', uses integer not null default 0,
    created real not null, updated real not null, superseded_by integer not null default 0
);
create unique index if not exists library_exchange on library (convo, seq);
create index if not exists library_project on library (project, superseded_by, updated);
"""
PROMPT_CHARS = 600
MAX_FILES = 20
MAX_FIXES = 8
MAX_ROUTE = 60
PROMPT_SAME = 0.6  # word-set Jaccard of two prompts that count as the same task
ROUTE_SAME = 0.5  # step-set Jaccard of two routes that count as the same task
COMPARE = 500  # most current entries of a project one new entry is compared with
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_EXIT = re.compile(r"(?i)\bexit(?:ed)?(?: with)?[ _](?:code|status)[: =]*[1-9]\d*")
_ERROR = re.compile(r"(?i)error|fail|exception|denied|not found|rejected|cannot|no module")
_EXIT_ONLY = re.compile(r"(?i)^exit code[: ]*\d+$")


def _short(path) -> str:
    return "/".join(str(path).replace("\\", "/").split("/")[-2:])


def _path(call) -> str:
    path = call["input"].get("file_path") or call["input"].get("notebook_path") or ""
    return path if isinstance(path, str) else ""


def _label(call) -> str:
    """A call as one readable step: edits name their file, commands use the miner's signature."""
    if call["name"] in EDITS and _path(call):
        return f"{call['name']} {_short(_path(call))}"
    return " + ".join(call["steps"]) or str(call["name"])


def _error_line(text) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not _EXIT_ONLY.match(line)]
    return clean(next((line for line in lines if _ERROR.search(line)), lines[0] if lines else ""))[:120]


def _calls(group) -> list:
    """The tool calls of an exchange in order, each with the steps it made and whether its result was a failure."""
    calls, by_id = [], {}
    for msg in group[1:]:
        for block in rq.blocks(msg):
            kind = block.get("type")
            if kind == "tool_use" and msg.get("role") == "assistant":
                args = block.get("input")
                call = {"name": block.get("name"), "steps": mine.steps(block), "input": args if isinstance(args, dict) else {},
                        "bad": False, "err": ""}
                calls.append(call)
                by_id[block.get("id")] = call
            elif kind == "tool_result" and block.get("tool_use_id") in by_id:
                call, inner = by_id[block["tool_use_id"]], block.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(x.get("text") or "" for x in inner if isinstance(x, dict) and x.get("type") == "text")
                text = str(inner or "")
                if block.get("is_error") or (call["name"] == "Bash" and _EXIT.search(text)):
                    call["bad"], call["err"] = True, _error_line(text)
    return calls


def _collapse(items) -> list:
    out = []
    for item in items:
        if item and (not out or out[-1] != item):
            out.append(item)
    return out


def _fixes(calls) -> list:
    """Each failed call that a later call of the same command succeeded after: `<step> failed (<why>) -> <steps> -> <step> passed`."""
    found, i = [], 0
    while i < len(calls):
        if calls[i]["bad"]:
            label = _label(calls[i])
            for j in range(i + 1, len(calls)):
                if _label(calls[j]) == label and not calls[j]["bad"]:
                    between = _collapse(_label(c) for c in calls[i + 1:j] if _label(c) != "Bash")[:6]
                    found.append(f"{label} failed ({calls[i]['err']}) -> " + " -> ".join(between + [label + " passed"]))
                    i = j
                    break
        i += 1
    return [clean(f) for f in dict.fromkeys(found)][:MAX_FIXES]


def _jaccard(a, b) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _fix_key(fix) -> str:
    return fix.split(" failed (")[0]


def _merge(store, new_id, project, prompt, route, fixes):
    """Make the new entry the current one when it is the same task as a current entry of the project."""
    words, steps, best = set(tokens(prompt)), set(route), None
    for old in store.run("select id, prompt, route, fixes, uses from library where project = ? and superseded_by = 0 "
                         "and id != ? order by updated desc limit ?", (project, new_id, COMPARE)):
        old_route = json.loads(old["route"])
        same, similar = _jaccard(words, set(tokens(old["prompt"]))), _jaccard(steps, set(old_route))
        if same >= PROMPT_SAME and similar >= ROUTE_SAME and (best is None or same + similar > best[0]):
            best = (same + similar, old, old_route)
    if not best:
        return
    _, old, old_route = best
    seen = {_fix_key(f) for f in json.loads(old["fixes"])}
    changes = ([f"added step: {s}" for s in dict.fromkeys(route) if s not in set(old_route)]
               + [f"dropped step: {s}" for s in dict.fromkeys(old_route) if s not in steps]
               + [f"new fix: {f}" for f in fixes if _fix_key(f) not in seen])
    store.run("update library set superseded_by = ? where id = ?", (new_id, old["id"]))
    store.run("update library set changes = ?, uses = ? where id = ?", (json.dumps(changes, ensure_ascii=False), old["uses"], new_id))


def record(memory, lane, group, seq, min_tools, now=None) -> int:
    """Write the entry for `group`, the exchange just finished as number `seq` of conversation `lane`. Returns 1 when written."""
    calls = _calls(group)
    if len(calls) < min_tools:
        return 0
    store = memory.store
    leaf = store.run("select n.convo, n.project from node n join convo c on c.id = n.convo "
                     "where c.lane = ? and n.kind = 'leaf' and n.seq = ?", (lane, seq))
    if not leaf:
        return 0
    steps = [s for call in calls for s in call["steps"]]
    route = _collapse(steps)[:MAX_ROUTE]
    files = []
    for call in calls:
        if call["name"] in ("Read", *EDITS) and _path(call) and _short(_path(call)) not in files:
            files.append(_short(_path(call)))
    prompt = " ".join(clean(rq.human_text(group[0])).split())[:PROMPT_CHARS]
    answer = _first_line(exchange(group)["answer"], 160)
    outcome = clean(f"{rule(group)}: {answer}" if answer else rule(group))
    now = now or time.time()
    fixes = _fixes(calls)
    row = (leaf[0]["convo"], seq, leaf[0]["project"], prompt, json.dumps(route, ensure_ascii=False),
           json.dumps(list(dict.fromkeys(s[7:] for s in steps if s.startswith("Skill: ")))),
           json.dumps(list(dict.fromkeys(s[7:] for s in steps if s.startswith("Agent: ")))),
           json.dumps([clean(f) for f in files[:MAX_FILES]], ensure_ascii=False),
           json.dumps(fixes, ensure_ascii=False), outcome, now, now)
    with store.lock:
        cursor = store.db.execute("insert or ignore into library (convo, seq, project, prompt, route, skills, agents, files, fixes, "
                                  "outcome, created, updated) values (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        if not cursor.rowcount:  # this exchange already has its entry
            return 0
        _merge(store, cursor.lastrowid, row[2], prompt, route, fixes)
    return 1


def observe(memory, ctx, messages, reply):
    """Called by the memory layer after the exchange was indexed (main thread only). Never raises."""
    try:
        turns = rq.human_turns(messages)
        if not turns or reply.get("stop_reason") == "tool_use":  # nothing finished yet
            return 0
        group = messages[turns[-1]:] + [{"role": "assistant", "content": reply["content"]}]
        return record(memory, ctx.lane, group, len(turns) - 1, (ctx.cfg.get("library") or {}).get("min_tools", 5))
    except Exception as exc:  # a failed record must not cost the rest of the worker's jobs
        try:
            ctx.proxy.log.write({"ts": time.time(), "kind": "error", "where": "library", "error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:
            pass
        return 0


def drop(store, convo_ids):
    """Delete the entries of deleted conversations. An older entry they replaced is current again."""
    for convo_id in convo_ids:
        for entry in [r["id"] for r in store.run("select id from library where convo = ?", (convo_id,))]:
            store.run("update library set superseded_by = 0 where superseded_by = ?", (entry,))
            store.run("delete from library where id = ?", (entry,))
