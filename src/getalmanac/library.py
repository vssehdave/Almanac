"""Prompt library: how each solved prompt was worked, written by code and kept current.

When an exchange ends after at least one tool call, one entry is written from it (searches and lists show
it once it has `library.min_tools` calls): the typed request, the route (the miner's step signatures, repeats
collapsed), the tools, skills and agents it used, the files touched, the fixes (a failed step and what
recovered it) and how it ended. No model is called. Text is redacted and tag-stripped like the rest of
memory. When a later entry in the same project asks for the same thing by nearly the same route, it replaces
the older one as the current entry, which is kept and linked under `superseded_by`; the newer one lists what
changed. Rows go with their conversation (forget, prune). The same rows say which tools, skills and agents a
project keeps using (`recently_used`), sub-agent exchanges included; those are shown nowhere else.
Current entries are searchable (an FTS5 table over prompt and route), and `note` renders the one that best
matches a typed prompt as the short reminder the playbook layer adds to the request.
"""
import hashlib
import json
import re
import sqlite3
import time

from . import config, mine
from . import request as rq
from .match import STOP, tokens
from .memory import QUOTED, _first_line, clean, exchange, redact
from .project import Projects
from .routing import handwritten
from .status import _ends_on_question

SCHEMA = """
create table if not exists library (
    id integer primary key, convo integer not null, seq integer not null, project text not null,
    prompt text not null, route text not null default '[]', skills text not null default '[]',
    agents text not null default '[]', files text not null default '[]', fixes text not null default '[]',
    outcome text not null default '', changes text not null default '[]', uses integer not null default 0,
    tools text not null default '[]', calls integer not null default 0,
    created real not null, updated real not null, superseded_by integer not null default 0
);
create unique index if not exists library_exchange on library (convo, seq);
create index if not exists library_project on library (project, superseded_by, updated);
create virtual table if not exists library_fts using fts5(prompt, route, tokenize='porter unicode61');
"""
MIN_TOOLS = 5  # fewer tool calls than this keeps an exchange out of searches and lists (config library.min_tools)
PROMPT_CHARS = 600
MAX_FILES = 20
MAX_FIXES = 8
MAX_ROUTE = 60
PROMPT_SAME = 0.6  # word-set Jaccard of two prompts that count as the same task
ROUTE_SAME = 0.5  # step-set Jaccard of two routes that count as the same task
COMPARE = 500  # most current entries of a project one new entry is compared with
THRESHOLD = 0.6  # share of a stored prompt's words found in the typed text for the note to be shown (see README)
MIN_SHARED = 3  # words they must share, whatever the share: two short prompts alike in one word are not the same task
MIN_SHARED_LONG = 5  # ... when the stored prompt has more than LONG words
LONG = 30
MIN_USES = 2  # a route shown this often is a habit and becomes a miner candidate (see `candidates`)
CANDIDATES = 50  # most entries the full-text search hands to the scorer
NOTE_CHARS = 800
CLOSE = "</system-reminder>"
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_EXIT = re.compile(r"(?i)exit(?:ed)?(?: with)?[ _](?:code|status)[: =]*[1-9]\d*")  # matched at the start of a result only
_ERROR = re.compile(r"(?i)error|fail|exception|denied|not found|rejected|cannot|no module")
_EXIT_ONLY = re.compile(r"(?i)^exit code[: ]*\d+$")


def min_tools(cfg=None) -> int:
    """The configured minimum of tool calls for an entry to be shown."""
    return ((cfg if cfg is not None else config.load()).get("library") or {}).get("min_tools", MIN_TOOLS)


def _carry_usage(store):
    """A store written when usage had a table of its own: each lane's names move to one row (prompt '', so no search or
    list shows it) of the lane's conversation, or of a number of its own below zero when it has none; then the table goes."""
    if not store.run("select 1 from sqlite_master where name = 'used'"):
        return
    store.run("begin immediate")
    try:
        if store.run("select 1 from sqlite_master where name = 'used'"):  # another process may have moved it first
            store.run("insert into library (convo, seq, project, prompt, tools, skills, created, updated) "
                      "select coalesce((select id from convo where lane = u.lane), -min(u.rowid)), -1, max(u.project), '', "
                      "json_group_array(u.name) filter (where u.kind = 'tool'), json_group_array(u.name) filter (where u.kind = 'skill'), "
                      "max(u.ts), max(u.ts) from used u group by u.lane")
            store.run("drop table used")
        store.run("commit")
    except BaseException:
        store.run("rollback")
        raise


