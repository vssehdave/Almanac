"""Project memory: a local tree of what earlier conversations did.

    project -> conversation -> topic -> exchange

An exchange is one thing the person asked plus how it ended (the final answer,
the tools used, the files touched). Exchanges on the same subject are grouped
under a topic by similarity. Everything is written by rule, with no model, and
lives in the SQLite file on this machine.

When a new conversation starts, the exchanges from earlier conversations in the
same project that match its first message are added to that message once, and
kept byte for byte from then on.
"""
import hashlib
import re
import time

from . import embed
from . import request as rq
from .match import STOP, tokens
from .project import Projects, tool_paths

SCHEMA = """
create table if not exists convo (
    id integer primary key, lane text unique not null, session text not null, project text not null,
    title text not null default '', started real not null, updated real not null, turns integer not null default 0
);
create table if not exists node (
    id integer primary key, convo integer not null, project text not null, parent integer,
    kind text not null, seq integer not null, ts real not null, title text not null, text text not null,
    mark text not null default '', vec blob
);
create index if not exists node_convo on node (convo, kind, seq);
create index if not exists node_project on node (project, kind, ts);
create virtual table if not exists node_fts using fts5(title, text, tokenize='porter unicode61');
"""
TOPIC_BAR = 0.22  # cosine above which an exchange joins an existing topic
BACKFILL = 400  # most exchanges indexed from one resumed conversation
_TAG = re.compile(r"</?[a-zA-Z][\w-]{0,40}>")
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[private key removed]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{20,}"), "[key removed]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[key removed]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[key removed]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[key removed]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[token removed]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)[^\s\"']{6,}"), r"\1\2[removed]"),
]


