"""state.db is its own file with its own connection and lock, and old almanac.db files move their state across."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest

import helpers  # noqa: F401  (a scratch ALMANAC_HOME before anything else)
from getalmanac.memory import Memory
from getalmanac.store import STATE_DAYS, State, Store, migrate

ROWS = [("compact:a", json.dumps({"n": 1}), time.time() - 100), ("stubs:a", json.dumps({"ids": {}}), time.time() - 50),
        ("summary:day", json.dumps({"calls": 3}), time.time())]


def legacy_file(tmp, rows=ROWS):
    """An almanac.db as an older version left it: a state table next to the memory tables."""
    path = os.path.join(tmp, "almanac.db")
    db = sqlite3.connect(path)
    db.execute("create table state (key text primary key, value text not null, ts real not null)")
    db.execute("create table convo (id integer primary key)")
    db.executemany("insert into state values (?, ?, ?)", rows)
    db.commit()
    db.close()
    return path


def tables(path):
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("select name from sqlite_master where type = 'table'")}
    finally:
        db.close()


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-split-")
        self.state_path = os.path.join(self.tmp, "state.db")

    def test_fresh_home_has_no_state_in_the_memory_file(self):
        memory = Memory(Store(os.path.join(self.tmp, "almanac.db")))
        self.addCleanup(memory.close)
        memory.state.put("k", 1)
        self.assertEqual(memory.state.get("k"), 1)
        self.assertNotIn("state", tables(memory.store.path))
        self.assertIn("state", tables(self.state_path))
        self.assertIn("convo", tables(memory.store.path))

    def test_old_file_is_copied_counted_and_dropped(self):
        legacy = legacy_file(self.tmp)
        state = State(self.state_path, legacy=legacy)
        self.addCleanup(state.close)
        self.assertEqual({r["key"]: r["ts"] for r in state.run("select key, ts from state")}, {k: ts for k, _, ts in ROWS})
        self.assertEqual(state.get("compact:a"), {"n": 1})
        self.assertNotIn("state", tables(legacy))
        self.assertIn("convo", tables(legacy))  # only the state table goes

    def test_already_migrated_is_a_no_op(self):
        legacy = legacy_file(self.tmp)
        State(self.state_path, legacy=legacy).close()
        state = State(self.state_path, legacy=legacy)
        self.addCleanup(state.close)
        state.put("later", 2)
        self.assertEqual(migrate(legacy, state), 0)
        self.assertEqual(len(state.run("select 1 from state")), len(ROWS) + 1)

    def test_a_stop_between_copy_and_drop_loses_and_duplicates_nothing(self):
        legacy = legacy_file(self.tmp)
        # the copy had committed, and a newer value was written since; the old table was never dropped
        first = State(self.state_path)
        first.run("insert into state values ('compact:a', ?, ?)", (json.dumps({"n": 2}), time.time()))
        first.run("insert into state values ('stubs:a', ?, ?)", (json.dumps({"ids": {}}), ROWS[1][2]))
        first.close()
        self.assertIn("state", tables(legacy))
        state = State(self.state_path, legacy=legacy)
        self.addCleanup(state.close)
        self.assertNotIn("state", tables(legacy))
        self.assertEqual(state.get("compact:a"), {"n": 2})  # the newer copy wins
        self.assertEqual(state.get("summary:day"), {"calls": 3})  # the row that had not been copied is now
        self.assertEqual(len(state.run("select 1 from state")), len(ROWS))

    def test_a_stop_in_the_copy_keeps_the_old_table(self):
        legacy = legacy_file(self.tmp)
        state = State(self.state_path)
        self.addCleanup(state.close)
        state.run("create trigger boom before insert on state begin select raise(abort, 'stop'); end")
        with self.assertRaises(sqlite3.Error):
            migrate(legacy, state)
        self.assertIn("state", tables(legacy))
        self.assertEqual(state.run("select count(*) n from state")[0]["n"], 0)  # rolled back: no half copy
        state.run("drop trigger boom")
        self.assertEqual(migrate(legacy, state), len(ROWS))
        self.assertNotIn("state", tables(legacy))

    def test_pragmas_mode_and_expiry_on_the_new_file(self):
        old = time.time() - (STATE_DAYS + 1) * 86400
        legacy = legacy_file(self.tmp, ROWS + [("stale:x", "1", old)])
        state = State(self.state_path, legacy=legacy)
        self.addCleanup(state.close)
        self.assertEqual(state.run("pragma journal_mode")[0][0], "wal")
        self.assertEqual(state.run("pragma secure_delete")[0][0], 1)
        self.assertEqual(state.run("pragma busy_timeout")[0][0], 5000)
        self.assertIsNone(state.get("stale:x"))
        if os.name != "nt":
            self.assertEqual(os.stat(self.state_path).st_mode & 0o777, 0o600)


class Independence(unittest.TestCase):
    def test_a_state_write_is_not_blocked_by_the_memory_lock(self):
        tmp = tempfile.mkdtemp(prefix="almanac-split-")
        memory = Memory(Store(os.path.join(tmp, "almanac.db")))
        self.addCleanup(memory.close)
        held, done = threading.Event(), threading.Event()

        def hold():
            with memory.store.lock:
                held.set()
                done.wait(10)

        threading.Thread(target=hold, daemon=True).start()
        held.wait(5)
        result = []

        def work():
            memory.state.put("k", 1)
            memory.state.merge("m", a=1)
            result.append(memory.state.get("k"))

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(3)
        finished = not worker.is_alive()
        done.set()
        self.assertTrue(finished, "state access waited for the memory lock")
        self.assertEqual(result, [1])

    def test_frozen_compute_may_read_memory_while_memory_code_writes_state(self):
        tmp = tempfile.mkdtemp(prefix="almanac-split-")
        memory = Memory(Store(os.path.join(tmp, "almanac.db")))
        self.addCleanup(memory.close)
        inside, release = threading.Event(), threading.Event()

        def compute():
            inside.set()
            release.wait(10)
            return memory.store.run("select count(*) n from convo")[0]["n"]  # takes the memory lock

        slow = threading.Thread(target=lambda: memory.state.frozen("x", compute), daemon=True)
        slow.start()
        inside.wait(5)
        got = []

        def under_memory_lock():
            with memory.store.lock:  # forget and index hold this, then write state
                memory.state.put("y", 1)
                got.append(True)

        other = threading.Thread(target=under_memory_lock, daemon=True)
        other.start()
        other.join(3)
        release.set()
        slow.join(5)
        self.assertTrue(got, "a state write under the memory lock waited for a frozen compute")


class Forget(unittest.TestCase):
    def test_forget_clears_the_conversation_in_both_files(self):
        tmp = tempfile.mkdtemp(prefix="almanac-split-")
        memory = Memory(Store(os.path.join(tmp, "almanac.db")))
        self.addCleanup(memory.close)
        memory.index("lane-1", "s1", "", [{"role": "user", "content": "fix the cache"}],
                     {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": {}})
        memory.state.put("compact:lane-1", {"x": 1})
        memory.state.put("usage:lane-1", {"x": 1})
        memory.state.put("compact:other-lane", {"x": 1})
        memory.store.run("update convo set updated = updated - 7200")
        self.assertEqual(memory.forget(convo_id=memory.store.run("select id from convo")[0]["id"]), 1)
        self.assertEqual(memory.store.run("select count(*) n from convo")[0]["n"], 0)
        self.assertIsNone(memory.state.get("compact:lane-1"))
        self.assertIsNone(memory.state.get("usage:lane-1"))
        self.assertEqual(memory.state.get("compact:other-lane"), {"x": 1})


if __name__ == "__main__":
    unittest.main()
