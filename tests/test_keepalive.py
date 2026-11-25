import json
import os
import unittest

from helpers import Rig, SESSION, sse

from getalmanac import report
from getalmanac.keepalive import Keepalive, ping_body, ping_tokens

TOKEN = "Bearer secret-login-do-not-store"
USAGE = {"input_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5000, "output_tokens": 1}


class Timer:
    """A timer that never runs by itself: the test fires it."""
    made = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled, self.daemon = seconds, fn, False, False
        Timer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def body(ttl=None, text="hello", tools=True, **more):
    mark = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    out = {"model": "claude-sonnet-5-5", "max_tokens": 4096, "stream": True,
           "system": [{"type": "text", "text": "system", "cache_control": mark}],
           "messages": [{"role": "user", "content": text}]}
    if tools:
        out["tools"] = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]
    return dict(out, **more)


class Case(unittest.TestCase):
    ping_status, ping_usage = 200, USAGE

    def setUp(self):
        Timer.made = []
        self.clock = Clock()
        self.clock.now = 1000.0
        self.rig = self.make(overrides={"keepalive.enabled": True})

    def make(self, overrides):
        def respond(req):
            if json.loads(req["body"]).get("stream"):
                return 200, {"content-type": "text/event-stream"}, sse()
            if self.ping_status != 200:
                return self.ping_status, {"content-type": "application/json"}, b'{"type":"error"}'
            return 200, {"content-type": "application/json"}, json.dumps({"usage": self.ping_usage}).encode()
        rig = Rig(respond, overrides=overrides)
        rig.proxy.keepalive = Keepalive(rig.proxy, self.clock, Timer)
        self.addCleanup(rig.close)
        return rig

    def real(self, b=None, session=SESSION):
        status, _, _ = self.rig.messages(b or body(), session=session, extra={"authorization": TOKEN})
        self.rig.records()
        return status

    def live(self):
        return [t for t in Timer.made if not t.cancelled]

    def fire(self):
        timer = self.live()[-1]
        timer.fn()
        return timer

    def pings(self):
        return [r for r in self.rig.records() if r["kind"] == "keepalive"]


class ScheduleTest(Case):
    def test_off_by_default(self):
        rig = self.make(overrides=None)
        self.assertFalse(rig.cfg["keepalive"]["enabled"])
        rig.messages(body())
        rig.records()
        self.assertEqual(Timer.made, [])

    def test_five_minute_mark_pings_at_270_seconds(self):
        self.real(body())
        self.assertEqual([t.seconds for t in self.live()], [270.0])

    def test_one_hour_mark_pings_at_3240_seconds(self):
        self.real(body(ttl="1h"))
        self.assertEqual([t.seconds for t in self.live()], [3240.0])

    def test_shortest_ttl_wins(self):
        b = body(ttl="1h")
        b["messages"] = [{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}]
        self.real(b)
        self.assertEqual(self.live()[0].seconds, 270.0)

    def test_no_marks_no_ping(self):
        b = body()
        del b["system"]
        self.real(b)
        self.assertEqual(Timer.made, [])

    def test_auxiliary_request_is_never_pinged(self):
        self.real(body(tools=False))
        self.assertEqual(Timer.made, [])

    def test_sub_agent_is_never_pinged(self):
        self.rig.messages(body(), session=SESSION, extra={"authorization": TOKEN, "x-claude-code-agent-id": "a1"})
        self.rig.records()
        self.assertEqual(Timer.made, [])

    def test_failed_request_is_not_kept(self):
        self.rig.upstream.respond = lambda req: (500, {"content-type": "application/json"}, b"{}")
        self.real()
        self.assertEqual(Timer.made, [])

    def test_new_request_rearms_the_lane(self):
        self.real()
        first = Timer.made[0]
        self.real()
        self.assertTrue(first.cancelled)
        self.assertEqual(len(self.live()), 1)

    def test_oldest_lane_is_evicted_over_the_limit(self):
        self.rig.cfg["keepalive"]["max_lanes"] = 2
        for n in range(3):
            self.real(body(text=f"conversation {n}"), session=f"{n}1111111-2222-3333-4444-555555555555")
        self.assertTrue(Timer.made[0].cancelled)
        self.assertEqual(len(self.live()), 2)
        self.assertEqual(len(self.rig.proxy.keepalive.lanes), 2)


