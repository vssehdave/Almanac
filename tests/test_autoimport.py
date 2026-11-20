import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_import as ti
from helpers import Rig

from getalmanac import autoimport, config, summarize

REAL_SOURCE = autoimport.source


class Base(ti.ImportBase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load()
        self.cfg["memory"]["summary_model"] = ""  # no summary call unless a test turns it on

    def leaf(self, n=0):
        return self.store.run("select id from node where kind = 'leaf' order by seq, id")[n]["id"]

    def stored(self, n=1, **kw):
        t = self.session(**kw)
        for i in range(n):
            t.turn(ti.stamp(2, i), f"question {i} about the parser", f"answer {i}", tool=("Edit", {"file_path": self.a + "/a.py"}))
        self.run_import(t.write())


class Round(Base):
    """autoimport.run_once on a projects folder laid out like Claude Code's."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "projects")
        self.folder = os.path.join(self.root, "alpha-folder")
        patch = mock.patch.object(autoimport, "source", lambda: Path(self.root))
        patch.start()
        self.addCleanup(patch.stop)

    def round(self, now=None):
        return autoimport.run_once(self.memory, self.cfg, now=now)

    def test_a_new_session_of_a_stored_project_is_imported_once(self):
        self.stored(session="bbbbbbbb-0000-4000-8000-000000000001")  # the project is known now
        self.session(session="bbbbbbbb-0000-4000-8000-000000000002").turn(ti.stamp(1), "new work", "ok").write()
        first = self.round()
        self.assertEqual((first["files"], first["exchanges"]), (2, 1))  # the first file is read again and adds nothing
        again = self.round()
        self.assertEqual((again["files"], again["exchanges"]), (0, 0))

    def test_a_grown_session_adds_its_new_exchanges(self):
        t = self.session(session="bbbbbbbb-0000-4000-8000-000000000003").turn(ti.stamp(1, 0), "one", "ok")
        self.stored()
        t.write()
        self.assertEqual(self.round()["exchanges"], 1)
        t.turn(ti.stamp(1, 5), "two", "ok").write()
        grown = self.round()
        self.assertEqual((grown["files"], grown["exchanges"]), (1, 1))

    def test_a_project_the_store_does_not_know_is_left_alone_until_it_is_known(self):
        self.session(session="bbbbbbbb-0000-4000-8000-000000000004", cwd=self.b).turn(ti.stamp(1), "beta work", "ok").write()
        self.stored()  # Alpha only
        self.assertEqual(self.round()["exchanges"], 0)
        self.assertEqual(self.store.run("select count(*) n from convo where project = ?", (self.key(self.b),))[0]["n"], 0)
        self.assertEqual(self.round()["exchanges"], 0)  # a second round with nothing new must not forget why it waited
        t = self.session(session="bbbbbbbb-0000-4000-8000-000000000005", cwd=self.b).turn(ti.stamp(3), "beta older", "ok")
        self.run_import(t.write())  # now Beta is stored
        self.assertEqual(self.round()["exchanges"], 1)  # the file that was left alone is looked at again

    def test_an_empty_store_is_never_filled(self):
        self.session().turn(ti.stamp(1), "work", "ok").write()
        self.assertEqual(self.round()["exchanges"], 0)

    def test_a_session_still_being_written_waits_and_old_ones_are_skipped(self):
        self.stored(session="bbbbbbbb-0000-4000-8000-000000000006")
        self.round()  # that one is settled now
        busy = self.session(session="bbbbbbbb-0000-4000-8000-000000000007").turn(ti.stamp(0), "busy", "ok").write()
        os.utime(busy, (time.time() - 60,) * 2)
        old = self.session(session="bbbbbbbb-0000-4000-8000-000000000008").turn(ti.stamp(40), "old", "ok").write()
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        self.assertEqual(self.round()["files"], 0)
        os.utime(busy, (time.time() - 7200,) * 2)
        self.assertEqual(self.round()["exchanges"], 1)

    def test_the_active_login_folder_is_read(self):
        home = os.path.join(self.tmp, "home")
        work = os.path.join(home, ".claude-work")
        with mock.patch.object(Path, "home", return_value=Path(home)):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": work}):
                self.assertEqual(REAL_SOURCE(), Path(work) / "projects")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
                self.assertEqual(REAL_SOURCE(), Path(home) / ".claude" / "projects")

    def test_the_summary_pass_and_vectors_follow(self):
        self.cfg["memory"]["summary_model"] = "claude-haiku-5-5"
        with mock.patch.object(summarize, "refine", return_value={"summarized": 2, "failed": 0, "stopped": "paused"}) as refine:
            report = self.round()
        self.assertTrue(refine.call_args.kwargs["say"])
        self.assertEqual((report["summarized"], report["stopped"]), (2, "paused"))
        self.assertIn(report["vectors"], (None, 0))
        self.assertIn("2 summarized", autoimport.line(self.store))

    def start(self, config_json=None, home=None):
        """autoimport.start over a home that is not ~/.almanac (or `home`), with `config_json` written there. Returns (thread, ran)."""
        ran = threading.Event()
        self.cfg["memory"]["auto_import"] = True
        folder = home or os.path.join(self.tmp, "almanac-home")
        os.makedirs(folder, exist_ok=True)
        if config_json:
            with open(os.path.join(folder, "config.json"), "w") as handle:
                handle.write(config_json)
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": folder}),                 mock.patch.object(autoimport, "run_once", lambda memory, cfg: ran.set()):
            thread = autoimport.start(mock.Mock(cfg=self.cfg, store=self.store), first=0, every=3600)
            return thread, ran.wait(3 if thread else 0.2)

    def test_start_runs_a_round_in_the_background(self):
        thread, ran = self.start('{"memory": {"auto_import": true}}')
        self.assertTrue(thread and ran)

    def test_a_scratch_home_does_not_import_unless_it_says_so(self):
        for text in (None, '{"memory": {"semantic": false}}', '{"memory": {"auto_import": "yes"}}'):
            self.assertEqual(self.start(text), (None, False), text)

    def test_only_the_default_home_counts_as_the_users_own(self):
        default = str(Path.home() / ".almanac")
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": default}):
            self.assertTrue(autoimport._own_home())
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": self.tmp}):
            self.assertFalse(autoimport._own_home())
        with mock.patch.dict(os.environ):
            os.environ.pop("ALMANAC_HOME", None)
            self.assertTrue(autoimport._own_home())

    def test_a_test_proxy_never_imports_the_real_transcripts(self):
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": self.tmp}):  # a home with no config: the defaults say auto_import is on
            rig = Rig(store=self.store)
        self.assertIsNone(rig.proxy.importing)

    def test_it_is_off_when_the_setting_is_off(self):
        proxy = mock.Mock(cfg={"memory": {"enabled": True, "auto_import": False}}, store=self.store)
        self.assertIsNone(autoimport.start(proxy))

if __name__ == "__main__":
    unittest.main()