def redact(text: str) -> str:
    """Take obvious secrets out of text before it is stored."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def clean(text: str) -> str:
    return redact(_TAG.sub(" ", text or "")).strip()


def _first_line(text: str, limit=100) -> str:
    for line in text.splitlines():
        line = " ".join(line.split())
        if len(line) > 2:
            return line[:limit].rstrip() + ("…" if len(line) > limit else "")
    return ""


def exchange(group) -> dict:
    """Turn the messages of one exchange (a human turn up to the next) into a stored record."""
    human = clean(rq.human_text(group[0]))[:2000]
    says, tools = [], {}
    for msg in group[1:]:
        if msg.get("role") != "assistant":
            continue
        for block in rq.blocks(msg):
            if block.get("type") == "text" and (block.get("text") or "").strip():
                says.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                tools[block.get("name")] = tools.get(block.get("name"), 0) + 1
    answer = clean(says[-1])[:3000] if says else ""
    files = []
    for path in tool_paths(group):
        short = "/".join(path.split("/")[-2:])
        if short not in files:
            files.append(short)
    notes = []
    if tools:
        notes.append("Tools: " + ", ".join(f"{name} x{count}" if count > 1 else str(name) for name, count in sorted(tools.items())))
    if files:
        notes.append("Files: " + ", ".join(files[:12]))
    text = f"Asked: {human}\n\nAnswer: {answer}" + ("\n\n" + ". ".join(notes) if notes else "")
    return {"title": _first_line(human), "text": text, "mark": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()}


def snippet(text: str, terms: set, limit=230) -> str:
    """The stretch of `text` that holds the most query words."""
    pieces = [" ".join(p.split()) for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    if not pieces:
        return ""
    best = max(range(len(pieces)), key=lambda i: (len(terms & set(tokens(pieces[i]))), -i))
    out = pieces[best]
    if len(out) < limit // 2 and best + 1 < len(pieces):
        out += " " + pieces[best + 1]
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


class Memory:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript(SCHEMA)
        self.projects = Projects(store)

    # ---- writing -------------------------------------------------------------

    def _convo(self, lane, session, project, title) -> dict:
        now = time.time()
        self.store.run("insert or ignore into convo (lane, session, project, title, started, updated) values (?, ?, ?, ?, ?, ?)",
                       (lane, session, project, title, now, now))
        return dict(self.store.run("select * from convo where lane = ?", (lane,))[0])

    def _drop_nodes(self, ids):
        for node_id in ids:
            self.store.run("delete from node_fts where rowid = ?", (node_id,))
            self.store.run("delete from node where id = ?", (node_id,))

    def _add_node(self, convo, project, parent, kind, seq, title, text, mark, vec) -> int:
        with self.store.lock:
            cursor = self.store.db.execute(
                "insert into node (convo, project, parent, kind, seq, ts, title, text, mark, vec) values (?,?,?,?,?,?,?,?,?,?)",
                (convo, project, parent, kind, seq, time.time(), title, text, mark, embed.pack(vec)))
            self.store.db.execute("insert into node_fts (rowid, title, text) values (?, ?, ?)", (cursor.lastrowid, title, text))
            return cursor.lastrowid

    def _refresh_topic(self, topic_id):
        """Rebuild a topic's text and vector from the exchanges under it; drop it when empty."""
        leaves = self.store.run("select title, text, vec from node where parent = ? order by seq", (topic_id,))
        if not leaves:
            return self._drop_nodes([topic_id])
        lines = []
        for leaf in leaves:
            answer = leaf["text"].split("\n\nAnswer: ", 1)[-1].split("\n\n", 1)[0]
            lines.append(f"- {leaf['title']}: {_first_line(answer, 160)}")
        text = "\n".join(lines)[:1800]
        vec = embed.mean([embed.unpack(leaf["vec"]) for leaf in leaves])
        title = leaves[0]["title"]
        self.store.run("update node set title = ?, text = ?, vec = ?, ts = ? where id = ?",
                       (title, text, embed.pack(vec), time.time(), topic_id))
        self.store.run("delete from node_fts where rowid = ?", (topic_id,))
        self.store.run("insert into node_fts (rowid, title, text) values (?, ?, ?)", (topic_id, title, text))

    def _topic_for(self, convo_id, project, seq, title, vec) -> int:
        best, score = None, TOPIC_BAR
        for topic in self.store.run("select id, vec from node where convo = ? and kind = 'topic'", (convo_id,)):
            similarity = embed.cosine(vec, embed.unpack(topic["vec"]))
            if similarity >= score:
                best, score = topic["id"], similarity
        return best or self._add_node(convo_id, project, None, "topic", seq, title, "", "", vec)

    def index(self, lane, session, project, messages, reply=None) -> int:
        """Store the finished exchanges of a conversation. Returns how many were written."""
        turns = rq.human_turns(messages)
        if not turns:
            return 0
        finished = reply is not None and reply.get("stop_reason") != "tool_use"
        with self.store.lock:
            convo = self._convo(lane, session, project, _first_line(clean(rq.human_text(messages[turns[0]]))))
            stale = [r["id"] for r in self.store.run(
                "select id from node where convo = ? and kind = 'leaf' and seq >= ?", (convo["id"], len(turns)))]
            touched = {r["parent"] for r in self.store.run(
                f"select parent from node where id in ({','.join('?' * len(stale))})", stale)} if stale else set()
            self._drop_nodes(stale)  # the conversation was rewound: later exchanges are gone
            written, last_project = 0, project
            start_at = max(0, min(convo["turns"], len(turns)) - 1, len(turns) - BACKFILL)
            previous = self.store.run("select project from node where convo = ? and kind = 'leaf' and seq < ? "
                                      "order by seq desc limit 1", (convo["id"], start_at))
            if previous:
                last_project = previous[0]["project"]
            for seq in range(start_at, len(turns)):
                last = seq == len(turns) - 1
                if last and not finished:
                    break
                end = turns[seq + 1] if not last else len(messages)
                group = messages[turns[seq]:end] + ([{"role": "assistant", "content": reply["content"]}] if last else [])
                record = exchange(group)
                last_project = self.projects.for_turn(group, last_project)
                old = self.store.run("select id, mark, parent from node where convo = ? and kind = 'leaf' and seq = ?",
                                     (convo["id"], seq))
                if old and old[0]["mark"] == record["mark"]:
                    continue
                if old:
                    self._drop_nodes([old[0]["id"]])
                    touched.add(old[0]["parent"])
                vec = embed.embed(record["title"] + "\n" + record["text"])
                topic = self._topic_for(convo["id"], last_project, seq, record["title"], vec)
                self._add_node(convo["id"], last_project, topic, "leaf", seq, record["title"], record["text"], record["mark"], vec)
                touched.add(topic)
                written += 1
            for topic in touched - {None}:
                self._refresh_topic(topic)
            main = self.store.run("select project, count(*) n from node where convo = ? and kind = 'leaf' "
                                  "group by project order by n desc limit 1", (convo["id"],))
            self.store.run("update convo set turns = ?, updated = ?, project = ? where id = ?",
                           (len(turns) if finished else len(turns) - 1, time.time(),
                            main[0]["project"] if main else project, convo["id"]))
            return written

    # ---- reading -------------------------------------------------------------

    def search(self, query, project=None, exclude_lane=None, limit=8, kind="leaf") -> list:
        """Stored exchanges that match `query`, best first. Needs two of its words (one if it has one)."""
        words = sorted({w for w in re.findall(r"[a-z0-9_]{2,}", query.lower()) if w not in STOP})[:24]
        terms = set(tokens(query))
        if not words or not terms:
            return []
        sql = ("select n.id, n.kind, n.title, n.text, n.ts, n.vec, n.project, n.convo, c.title ctitle, "
               "bm25(node_fts, 3.0, 1.0) rank from node_fts join node n on n.id = node_fts.rowid "
               "join convo c on c.id = n.convo where node_fts match ? and n.kind = ?")
        args = [" OR ".join(f'"{w}"' for w in words), kind]
        if project:
            sql, args = sql + " and n.project = ?", args + [project]
        if exclude_lane:
            sql, args = sql + " and c.lane != ?", args + [exclude_lane]
        vector, need, hits = embed.embed(query), min(2, len(terms)), []
        for row in self.store.run(sql + " order by rank limit 60", args):
            overlap = len(terms & set(tokens(row["title"] + " " + row["text"])))
            if overlap < need:
                continue
            score = -row["rank"] + 4.0 * embed.cosine(vector, embed.unpack(row["vec"])) + 0.5 * overlap
            hits.append(dict(row, score=round(score, 3), snippet=snippet(row["text"], terms)))
        hits.sort(key=lambda h: (-h["score"], -h["ts"]))
        for hit in hits:
            hit.pop("vec", None)
        return hits[:limit]

    def recent(self, project=None, exclude_lane=None, limit=3) -> list:
        sql, args = "select * from convo where turns > 0", []
        if project:
            sql, args = sql + " and project = ?", args + [project]
        if exclude_lane:
            sql, args = sql + " and lane != ?", args + [exclude_lane]
        return [dict(r) for r in self.store.run(sql + " order by updated desc limit ?", args + [limit])]

    def read(self, node_id: int):
        """One node with what is under it: a topic's exchanges, or an exchange's full text."""
        rows = self.store.run("select n.*, c.title ctitle, c.started from node n join convo c on c.id = n.convo "
                              "where n.id = ?", (node_id,))
        if not rows:
            return None
        node = dict(rows[0])
        node.pop("vec", None)
        node["children"] = [dict(r) for r in self.store.run(
            "select id, seq, title from node where parent = ? order by seq", (node_id,))]
        return node

    def outline(self, convo_id: int) -> list:
        """A conversation's topics, each with its exchanges."""
        topics = [dict(r) for r in self.store.run(
            "select id, title, seq from node where convo = ? and kind = 'topic' order by seq", (convo_id,))]
        for topic in topics:
            topic["children"] = [dict(r) for r in self.store.run(
                "select id, seq, title from node where parent = ? order by seq", (topic["id"],))]
        return topics

    def counts(self) -> dict:
        row = self.store.run("select (select count(*) from convo) convos, "
                             "(select count(*) from node where kind = 'leaf') exchanges, "
                             "(select count(*) from node where kind = 'topic') topics")[0]
        return dict(row)

    # ---- recall ----------------------------------------------------------------

    def recall(self, project, first_text, lane, limit_chars, tools_hint=True) -> str:
        """The note added to a new conversation's first message, or "" when nothing is worth adding."""
        if not project:
            return ""
        hits = self.search(first_text, project=project, exclude_lane=lane, limit=6)
        recent = self.recent(project=project, exclude_lane=lane)
        if not hits and not recent:
            return ""
        day = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts))  # noqa: E731
        lines = [f"Almanac memory for this project ({self.projects.name(project)}), from earlier conversations on "
                 "this machine. It can be out of date: check it against the code before relying on it."]
        if recent:
            lines.append("Most recent conversations:")
            lines += [f"- {day(c['updated'])} {c['title']} ({c['turns']} exchange{'s' if c['turns'] != 1 else ''})" for c in recent]
        if hits:
            lines.append("Earlier exchanges related to this message:")
            lines += [f"- [{h['id']}] {day(h['ts'])} {h['title']}: {h['snippet']}" for h in hits]
        if tools_hint:
            lines.append("To read one in full or search further, load the almanac memory tools with ToolSearch "
                         "(memory_read takes the number in brackets).")
        body = ""
        for line in lines:
            if len(body) + len(line) > limit_chars:
                break
            body += line + "\n"
        return "<system-reminder>\n" + body + "</system-reminder>"

    # ---- upkeep ----------------------------------------------------------------

    def forget(self, project=None, convo_id=None) -> int:
        """Delete stored conversations: one, a project's, or all. Returns how many."""
        sql, args = "select id, lane from convo", []
        if convo_id is not None:
            sql, args = sql + " where id = ?", [convo_id]
        elif project:
            sql, args = sql + " where project = ?", [project]
        with self.store.lock:
            rows = self.store.run(sql, args)
            for row in rows:
                self._drop_nodes([r["id"] for r in self.store.run("select id from node where convo = ?", (row["id"],))])
                self.store.run("delete from convo where id = ?", (row["id"],))
            if project and convo_id is None:
                self._drop_nodes([r["id"] for r in self.store.run("select id from node where project = ?", (project,))])
            return len(rows)

    def merge_projects(self, source, target):
        with self.store.lock:
            self.projects.merge(source, target)
            self.store.run("update node set project = ? where project = ?", (target, source))
            self.store.run("update convo set project = ? where project = ?", (target, source))
            self.store.run("update used set project = ? where project = ?", (target, source))


