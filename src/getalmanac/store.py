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

    def frozen(self, key, compute):
        """A decision made once per key and returned unchanged afterwards."""
        with self.lock:
            value = self.get(key)
            if value is None:
                value = compute()
                self.put(key, value)
            return value

    def close(self):
        with self.lock:
            self.db.close()
