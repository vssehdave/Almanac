import contextlib
import io
import json
import time
import unittest

from test_keepalive import Case as KeepaliveCase
from test_playbook import Home

from getalmanac import cli, config, mcp, report


class Boom(Exception):
    pass


class ErrorLogTest(Home):
    def log(self):
        return report.Log(config.home() / "logs" / "requests.jsonl")

    def seed(self):
        now = time.time()
        for age, where in ((5, "old"), (7200, "mid"), (100000, "ancient")):
            self.log().write({"ts": now - age, "kind": "error", "where": where, "error": "Boom: x"})
        self.log().write({"ts": now - 1, "kind": "error", "where": "new", "agent": "Explore", "error": "Boom: y"})
        self.log().write({"ts": now, "kind": "request"})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["errors", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_helper_writes_a_trimmed_record_without_credentials(self):
        report.log_error("here", Boom("x" * 900 + " Bearer sk-ant-abcdefghijklmnop1234567890"))
        (rec,) = self.log().tail()
        self.assertEqual((rec["kind"], rec["where"]), ("error", "here"))
        self.assertTrue(rec["error"].startswith("Boom: xxx"))
        self.assertLessEqual(len(rec["error"]), 500)
        self.assertNotIn("sk-ant", json.dumps(rec))

    def test_lists_newest_first_within_the_window_and_limit(self):
        self.seed()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)  # the day-old one is outside 24h; the request record is not an error
        self.assertIn("new  agent Explore  Boom: y", lines[0])
        self.assertIn("old", lines[1])
        self.assertEqual(len(self.run_cli("--limit", "1")[1].strip().splitlines()), 1)
        self.assertEqual(len(self.run_cli("--since", "7d")[1].strip().splitlines()), 4)
        self.assertEqual([r["where"] for r in json.loads(self.run_cli("--json", "--limit", "2")[1])], ["new", "old"])

    def test_none_and_a_bad_window(self):
        self.assertEqual(self.run_cli()[:2], (0, "no errors\n"))
        self.assertEqual(self.run_cli("--since", "soon")[0], 2)

    def test_status_line_and_mcp_tool(self):
        self.seed()
        self.assertIn("3 error(s) in the last 24 hours, newest in new", report.error_line(self.log()))
        reply = mcp.handle(None, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "errors", "arguments": {"limit": 1}}}, ".")["result"]
        self.assertFalse(reply["isError"])
        self.assertIn("new  agent Explore", reply["content"][0]["text"])
        self.assertEqual(len(reply["content"][0]["text"].splitlines()), 1)
        self.assertIn("errors", [t["name"] for t in mcp.TOOLS])


class SilentSiteTest(KeepaliveCase):
    def test_a_raising_tick_leaves_a_record(self):
        self.real()
        entry = next(iter(self.rig.proxy.keepalive.lanes.values()))
        self.rig.proxy.keepalive._send = lambda _entry: (_ for _ in ()).throw(Boom("ping broke"))
        self.fire()
        (rec,) = [r for r in self.rig.records() if r["kind"] == "error"]
        self.assertEqual((rec["where"], rec["error"]), ("keepalive:tick", "Boom: ping broke"))
        self.assertNotIn(entry.lane, self.rig.proxy.keepalive.lanes)  # still fails open: the lane is dropped


if __name__ == "__main__":
    unittest.main()