class ProjectLayer:
    """Work out the conversation's project once, before the other layers run."""

    def __init__(self, memory):
        self.memory = memory

    def transform(self, body, ctx):
        messages = body.get("messages") or []
        turns = rq.human_turns(messages)
        first = rq.human_text(messages[turns[0]]) if turns else ""
        ctx.project = ctx.store.frozen(f"project:{ctx.lane}", lambda: self.memory.projects.for_start(ctx.cwd, first))


class MemoryLayer:
    """Add what earlier conversations in the project learned; index this one as it goes."""

    def __init__(self, memory):
        self.memory = memory

    @staticmethod
    def _main_thread(body, ctx) -> bool:
        return not ctx.agent and bool(body.get("tools")) and bool(rq.human_turns(body.get("messages") or []))

    def transform(self, body, ctx):
        cfg = ctx.cfg["memory"]
        if not (cfg["enabled"] and cfg["recall"]) or not self._main_thread(body, ctx):
            return
        messages = body["messages"]
        first = messages[rq.human_turns(messages)[0]]
        note = ctx.store.frozen(f"recall:{ctx.lane}", lambda: self.memory.recall(
            ctx.project, rq.human_text(first), ctx.lane, cfg["recall_chars"], tools_hint=cfg.get("tools", True)))
        if not note:
            return
        block = {"type": "text", "text": note}
        if isinstance(first.get("content"), str):
            first["content"] = [block, {"type": "text", "text": first["content"]}]
        else:
            first["content"].insert(max(0, len(first["content"]) - 1), block)
        ctx.stats["memory"] = {"recall_chars": len(note)}

    def observe(self, ctx, orig, reply):
        if not ctx.cfg["memory"]["enabled"] or not self._main_thread(orig, ctx):
            return
        self.memory.index(ctx.lane, ctx.session, ctx.project, orig["messages"], reply)
