import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import test_import as ti

from getalmanac import accounts, cli, config, summarize

MODEL = "claude-haiku-5-5"
ANSWER = {"title": "Fixed the parser", "summary": "Fixed a bug in parser.py.", "status": "open"}


class FakeConn:
    """Stands in for the HTTPS connection to the API; records what was sent. Nothing leaves the machine."""

    sent = []

    def __init__(self, statuses):
        self.statuses, self.timeout = statuses, 0

    def request(self, method, path, body, headers):
        FakeConn.sent.append({"method": method, "path": path, "body": json.loads(body), "headers": headers})
        self.status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def getresponse(self):
        conn = self

        class Resp:
            status = conn.status

            def read(self):
                return json.dumps({"content": [{"type": "text", "text": json.dumps(ANSWER)}],
                                   "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
        return Resp()

    def close(self):
        pass


class Refine(ti.ImportBase):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.login = self.credentials(os.path.join(self.home, ".claude"), "personal-token")
        self.other = self.credentials(os.path.join(self.tmp, "work"), "work-token")
        self.statuses = [200]
        FakeConn.sent = []
        for patch in (mock.patch.object(Path, "home", return_value=Path(self.home)),
                      mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.other}),
                      mock.patch.object(summarize._Upstream, "connect", lambda me: FakeConn(self.statuses)),
                      mock.patch.object(summarize.time, "sleep", lambda s: None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.cfg = config.load()
        self.cfg["memory"]["summary_model"] = MODEL

    def credentials(self, folder, token):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, ".credentials.json"), "w") as handle:
            json.dump({"claudeAiOauth": {"accessToken": token, "expiresAt": (time.time() + 3600) * 1000}}, handle)
        return folder

    def stored(self, days=2, n=2):
        t = self.session()
        for i in range(n):
            t.turn(ti.stamp(days, i), f"question {i}", f"answer {i}")
        self.run_import(t.write())

    def refine(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report = summarize.refine(self.memory, self.cfg, **kw)
        return report, out.getvalue()

    def test_the_token_comes_from_the_login_almanac_runs_with(self):
        self.stored(n=1)
        self.refine()
        self.assertEqual(len(FakeConn.sent), 1)
        sent = FakeConn.sent[0]
        self.assertEqual(sent["headers"]["authorization"], "Bearer work-token")
        self.assertIn("oauth-2025-04-20", sent["headers"]["anthropic-beta"])
        self.assertEqual(sent["body"]["model"], MODEL)
        self.assertEqual(sent["body"]["system"][0]["text"], summarize.IDENTITY)
        self.assertEqual(sent["path"], "/v1/messages?beta=true")

    def test_it_stops_without_a_login_in_the_config_dir(self):
        os.remove(os.path.join(self.other, ".credentials.json"))
        self.stored(n=1)
        report, out = self.refine()
        self.assertEqual(FakeConn.sent, [])
        self.assertIn("login", out)

    def test_it_only_sends_to_the_anthropic_api(self):
        proxy = summarize._Upstream()
        self.assertTrue(accounts.allowed(proxy))

    def test_only_imported_exchanges_without_a_summary_are_picked(self):
        self.stored(n=2)
        live = self.memory.index("proxylane", "live", self.key(self.a), ti.importer.load(
            self.session(session="live").turn(ti.stamp(1), "a live one", "ok").write())["messages"], finished=True)
        self.assertEqual(live, 1)
        first = self.store.run("select n.id from node n join convo c on c.id = n.convo where c.lane like 'import:%' and n.seq = 0")[0]["id"]
        self.memory.set_summary(first, "Done already", "Has one.")
        report, _ = self.refine()
        self.assertEqual(report["summarized"], 1)
        self.assertEqual(len(FakeConn.sent), 1)
        lanes = {r["lane"] for r in self.store.run("select c.lane from node n join convo c on c.id = n.convo where n.summary != ''")}
        self.assertEqual(lanes, {f"import:{ti.SID}:0"})
        self.assertEqual(self.store.run("select count(*) n from node n join convo c on c.id = n.convo "
                                        "where c.lane = 'proxylane' and n.summary != ''")[0]["n"], 0)

    def test_backfill_summarizes_a_conversation_and_stores_the_models_closing_verdict(self):
        self.stored(n=3)
        self.store.run("update node set status = 'open' where seq = 0")
        with mock.patch.dict(ANSWER, {"summary": "Three questions answered.", "closed": [1, 3]}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                report = summarize.backfill(self.memory, self.cfg)
        self.assertEqual((report["pending"], report["summarized"]), (1, 1))
        self.assertIn(summarize.CONVO_INSTRUCTION, [b["text"] for b in FakeConn.sent[0]["body"]["system"]])
        self.assertEqual(FakeConn.sent[0]["headers"]["authorization"], "Bearer work-token")
        self.assertEqual(self.store.run("select summary, sumturns from convo")[0]["summary"], "Three questions answered.")
        self.assertEqual([r["status"] for r in self.store.run("select status from node where kind = 'leaf' order by seq")][0], "done")
        with contextlib.redirect_stdout(io.StringIO()):  # now summarized: nothing left to do
            self.assertEqual(summarize.backfill(self.memory, self.cfg)["pending"], 0)

    def test_the_summary_goes_through_set_summary(self):
        self.stored(n=1)
        report, out = self.refine()
        row = self.store.run("select title, summary, status, mstatus from node where kind = 'leaf'")[0]
        self.assertEqual((row["title"], row["summary"], row["mstatus"]), ("Fixed the parser", "Fixed a bug in parser.py.", "open"))
        self.assertEqual(report["summarized"], 1)
        self.assertIn("1 summarized", out)
        self.assertEqual(summarize.today(self.state)["calls"], 1)

    def test_the_call_asks_for_facts_and_stores_them(self):
        self.stored(n=1)
        with mock.patch.dict(ANSWER, {"facts": ["Trigger is 100k tokens", "parser.py: fix off-by-one"]}):
            self.refine()
        self.assertIn("facts", FakeConn.sent[0]["body"]["system"][1]["text"])
        self.assertEqual(FakeConn.sent[0]["body"]["thinking"], {"type": "disabled"})
        row = self.store.run("select id, facts from node where kind = 'leaf'")[0]
        self.assertEqual(row["facts"], 1)
        self.assertEqual([r["text"] for r in self.store.run("select text from fact where node = ?", (row["id"],))],
                         ["Trigger is 100k tokens", "parser.py: fix off-by-one"])

    def test_facts_backfill_touches_only_the_facts(self):
        self.stored(n=2)
        first = self.store.run("select id from node where kind = 'leaf' order by seq")[0]["id"]
        self.memory.set_summary(first, "Old title", "Old summary.", "done")  # summarized before facts existed
        with mock.patch.dict(ANSWER, {"facts": ["A new fact"]}):
            report, _ = self.refine(facts=True)
        self.assertEqual(report["summarized"], 1)  # only the exchange that has a summary and no facts yet
        row = self.store.run("select title, summary, facts from node where id = ?", (first,))[0]
        self.assertEqual((row["title"], row["summary"], row["facts"]), ("Old title", "Old summary.", 1))
        self.assertEqual(self.memory.search("new fact")[0]["id"], first)
        self.assertEqual(self.refine(facts=True)[0]["pending"], 0)

    def test_old_work_is_never_reopened(self):
        self.stored(days=30, n=1)  # the fake model says "open"
        self.refine()
        self.assertEqual([r["status"] for r in self.store.run("select status from node where kind = 'leaf'")], ["done"])

    def test_limit_and_project_narrow_the_pick(self):
        t = self.session().turn(ti.stamp(2, 0), "alpha work", "ok", tool=("Edit", {"file_path": self.a + "/a.py"}))
        t.turn(ti.stamp(2, 1), "beta work", "ok", tool=("Edit", {"file_path": self.b + "/b.py"}))
        self.run_import(t.write())
        report, _ = self.refine(project=self.key(self.b))
        self.assertEqual(report["summarized"], 1)
        self.assertEqual({r["project"] for r in self.store.run("select project from node where summary != ''")}, {self.key(self.b)})
        report, _ = self.refine(limit=1)  # the Alpha one is still waiting
        self.assertEqual(report["summarized"], 1)
        self.assertEqual(len(FakeConn.sent), 2)

    def test_dry_run_calls_nothing(self):
        self.stored(n=2)
        report, out = self.refine(dry_run=True)
        self.assertEqual(FakeConn.sent, [])
        self.assertEqual(report["pending"], 2)
        self.assertIn("2", out)

    def test_an_explicit_limit_may_pass_the_daily_cap(self):
        self.cfg["memory"]["summary_daily_calls"] = 1
        self.stored(n=3)
        report, _ = self.refine()
        self.assertEqual(report["summarized"], 1)  # the default stops at the cap
        report, _ = self.refine(limit=5)
        self.assertEqual(report["summarized"], 2)

    def test_a_429_stops_cleanly_and_keeps_the_try(self):
        self.stored(n=3)
        self.statuses[:] = [429]
        report, out = self.refine()
        self.assertEqual(len(FakeConn.sent), 1)
        self.assertEqual(report["summarized"], 0)
        self.assertTrue(report["stopped"])
        self.assertIn("429", out)
        self.assertTrue(summarize.paused_until(self.state))
        self.assertEqual(self.store.run("select count(*) n from node where tries > 0")[0]["n"], 0)  # not the exchange's fault
        again, out = self.refine()  # paused: no call at all
        self.assertEqual(len(FakeConn.sent), 1)
        self.assertIn("paused", out)

    def test_cli_dry_run(self):
        self.stored(n=1)
        os.environ["ALMANAC_HOME"], old = self.tmp, os.environ["ALMANAC_HOME"]
        self.addCleanup(os.environ.__setitem__, "ALMANAC_HOME", old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["memory", "summarize", "--dry-run"]), 0)
        self.assertIn("1 imported exchange", out.getvalue())


if __name__ == "__main__":
    unittest.main()