def setup(store):
    """Create the tables. The search table of a store written before it existed is filled from the current entries."""
    with store.lock:
        store.db.executescript(SCHEMA)
        have = {r["name"] for r in store.run("pragma table_info(library)")}
        for column, ddl in (("tools", "text not null default '[]'"), ("calls", "integer not null default 0")):
            if column not in have:  # a store written before every exchange was kept
                store.run(f"alter table library add column {column} {ddl}")
                if column == "calls":  # the entries it holds were written at the old minimum: they stay shown
                    store.run("update library set calls = max(5, json_array_length(route))")
        if not store.run("select 1 from library_fts limit 1") and store.run("select 1 from library where superseded_by = 0 and prompt != '' limit 1"):
            store.run("insert into library_fts (rowid, prompt, route) select id, prompt, route from library where superseded_by = 0 and prompt != ''")
        _carry_usage(store)


def _sync(store, ids):
    """Only current entries are searchable: bring the search rows of `ids` in line with the table."""
    for entry in ids:
        store.run("delete from library_fts where rowid = ?", (entry,))
        store.run("insert into library_fts (rowid, prompt, route) select id, prompt, route from library where id = ? and superseded_by = 0 and prompt != ''", (entry,))


def _short(path) -> str:
    return "/".join(str(path).replace("\\", "/").split("/")[-2:])


def _path(call) -> str:
    path = call["input"].get("file_path") or call["input"].get("notebook_path") or ""
    return path if isinstance(path, str) else ""