class PingTest(Case):
    def test_ping_changes_only_stream_and_max_tokens(self):
        self.real()
        sent = self.rig.upstream.requests[0]["body"]
        self.fire()
        ping = self.rig.upstream.requests[1]
        self.assertEqual(ping["path"], self.rig.upstream.requests[0]["path"])
        self.assertEqual(ping["headers"]["authorization"], TOKEN)
        a, b = json.loads(sent), json.loads(ping["body"])
        self.assertEqual((b["stream"], b["max_tokens"]), (False, 1))
        a.update(stream=False, max_tokens=1)
        self.assertEqual(list(a), list(b))
        self.assertEqual(a, b)

    def test_ping_logged_as_keepalive_and_rearmed(self):
        self.real()
        self.fire()
        (ping,) = self.pings()
        self.assertEqual((ping["status"], ping["usage"]["cache_read_input_tokens"]), (200, 5000))
        self.assertEqual([t.seconds for t in Timer.made], [270.0, 270.0])  # the one that fired, and the next

    def test_thinking_budget_is_kept_and_max_tokens_stays_above_it(self):
        thinking = {"type": "enabled", "budget_tokens": 2048}
        self.assertEqual(ping_tokens({"thinking": thinking}), 2049)
        out = json.loads(ping_body(json.dumps(body(thinking=thinking, output_config={"effort": "low"})).encode()))
        self.assertEqual((out["max_tokens"], out["thinking"], out["output_config"]), (2049, thinking, {"effort": "low"}))
        self.assertEqual(ping_tokens({"thinking": {"type": "adaptive"}}), 1)

    def test_skips_while_a_real_request_is_in_flight(self):
        self.real()
        self.rig.proxy.keepalive.begin(next(iter(self.rig.proxy.keepalive.lanes)))
        timer = self.fire()
        self.assertEqual(len(self.rig.upstream.requests), 1)
        self.assertEqual((timer.cancelled, len(self.live())), (False, 2))
        self.assertEqual(self.live()[-1].seconds, 5)


class StopTest(Case):
    def stopped(self):
        return not self.rig.proxy.keepalive.lanes

    def test_429_stops_the_lane(self):
        self.real()
        self.ping_status = 429
        self.fire()
        self.assertTrue(self.stopped())
        self.assertEqual(len(self.live()), 0)

    def test_any_4xx_stops_the_lane(self):
        self.real()
        self.ping_status = 400
        self.fire()
        self.assertTrue(self.stopped())

    def test_a_real_429_stops_the_lane(self):
        self.real()
        self.rig.upstream.respond = lambda req: (429, {"content-type": "application/json"}, b"{}")
        self.real()
        self.assertTrue(self.stopped())

    def test_dead_cache_stops_the_lane(self):
        self.real()
        self.ping_usage = dict(USAGE, cache_read_input_tokens=0)
        self.fire()
        self.assertTrue(self.stopped())
        self.assertEqual(self.pings()[0]["usage"]["cache_read_input_tokens"], 0)

    def test_three_failures_in_a_row_stop_the_lane(self):
        self.real()
        self.ping_status = 500
        for _ in range(2):
            self.fire()
            self.assertFalse(self.stopped())
        self.fire()
        self.assertTrue(self.stopped())

    def test_a_success_resets_the_failure_count(self):
        self.real()
        self.ping_status = 500
        self.fire()
        self.fire()
        self.ping_status = 200
        self.fire()
        self.ping_status = 500
        self.fire()
        self.fire()
        self.assertFalse(self.stopped())

    def test_idle_cap_stops_the_lane_without_a_ping(self):
        self.real()
        self.clock.now += 241 * 60
        self.fire()
        self.assertTrue(self.stopped())
        self.assertEqual(len(self.rig.upstream.requests), 1)

    def test_shutdown_stops_every_lane(self):
        self.real()
        self.rig.proxy.keepalive.stop_all()
        self.assertTrue(self.stopped())
        self.assertTrue(Timer.made[0].cancelled)

    def test_an_old_timer_does_nothing_after_a_rearm(self):
        self.real()
        old = Timer.made[0]
        self.real()
        old.fn()
        self.assertEqual(len(self.rig.upstream.requests), 2)  # the two real requests only


class PrivacyTest(Case):
    def test_login_is_never_written_to_disk(self):
        self.real()
        self.fire()
        files = [self.rig.log_path] + [os.path.join(d, f) for d, _, names in os.walk(os.environ["ALMANAC_HOME"]) for f in names]
        for path in files:
            with open(path, "rb") as handle:
                self.assertNotIn(b"secret-login-do-not-store", handle.read(), path)
        self.assertNotIn("headers", self.pings()[0])


class MeterTest(unittest.TestCase):
    def test_keepalive_counts_in_usage_and_shows_apart(self):
        now = 1_000_000.0
        use = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 100, "output_tokens": 5}
        records = [{"kind": "messages", "ts": now - 10, "usage": use, "cache": {"marks": 1, "ttl": ["5m"]}},
                   {"kind": "keepalive", "ts": now - 5, "usage": USAGE}]
        day = report.meter(records, now)["24h"]
        self.assertEqual((day["requests"], day["keepalive_pings"], day["keepalive_read_tokens"]), (1, 1, 5000))
        self.assertEqual(day["cache_read_input_tokens"], 5100)
        text = "\n".join(report.render_meter(records, now))
        self.assertIn("keepalive  1 pings, cache read 5,000", text)
        self.assertNotIn("keepalive", "\n".join(report.render_meter(records[:1], now)))


if __name__ == "__main__":
    unittest.main()
