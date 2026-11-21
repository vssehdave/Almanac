"""The local SQLite files.

`Store` is one connection behind a lock, shared by the request threads and the background worker. Two files use it:
almanac.db holds the memory (conversations, nodes, facts, library, projects) and state.db holds `State`, the small
per-conversation decisions every request reads and writes. Each has its own connection and lock, so a request's
state read never waits for an import, a search or a summary write. Everything here stays on this machine.

Lock order: a thread may take the memory store's lock and then the state's (forget, index), never the reverse, and
nothing holds the state's lock while it calls into memory. `State.frozen` runs its compute under a lock of its own
(`freezing`) for that reason: the compute may read memory, and no state operation waits on it.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config

SCHEMA = """
create table if not exists state (key text primary key, value text not null, ts real not null);
"""
STATE_DAYS = 45
VERSION = 1
STATE_FILE = "state.db"


class Store:
    """A SQLite file: one connection, one lock, private file mode, the pragmas every file here needs."""

    def __init__(self, path, schema=""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        config.private(self.path, 0o600)  # before the log files are made: they take this file's mode
        with self.lock:
            self.db.execute("pragma busy_timeout=5000")  # wait up to 5 s for another connection's write (the background import has its own)
            self.db.execute("pragma secure_delete=on")  # deleted text is overwritten, not left in the file
            self.db.execute("pragma journal_mode=wal")
            self.db.execute("pragma synchronous=normal")
            if schema:
                self.db.executescript(schema)
            if not self.db.execute("pragma user_version").fetchone()[0]:
                self.db.execute(f"pragma user_version = {VERSION}")  # for migrations in later releases

    def run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def many(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)

    def close(self):
        with self.lock:
            self.db.close()


class State(Store):
    """state.db: frozen decisions, counters and bookkeeping, one `state` table of key -> JSON value.

    `legacy` is the memory file (almanac.db): older versions kept the table there, and it moves here on first open."""

    def __init__(self, path, legacy=None):
        super().__init__(path, SCHEMA)
        self.freezing = threading.RLock()
        if legacy:
            migrate(legacy, self)
        with self.lock:
            self.db.execute("delete from state where ts < ?", (time.time() - STATE_DAYS * 86400,))

    def get(self, key, default=None):
        rows = self.run("select value, ts from state where key = ?", (key,))
        if not rows:
            return default
        if time.time() - rows[0]["ts"] > 86400:  # a key still being read belongs to a live conversation: keep it out of the sweep
            try:
                self.run("update state set ts = ? where key = ?", (time.time(), key))
            except sqlite3.Error:
                pass  # another process holds the write lock; the sweep can wait, the value was read
        return json.loads(rows[0]["value"])

    def put(self, key, value):
        self.run("insert into state (key, value, ts) values (?, ?, ?) "
                 "on conflict(key) do update set value = excluded.value, ts = excluded.ts",
                 (key, json.dumps(value), time.time()))

    def update(self, key, change):
        """Atomic read-modify-write: `change(old or None)` returns the new value. Holds SQLite's write lock from the read
        to the write, so another connection (the background import) cannot slip an update between them."""
        with self.lock:
            self.db.execute("begin immediate")
            try:
                rows = self.db.execute("select value from state where key = ?", (key,)).fetchall()
                new = change(json.loads(rows[0]["value"]) if rows else None)
                self.db.execute("insert into state (key, value, ts) values (?, ?, ?) "
                                "on conflict(key) do update set value = excluded.value, ts = excluded.ts",
                                (key, json.dumps(new), time.time()))
                self.db.execute("commit")
            except BaseException:
                self.db.execute("rollback")
                raise
            return new

    def merge(self, key, **fields):
        """Set some fields of a dict value and keep the others; writes only when one changes."""
        with self.lock:
            now = self.get(key) or {}
            if any(now.get(k) != v for k, v in fields.items()):
                self.put(key, {**now, **fields})

    def scan(self, *prefixes):
        """{key: value} of every key that starts with one of `prefixes` (each ends in ':'), in one query. Reads keep keys
        out of the sweep the way `get` does."""
        clause = " or ".join("(key >= ? and key < ?)" for _ in prefixes)
        args = [x for p in prefixes for x in (p, p[:-1] + chr(ord(p[-1]) + 1))]
        rows = self.run(f"select key, value, ts from state where {clause}", args)
        stale = [r["key"] for r in rows if time.time() - r["ts"] > 86400]
        if stale:
            try:
                self.many("update state set ts = ? where key = ?", [(time.time(), k) for k in stale])
            except sqlite3.Error:
                pass  # another process holds the write lock; the sweep can wait, the values were read
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def frozen(self, key, compute, outside=False):
        """A decision made once per key and returned unchanged afterwards.

        `outside` runs `compute` without any lock, for slow work: concurrent first callers may each compute a candidate,
        but the first to finish stores its own and every caller returns that one, so the bytes are identical."""
        if outside:
            value = self.get(key)
            if value is None:
                candidate = compute()
                with self.freezing:
                    value = self.get(key)
                    if value is None:
                        value = candidate
                        self.put(key, value)
            return value
        with self.freezing:  # not self.lock: compute may read memory, and memory code writes state while it holds its own lock
            value = self.get(key)
            if value is None:
                value = compute()
                self.put(key, value)
            return value


def migrate(legacy, state):
    """Move the `state` table out of the memory file into `state`. Safe to repeat and to crash in: the rows are copied
    (a newer copy wins) and counted in one transaction of state.db, and only then is the old table dropped, so a crash
    between the two leaves both copies and the next open finishes the drop. Returns the rows moved, 0 if none."""
    legacy = Path(legacy)
    if not legacy.exists():
        return 0
    old = sqlite3.connect(str(legacy), timeout=5, isolation_level=None)
    try:
        if not old.execute("select 1 from sqlite_master where type = 'table' and name = 'state'").fetchone():
            return 0
        rows = old.execute("select key, value, ts from state").fetchall()
        with state.lock:
            state.db.execute("begin immediate")
            try:
                state.db.executemany("insert into state (key, value, ts) values (?, ?, ?) on conflict(key) do update "
                                     "set value = excluded.value, ts = excluded.ts where excluded.ts > state.ts", rows)
                have = {r["key"] for r in state.db.execute("select key from state")}
                if not {r[0] for r in rows} <= have:
                    raise sqlite3.DatabaseError("state rows missing after the copy")
                state.db.execute("commit")
            except BaseException:
                state.db.execute("rollback")
                raise
        old.execute("pragma secure_delete=on")  # the dropped pages hold conversation text: overwrite them
        old.execute("drop table if exists state")
        try:
            old.execute("pragma wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass  # the next merge or forget checkpoints it
        return len(rows)
    finally:
        old.close()
