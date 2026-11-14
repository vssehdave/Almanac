"""The local SQLite file: frozen per-conversation decisions and usage counts.

One connection shared by the request threads and the background worker, behind
a lock. Everything here stays on this machine.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists state (key text primary key, value text not null, ts real not null);
create table if not exists used (
    kind text not null, name text not null, project text not null, lane text not null, ts real not null,
    primary key (kind, name, lane)
);
create index if not exists used_project on used (kind, project, ts);
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
        with self.lock:
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
        rows = self.run("select value from state where key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

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

    def mark_used(self, kind, name, project, lane):
        self.run("insert or ignore into used (kind, name, project, lane, ts) values (?, ?, ?, ?, ?)",
                 (kind, name, project, lane, time.time()))

    def recently_used(self, kind, project, lanes=10) -> dict:
        """name -> how many of the project's last `lanes` conversations used it."""
        rows = self.run(
            "select name, count(*) n from used where kind = ? and project = ? and lane in "
            "(select lane from used where kind = ? and project = ? group by lane order by max(ts) desc limit ?) "
            "group by name", (kind, project, kind, project, lanes))
        return {r["name"]: r["n"] for r in rows}

    def close(self):
        with self.lock:
            self.db.close()
