"""The local SQLite file: frozen per-conversation decisions.

One connection shared by the request threads and the background worker, behind
a lock. Everything here stays on this machine.
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


class Store:
    def __init__(self, path):
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
            self.db.executescript(SCHEMA)
            if not self.db.execute("pragma user_version").fetchone()[0]:
                self.db.execute(f"pragma user_version = {VERSION}")  # for migrations in later releases
            self.db.execute("delete from state where ts < ?", (time.time() - STATE_DAYS * 86400,))

    def run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def many(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)

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

        `outside` runs `compute` without the lock, for slow work: concurrent first callers may each compute a candidate,
        but the first to finish stores its own and every caller returns that one, so the bytes are identical."""
        if outside:
            value = self.get(key)
            if value is None:
                candidate = compute()
                with self.lock:
                    value = self.get(key)
                    if value is None:
                        value = candidate
                        self.put(key, value)
            return value
        with self.lock:
            value = self.get(key)
            if value is None:
                value = compute()
                self.put(key, value)
            return value

    def close(self):
        with self.lock:
            self.db.close()