def _label(call) -> str:
    """A call as one readable step: edits name their file, commands use the miner's signature."""
    if call["name"] in EDITS and _path(call):
        return clean(f"{call['name']} {_short(_path(call))}")
    return " + ".join(call["steps"]) or call["name"]


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
                call = {"name": clean(str(block.get("name")))[:60], "steps": [clean(s) for s in mine.steps(block)], "input": args if isinstance(args, dict) else {},
                        "bad": False, "err": ""}
                calls.append(call)
                by_id[str(block.get("id"))] = call
            elif kind == "tool_result" and isinstance(block.get("tool_use_id"), str) and block["tool_use_id"] in by_id:
                call, inner = by_id[block["tool_use_id"]], block.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(x["text"] for x in inner if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
                text = inner if isinstance(inner, str) else ""
                if block.get("is_error") or (call["name"] == "Bash" and _EXIT.match(text.lstrip())):
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
        label = _label(calls[i])
        if calls[i]["bad"] and label != "Bash":  # a bare "Bash" is a read-only filter such as grep: failing is its answer
            for j in range(i + 1, len(calls)):
                if _label(calls[j]) == label and not calls[j]["bad"]:
                    between = _collapse(_label(c) for c in calls[i + 1:j] if _label(c) != "Bash")[:6]
                    found.append(f"{label} failed ({calls[i]['err']}) -> " + " -> ".join(between + [label + " passed"]))
                    i = j
                    break
        i += 1
    return list(dict.fromkeys(found))[:MAX_FIXES]  # every piece is already clean: cleaning the joined line lets one pattern eat the rest


def _jaccard(a, b) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _fix_key(fix) -> str:
    return fix.split(" failed (")[0]


def _merge(store, new_id, project, prompt, route, fixes, min_tools=MIN_TOOLS):
    """Make the new entry the current one when it is the same task as a current entry of the project."""
    if not store.run("select 1 from library l where l.id = ? and " + SHOWN, (new_id, min_tools)):  # an entry no search shows replaces nothing
        return
    words, steps, best = set(tokens(prompt)), set(route), None
    for old in store.run("select l.id, l.prompt, l.route, l.fixes, l.uses from library l where l.project = ? and l.superseded_by = 0 "
                         "and l.id != ? and " + SHOWN + " order by l.updated desc limit ?", (project, new_id, min_tools, COMPARE)):
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
    _sync(store, [old["id"]])


def record(memory, lane, group, seq, min_tools=MIN_TOOLS, now=None) -> int:
    """Write the entry for `group`, the exchange just finished as number `seq` of conversation `lane`. Returns 1 when written."""
    store = memory.store
    leaf = store.run("select n.convo, n.project from node n join convo c on c.id = n.convo "
                     "where c.lane = ? and n.kind = 'leaf' and n.seq = ?", (lane, seq))
    if not leaf:
        return 0
    prompt = " ".join(clean(rq.human_text(group[0])).split())[:PROMPT_CHARS]
    with store.lock:  # the conversation was rewound or the request edited: the entry at this place is another task's
        _delete(store, [r["id"] for r in store.run("select id from library where convo = ? and seq = ? and prompt != ?",
                                                   (leaf[0]["convo"], seq, prompt))])
    calls = _calls(group)
    if not calls:
        return 0
    steps = [s for call in calls for s in call["steps"]]
    route = _collapse(steps)[:MAX_ROUTE]
    files = []
    for call in calls:
        if call["name"] in ("Read", *EDITS) and _path(call) and _short(_path(call)) not in files:
            files.append(_short(_path(call)))
    answer = _first_line(exchange(group)["answer"], 160)
    ended = "open" if _ends_on_question(group) else "done"  # how the work ended; a rule in the prompt is no outcome
    outcome = clean(f"{ended}: {answer}" if answer else ended)
    now = now or time.time()
    fixes = _fixes(calls)
    row = (leaf[0]["convo"], seq, leaf[0]["project"], prompt, json.dumps(route, ensure_ascii=False),
           json.dumps(list(dict.fromkeys(s[7:] for s in steps if s.startswith("Skill: ")))),
           json.dumps(list(dict.fromkeys(s[7:] for s in steps if s.startswith("Agent: ")))),
           json.dumps([clean(f) for f in files[:MAX_FILES]], ensure_ascii=False),
           json.dumps(fixes, ensure_ascii=False), outcome, now, now, json.dumps(list(dict.fromkeys(c["name"] for c in calls))), len(calls))
    with store.lock:
        cursor = store.db.execute("insert or ignore into library (convo, seq, project, prompt, route, skills, agents, files, fixes, "
                                  "outcome, created, updated, tools, calls) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        if not cursor.rowcount:  # this exchange already has its entry
            return 0
        _sync(store, [cursor.lastrowid])
        _merge(store, cursor.lastrowid, row[2], prompt, route, fixes, min_tools)
    return 1


def observe(memory, ctx, messages, reply):
    """Called by the memory layer after the exchange was indexed. Never raises."""
    try:
        turns = rq.human_turns(messages)
        if not turns or reply.get("stop_reason") == "tool_use":  # nothing finished yet
            return 0
        group = messages[turns[-1]:] + [{"role": "assistant", "content": reply["content"]}]
        written = record(memory, ctx.lane, group, len(turns) - 1, min_tools(ctx.cfg))
        if written:  # `almanac report` counts these; the lane keeps the record shaped like the request records
            ctx.proxy.log.write({"ts": time.time(), "kind": "library", "lane": ctx.lane, "written": written})
        return written
    except Exception as exc:  # a failed record must not cost the rest of the worker's jobs
        try:
            ctx.proxy.log.write({"ts": time.time(), "kind": "error", "where": "library", "error": redact(f"{type(exc).__name__}: {exc}")[:300]})
        except Exception:
            pass
        return 0


def _delete(store, ids):
    """Delete entries. What each one superseded now points at what it was superseded by (or is current again)."""
    for entry in ids:
        older = [r["id"] for r in store.run("select id from library where superseded_by = ?", (entry,))]
        store.run("update library set superseded_by = coalesce((select superseded_by from library where id = ?), 0) "
                  "where superseded_by = ?", (entry, entry))
        store.run("delete from library where id = ?", (entry,))
        _sync(store, [entry, *older])


def forget(store, entry) -> int:
    """Delete an entry and every older entry it replaced, so forgetting a task does not bring its past version back.
    Returns the ids deleted, the entry first (empty when there is no such entry)."""
    chain, todo = [], [entry]
    while todo:
        found = todo.pop()
        if found in chain or not store.run("select 1 from library where id = ?", (found,)):
            continue
        chain.append(found)
        todo += [r["id"] for r in store.run("select id from library where superseded_by = ?", (found,))]
    _delete(store, chain)
    return chain


def drop(store, convo_ids):
    """Delete the entries of deleted conversations."""
    for convo_id in convo_ids:
        _delete(store, [r["id"] for r in store.run("select id from library where convo = ?", (convo_id,))])


def drop_from(store, convo_id, seq):
    """Delete a conversation's entries from exchange `seq` on: it was rewound and those exchanges are gone."""
    _delete(store, [r["id"] for r in store.run("select id from library where convo = ? and seq >= ?", (convo_id, seq))])


# ---- finding and showing ---------------------------------------------------------------------------------

# what a search or a list shows: enough tool calls, not a usage-only row, and not a sub-agent's exchange
SHOWN = "l.calls >= ? and l.prompt != '' and not exists (select 1 from convo c where c.id = l.convo and c.parent != 0)"
_SQL = ("select l.*, n.id node from library_fts f join library l on l.id = f.rowid "
        "join node n on n.convo = l.convo and n.kind = 'leaf' and n.seq = l.seq "  # an entry whose exchange is gone cannot be read back: skipped
        "where library_fts match ? and l.superseded_by = 0 and " + SHOWN)


def _score(query, prompt) -> float:
    """The share of the stored prompt's words the typed text has. Only the part the person typed counts on the stored side
    (it may hold pasted text), and the typed text's own length does not matter: a long prompt typed again, or with a new
    tail, is not penalised for being long."""
    words = set(tokens(handwritten(prompt)))
    shared = len(query & words)
    return round(shared / len(words), 3) if words and shared >= (MIN_SHARED_LONG if len(words) > LONG else MIN_SHARED) else 0.0


def search(store, text, project="", limit=3, floor=0.0, min_tools=MIN_TOOLS) -> list:
    """Current entries that match `text`, as (score, entry) best first: those of `project` before any other project's,
    then by score, then the newer. The score is the word overlap of the text with the entry's request. Deterministic."""
    query = set(tokens(text))
    words = list(dict.fromkeys(w for w in re.findall(r"[a-z0-9_]{2,}", text.lower()) if w not in STOP))[:24]
    if not query or not words:
        return []
    match, found = " OR ".join(f'"{w}"' for w in words), {}
    try:
        for scope in ([project] if project else []) + [None]:
            rows = store.run(_SQL + (" and l.project = ?" if scope else "") + " order by bm25(library_fts), l.id limit ?",
                             (match, min_tools, *([scope] if scope else []), CANDIDATES))
            found.update({r["id"]: dict(r) for r in rows})
    except sqlite3.OperationalError:  # a store opened without the library tables
        return []
    scored = [(_score(query, row["prompt"]), row) for row in found.values()]
    scored = [(score, row) for score, row in scored if score > 0 and score >= floor]
    scored.sort(key=lambda p: (p[1]["project"] != project, -p[0], -p[1]["id"]))
    return scored[:limit]


def find(store, text, project="", floor=0.0, min_tools=MIN_TOOLS):
    """The best current entry for `text` as (entry, score): in `project` first, then in any project. None when nothing scores."""
    best = search(store, text, project, 1, floor, min_tools)
    return (best[0][1], best[0][0]) if best else None


def recently_used(store, kind, project, convos=30, agent_type=None) -> dict:
    """name -> how many of the project's last `convos` conversations used it. `kind` is "tool", "skill" or "agent".
    Main conversations count for the main thread; with `agent_type`, only that type's sub-agent conversations count."""
    column = {"tool": "tools", "skill": "skills", "agent": "agents"}[kind]
    scope = ("exists (select 1 from convo c where c.id = l.convo and c.parent != 0 and c.agent_type = ?)" if agent_type is not None
             else "not exists (select 1 from convo c where c.id = l.convo and c.parent != 0)")
    extra = (agent_type,) if agent_type is not None else ()
    rows = store.run(f"select j.value name, count(distinct l.convo) n from library l, json_each(l.{column}) j where l.project = ? and l.convo in "
                     f"(select l.convo from library l where l.project = ? and {scope} group by l.convo order by max(l.updated) desc limit ?) "
                     "group by j.value", (project, project, *extra, convos))
    return {r["name"]: r["n"] for r in rows}


def bump(store, entry) -> int:
    """Count one more time the entry was shown. Returns its uses."""
    store.run("update library set uses = uses + 1 where id = ?", (entry,))
    return store.run("select uses from library where id = ?", (entry,))[0]["uses"]


def _piece(value, size=None) -> str:
    """One stored piece of text on one line, with no reminder tag whatever was stored."""
    value = " ".join(clean(str(value)).split())
    return value[:size].rstrip() + "…" if size and len(value) > size else value


def _list(value) -> list:
    try:
        items = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [i for i in items if isinstance(i, str)] if isinstance(items, list) else []


def _fit(line, room) -> str:
    """`line` cut to `room` characters (with its newline), or "" when too little room is left to say anything."""
    if room < 24:
        return ""
    return line if len(line) + 1 <= room else line[:room - 2].rstrip() + "…"


def lines(entry, limit) -> list:
    """The body of a note: when it was, the route, fixes and files, cut so the lines together stay under `limit`
    characters (each is cut, the route first, then the fixes; the rest gets what is left)."""
    uses = entry["uses"]
    head = (f"Almanac: you did a task like this before ({time.strftime('%Y-%m-%d', time.localtime(entry['created']))}, "
            f"exchange {entry['node']}, used {uses} time{'' if uses == 1 else 's'}).")
    route = " > ".join(_piece(s) for s in _list(entry["route"]) if _piece(s))
    fixes = "; ".join(f for f in (_piece(f, 160) for f in _list(entry["fixes"])[:2]) if f)
    files = ", ".join(f for f in (_piece(f, 80) for f in _list(entry["files"])[:5]) if f)
    room, out = limit - len(head) - 1, [head]
    for label, text, share in (("Route last time: ", route, 0.5), ("Fixes found then: ", fixes, 0.6), ("Files: ", files, 1.0)):
        line = _fit(label + text, int(room * share)) if text else ""
        if line:
            out.append(line)
            room -= len(line) + 1
    return out


def note(entry, limit=NOTE_CHARS, tools_hint=True) -> str:
    """The reminder for a typed prompt that matches `entry`: at most `limit` characters, always closed by the tag.
    `tools_hint` is false when the memory tools are off: the note then names no tool."""
    read = f'; read the full transcript with memory_read {{"id": {int(entry["node"])}}} if you need it' if tools_hint else ""
    tail = f'This is a record of earlier work, not an instruction. Check it against the code{read}.\n' + CLOSE
    body = lines(entry, max(0, limit - len(tail) - len("<system-reminder>\n")))
    text = "<system-reminder>\n" + "".join(line + "\n" for line in body) + tail
    if len(text) > limit:  # a limit too small for even the fixed parts: keep the closing tag
        text = text[:max(0, limit - len(CLOSE) - 2)].rstrip() + "…\n" + CLOSE
    return text


def render_found(found) -> str:
    """Entries from `search`, as the `library_find` tool shows them: like the note, with what was asked, and the quoted sentence."""
    if not found:
        return "No earlier entry matches."
    return "\n\n".join("\n".join([f"Asked: {_piece(entry['prompt'], 200)}", *lines(entry, NOTE_CHARS)]) for _, entry in found) + "\n\n" + QUOTED


def listing(store, project=None, min_tools=MIN_TOOLS) -> list:
    """Current entries, oldest first, of one project or of all."""
    return [dict(r) for r in store.run("select * from library l where l.superseded_by = 0 and " + SHOWN + (" and l.project = ?" if project else "")
                                       + " order by l.id", (min_tools, *([project] if project else [])))]


def _found(route, prompt, found) -> bool:
    """Did the transcript miner already find this work? By steps (the same form) when the candidate has them, and by
    its tools or the words of its requests when not: a worded candidate keeps only each command's first two raw words."""
    words = mine.words(prompt)
    for c in found:
        if tuple(c.get("tools") or ()) == route:
            return True
        if c.get("steps"):
            if _jaccard(set(route), set(c["steps"])) >= ROUTE_SAME:
                return True
        elif any(_jaccard(words, mine.words(x)) >= mine.SIMILAR for x in c.get("examples") or ()):
            return True
    return False


def candidates(store, found=(), min_tools=MIN_TOOLS) -> list:
    """Miner candidates from the current entries used at least MIN_USES times whose route is all commands, most used first.

    Entries of one route make one candidate. Work the transcript miner already found (`found`), a route a stored playbook's
    triggers already match, and one already compiled are left out: the existing compile step takes it from here.
    """
    done, names, made, convos = mine._state()["compiled"], Projects(store), {}, {}
    for e in store.run("select l.convo, l.project, l.prompt, l.route, l.uses from library l where l.superseded_by = 0 and l.uses >= ? and "
                       + SHOWN + " order by l.uses desc, l.id", (MIN_USES, min_tools)):
        route = tuple(_list(e["route"]))
        if not mine.mechanical(route) or _found(route, e["prompt"], found) or mine._matched([{"text": e["prompt"]}]):
            continue
        c = made.setdefault(route, {"id": hashlib.sha256(("library:" + ",".join(route)).encode("utf-8")).hexdigest()[:12], "count": 0,
                                    "sessions": 0, "projects": [], "tokens": 0, "examples": [], "tools": list(route),
                                    "skills": [], "agents": [], "kind": "library"})
        convos.setdefault(route, set()).add(e["convo"])
        c["count"] += e["uses"]
        name = names.name(e["project"])
        c["projects"] += [name] * (name not in c["projects"] and len(c["projects"]) < 5)
        c["examples"] += [e["prompt"]] * (len(c["examples"]) < 3)
    for route, c in made.items():
        c["sessions"] = len(convos[route])
    return [c for c in made.values() if c["id"] not in done]


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _describe(entry, indent="") -> list:
    out = [f"Asked: {_piece(entry['prompt'])}", f"Route: {' > '.join(_list(entry['route'])) or 'none'}"]
    for label, key in (("Fixes", "fixes"), ("Changes since the earlier entry", "changes")):
        if _list(entry[key]):
            out += [f"{label}:"] + [f"  - {_piece(item)}" for item in _list(entry[key])]
    out += [f"Files: {', '.join(_list(entry['files'])) or 'none'}", f"Outcome: {_piece(entry['outcome'])}"]
    return [indent + line for line in out]


def show(memory, entry_id):
    """One entry in full with its history (the entries it replaced, newest first), as text; None when there is no such entry."""
    store = memory.store
    rows = store.run("select * from library where id = ?", (entry_id,))
    if not rows:
        return None
    entry, older, seen, todo = dict(rows[0]), [], {entry_id}, [entry_id]
    while todo:
        for row in store.run("select * from library where superseded_by = ? order by id desc", (todo.pop(),)):
            if row["id"] not in seen:
                seen.add(row["id"])
                older.append(dict(row))
                todo.append(row["id"])

    def head(row):
        return (f"Entry {row['id']} (project {memory.projects.name(row['project'])}, {_day(row['created'])}, used {row['uses']} "
                f"time{'' if row['uses'] == 1 else 's'}, exchange {row['seq'] + 1} of conversation #{row['convo']})"
                + (f", replaced by entry {row['superseded_by']}" if row["superseded_by"] else ""))

    out = [head(entry), *_describe(entry), "History:"]
    out += [x for row in older for x in ["  " + head(row), *_describe(row, "    ")]] if older else ["  none: no earlier entry was replaced"]
    return "\n".join(out)
