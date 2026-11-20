import contextlib
import copy
import io
import json
import unittest

from helpers import Rig
from test_proxy import BODY

from getalmanac import cli, report

NOW = 1_000_000.0
USE = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 20}


def rec(ago, ttl=("5m",), idle=None, usage=USE, **more):
    out = {"kind": "messages", "ts": NOW - ago, "usage": dict(usage), "cache": {"marks": len(ttl), "ttl": list(ttl)}}
    if idle is not None:
        out["idle"] = idle
    return dict(out, **more)


class MarksTest(unittest.TestCase):
    def test_system_tools_and_messages_are_counted_in_order(self):
        body = {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                "tools": [{"name": "t", "cache_control": {"type": "ephemeral"}}, {"name": "u"}],
                "messages": [{"role": "user", "content": "plain"},
                             {"role": "user", "content": [{"type": "text", "text": "x"},
                                                          {"type": "text", "text": "y", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]}]}
        self.assertEqual(report.cache_marks(body), {"marks": 3, "ttl": ["1h", "5m", "5m"]})

    def test_malformed_bodies_do_not_raise(self):
        for body in ({}, {"system": "text", "tools": [None, 3], "messages": [None, {"content": [1, None]}]},
                     {"system": [{"cache_control": "x"}]}):
            self.assertEqual(report.cache_marks(body)["marks"], 0)


class MeterTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec(60, idle=30),
            rec(120, ttl=("1h", "5m"), idle=400),          # 400s > 300s, the shortest ttl: cold
            rec(180, ttl=("1h",), idle=400),               # 400s < 3600s: warm
            rec(6 * 3600, ttl=("5m",), idle=10_000),       # outside 5h, inside 24h, cold
            rec(30 * 3600, idle=10_000),                   # outside 24h
            {"kind": "messages", "ts": NOW - 10, "usage": "bad"}, "junk", {"ts": "x", "usage": {}},
        ]

    def test_windows_and_cache_health(self):
        m = report.meter(self.records, NOW)
        self.assertEqual(m["5h"]["requests"], 3)
        self.assertEqual(m["24h"]["requests"], 4)
        self.assertEqual(m["24h"]["cache_read_input_tokens"], 4000)
        self.assertNotIn("weighted", m["24h"])
        self.assertEqual(m["cache"]["cold_returns"], 2)
        self.assertEqual(m["cache"]["cold_write_tokens"], 200)
        self.assertEqual(m["24h"]["cold_share"], round(200 / (4 * 110), 3))  # cold writes over the period's writes + input
        self.assertEqual((m["5h"]["cold_returns"], m["5h"]["cold_write_tokens"]), (1, 100))
        self.assertEqual(m["cache"]["ttl_seen"], {"5m": 3, "1h": 2})
        self.assertEqual(m["cache"]["read_share"], round(4000 / 4440, 3))

    def test_empty_log_is_fine(self):
        m = report.meter([], NOW)
        self.assertIsNone(m["cache"]["read_share"])
        self.assertEqual(len(report.render_meter([], NOW)), 5)

    def test_compacted_cold_returns_are_counted_with_their_saving(self):
        records = [rec(60, ttl=("1h",), idle=4000, cold={"before": 90_000, "after": 30_000}),
                   rec(120, ttl=("1h",), idle=4000), rec(180, idle=10, cold={"before": 5, "after": "x"})]
        m = report.meter(records, NOW)
        self.assertEqual((m["24h"]["cold_returns"], m["24h"]["cold_compacted"], m["24h"]["cold_saved_tokens"]), (2, 1, 60_000))
        text = " ".join(report.render_meter(records, NOW))
        self.assertIn("2 returns wrote 200 cache tokens", text)
        self.assertIn("1 compacted, 60,000 tokens saved", text)
        self.assertNotIn("weighted", text)

    def test_status_prints_the_lines(self):
        import time
        from getalmanac import config
        log = report.Log(config.home() / "logs" / "requests.jsonl")
        log.write(rec(60, idle=1000, ts=time.time() - 60))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
            self.assertEqual(cli.main(["report", "--json"]), 0)
        text = out.getvalue()
        self.assertIn("usage 5h", text)
        self.assertIn("usage 24h", text)
        self.assertIn("returns wrote", text)
        self.assertNotIn("weighted", text)
        self.assertIn('"meter"', text)


class LogTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_idle_starts_on_a_lanes_second_request(self):
        body = copy.deepcopy(BODY)
        body["system"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        raw = json.dumps(body).encode()
        self.rig.messages(raw)
        self.rig.messages(raw)
        first, second = self.rig.records()[-2:]
        self.assertEqual(first["cache"], {"marks": 1, "ttl": ["1h"]})
        self.assertNotIn("idle", first)
        self.assertGreaterEqual(second["idle"], 0)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)


if __name__ == "__main__":
    unittest.main()
