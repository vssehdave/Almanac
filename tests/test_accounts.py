import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from helpers import Rig, sse

from getalmanac import accounts, cli

BODY = {"model": "claude-test", "messages": [{"role": "user", "content": "hello there"}]}
LIMIT = json.dumps({"type": "error", "error": {"type": "rate_limit_error",
                                               "message": "You've hit your usage limit."}}).encode()
PLAIN = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited, slow down"}}).encode()
A, B = "tok-personal-aaaa", "tok-work-bbbb"  # fake: no real token is ever used here


def folder(token, expires_in=3600):
    path = tempfile.mkdtemp(prefix="almanac-acct-")
    with open(os.path.join(path, ".credentials.json"), "w") as handle:
        json.dump({"claudeAiOauth": {"accessToken": token, "refreshToken": "r",
                                     "expiresAt": int((time.time() + expires_in) * 1000)}}, handle)
    return path


def bearer(req):
    return req["headers"]["authorization"].split(" ", 1)[1]


class TokenTest(unittest.TestCase):
    def test_valid_expired_and_broken(self):
        self.assertEqual(accounts.token(folder(A)), A)
        self.assertIsNone(accounts.token(folder(A, expires_in=30)))
        self.assertIsNone(accounts.token(tempfile.mkdtemp()))  # no file
        bad = tempfile.mkdtemp()
        for text in ("not json", "[]", '{"claudeAiOauth": {"accessToken": "x"}}'):
            with open(os.path.join(bad, ".credentials.json"), "w") as handle:
                handle.write(text)
            self.assertIsNone(accounts.token(bad))

    def test_is_limit(self):
        self.assertTrue(accounts.is_limit(429, LIMIT))
        self.assertTrue(accounts.is_limit(429, b'{"error": {"type": "usage_limit_reached"}}'))
        self.assertFalse(accounts.is_limit(429, PLAIN))
        self.assertFalse(accounts.is_limit(500, LIMIT))

    def test_is_limit_real_shapes(self):
        rejected = {"anthropic-ratelimit-unified-status": "rejected"}.get
        self.assertTrue(accounts.is_limit(429, b"{}", rejected))  # the header alone is enough
        self.assertFalse(accounts.is_limit(429, PLAIN, {"anthropic-ratelimit-unified-status": "allowed"}.get))
        self.assertTrue(accounts.is_limit(429, b'{"error": {"message": "Claude AI usage limit reached"}}'))
        self.assertTrue(accounts.is_limit(429, b'{"error": {"type": "Usage_Limit_Reached"}}'))


CFG = {"accounts": {"fallback": [], "cooldown_minutes": 30}}
NOW = time.mktime((2026, 3, 10, 12, 0, 0, 0, 0, -1))  # noon local, so "2:30am" is the next morning


def at(header=None, body=b"", now=NOW):
    return accounts.reset_at(CFG, (header or {}).get, body, now)


class ResetTest(unittest.TestCase):
    def test_unified_reset_header_wins(self):
        got = at({"anthropic-ratelimit-unified-reset": str(NOW + 7200), "retry-after": "60"}, b"resets 2:30am")
        self.assertEqual(got, NOW + 7200)

    def test_retry_after_next(self):
        self.assertEqual(at({"retry-after": "900"}, b"resets 2:30am"), NOW + 900)
        self.assertEqual(at({"retry-after": "Wed, 21 Oct"}, b""), NOW + 1800)  # not seconds: ignored

    def test_body_clock_time(self):
        got = time.localtime(at(body=b'{"message": "You hit your limit. Resets 2:30am"}'))
        self.assertEqual((got.tm_mday, got.tm_hour, got.tm_min), (11, 2, 30))
        got = time.localtime(at(body=b"resets 3pm"))  # later today
        self.assertEqual((got.tm_mday, got.tm_hour, got.tm_min), (10, 15, 0))
        got = time.localtime(at(body=b"resets at 12am"))
        self.assertEqual((got.tm_mday, got.tm_hour), (11, 0))

    def test_body_clock_time_with_zone(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("UTC")
        except Exception:
            self.skipTest("no zone data")
        got = at(body=b"resets 2:30am (UTC)")
        import datetime
        stamp = datetime.datetime.fromtimestamp(got, datetime.timezone.utc)
        self.assertEqual((stamp.hour, stamp.minute), (2, 30))
        self.assertTrue(NOW < got <= NOW + 86400)

    def test_cooldown_fallback(self):
        self.assertEqual(at(body=b"You hit your usage limit"), NOW + 1800)

    def test_nan_header_counts_as_missing(self):
        self.assertEqual(at({"anthropic-ratelimit-unified-reset": "nan", "retry-after": "900"}), NOW + 900)
        self.assertEqual(at({"anthropic-ratelimit-unified-reset": "inf"}), NOW + 1800)

    def test_clamp(self):
        self.assertEqual(at({"retry-after": "5"}), NOW + 60)
        self.assertEqual(at({"anthropic-ratelimit-unified-reset": str(NOW - 500)}), NOW + 60)
        self.assertEqual(at({"anthropic-ratelimit-unified-reset": str(NOW + 99 * 86400)}), NOW + 7 * 86400)


class OtherUpstreamTest(unittest.TestCase):
    def test_a_usage_limit_is_passed_through_and_no_other_token_is_read(self):
        accounts.reset()
        self.addCleanup(accounts.reset)
        other = folder(B)
        rig = Rig(lambda req: (429, {"content-type": "application/json", "retry-after": "60"}, LIMIT),
                  overrides={"accounts.fallback": [other]})  # the fake upstream is a local gateway, not api.anthropic.com
        self.addCleanup(rig.close)
        with mock.patch.object(accounts, "token", wraps=accounts.token) as read:
            status, headers, data = rig.messages(BODY, extra={"authorization": f"Bearer {A}"})
        read.assert_not_called()  # no other login's token was read
        self.assertEqual((status, data, headers["retry-after"]), (429, LIMIT, "60"))
        self.assertEqual([bearer(r) for r in rig.upstream.requests], [A])


    def test_only_the_anthropic_api_over_https_is_allowed(self):
        from types import SimpleNamespace as P
        self.assertTrue(accounts.allowed(P(secure=True, host="api.anthropic.com", port=443)))
        for bad in (P(secure=False, host="api.anthropic.com", port=443), P(secure=True, host="api.anthropic.com.evil.test", port=443),
                    P(secure=True, host="gateway.example.com", port=443), P(secure=True, host="api.anthropic.com", port=8443)):
            self.assertFalse(accounts.allowed(bad))


class FallbackTest(unittest.TestCase):
    def setUp(self):
        accounts.reset()
        self.addCleanup(accounts.reset)
        self.err = io.StringIO()  # the one-time notice goes to stderr
        patch = mock.patch("sys.stderr", self.err)
        patch.start()
        self.addCleanup(patch.stop)
        self.personal, self.work = folder(A), folder(B)
        self.allowed = mock.patch.object(accounts, "allowed", return_value=True)  # the fake upstream stands in for api.anthropic.com
        self.allowed.start()
        self.addCleanup(self.allowed.stop)
        self.limited = {A}  # which tokens the fake upstream answers with a usage limit
        self.reply = LIMIT

    def rig(self, fallback=None, **kw):
        def respond(req):
            if bearer(req) in self.limited:
                return 429, {"content-type": "application/json", "retry-after": "60"}, self.reply
            return 200, {"content-type": "text/event-stream"}, sse()
        overrides = {"accounts.fallback": [self.work, self.personal] if fallback is None else fallback}
        rig = Rig(respond, overrides=overrides, **kw)
        self.addCleanup(rig.close)
        return rig

    def send(self, rig, token=A):
        return rig.messages(BODY, extra={"authorization": f"Bearer {token}"})

    def test_switch_on_usage_limit_then_straight_to_alternative(self):
        rig = self.rig()
        status, _, _ = self.send(rig)
        self.assertEqual(status, 200)
        self.assertEqual([bearer(r) for r in rig.upstream.requests], [A, B])
        self.assertEqual(rig.upstream.requests[0]["body"], rig.upstream.requests[1]["body"])
        self.assertEqual(self.send(rig)[0], 200)
        self.assertEqual([bearer(r) for r in rig.upstream.requests], [A, B, B])  # no second 429
        records = rig.records()
        name = os.path.basename(self.work)
        self.assertEqual({k: v for k, v in records[0]["account"].items() if k != "until"},
                         {"switched": name, "reason": "usage limit"})
        self.assertAlmostEqual(records[0]["account"]["until"], time.time() + 60, delta=10)  # retry-after: 60
        self.assertEqual(records[1]["account"]["switched"], name)

    def test_other_headers_untouched_by_swap(self):
        rig = self.rig()
        self.send(rig)
        first, second = (r["headers"] for r in rig.upstream.requests)
        first.pop("authorization"), second.pop("authorization")
        self.assertEqual(first, second)

    def test_plain_rate_limit_passes_through(self):
        self.reply = PLAIN
        rig = self.rig()
        status, headers, data = self.send(rig)
        self.assertEqual((status, data, headers["retry-after"]), (429, PLAIN, "60"))
        self.assertEqual(len(rig.upstream.requests), 1)
        self.assertNotIn("account", rig.records()[0])

    def test_off_means_untouched(self):
        rig = self.rig(fallback=[])
        status, headers, data = self.send(rig)
        self.assertEqual((status, data, headers["retry-after"]), (429, LIMIT, "60"))
        self.assertEqual(len(rig.upstream.requests), 1)

    def test_expired_alternative_returns_original(self):
        rig = self.rig(fallback=[folder(B, expires_in=10), self.personal])
        status, headers, data = self.send(rig)
        self.assertEqual((status, data, headers["retry-after"]), (429, LIMIT, "60"))
        self.assertEqual(len(rig.upstream.requests), 1)

    def test_both_out_returns_original(self):
        self.limited = {A, B}
        rig = self.rig()
        status, headers, data = self.send(rig)
        self.assertEqual((status, data, headers["retry-after"]), (429, LIMIT, "60"))
        self.assertEqual([bearer(r) for r in rig.upstream.requests], [A, B])
        self.assertNotIn("account", rig.records()[0])
        self.send(rig)  # both are cooling now: no further tries
        self.assertEqual(len(rig.upstream.requests), 3)

    def test_first_account_tried_again_after_cooldown(self):
        rig = self.rig()
        self.send(rig)
        self.limited = set()  # the plan renewed
        later = time.time() + 31 * 60
        with mock.patch("getalmanac.accounts.time.time", return_value=later):
            self.send(rig)
        self.assertEqual(bearer(rig.upstream.requests[-1]), A)

    def test_first_account_stays_skipped_until_its_reset(self):
        rig = self.rig()
        self.send(rig)  # the reply says retry-after 60
        self.limited = set()
        with mock.patch("getalmanac.accounts.time.time", return_value=time.time() + 30):
            self.send(rig)
        self.assertEqual(bearer(rig.upstream.requests[-1]), B)
        self.limited = {A}  # back too early: still refused, the new reset time replaces the old
        with mock.patch("getalmanac.accounts.time.time", return_value=time.time() + 120):
            self.assertEqual(self.send(rig)[0], 200)
        self.assertEqual([bearer(r) for r in rig.upstream.requests[-2:]], [A, B])
        self.assertEqual(rig.records()[-1]["account"]["until"] > time.time() + 100, True)

    def test_notice_once_on_stderr(self):
        rig = self.rig()
        self.send(rig)
        self.send(rig)
        text = self.err.getvalue()
        self.assertEqual(text.count("is out of usage"), 1)
        self.assertIn(os.path.basename(self.work), text)
        self.assertRegex(text, r"until \d\d:\d\d")

    def test_token_never_logged(self):
        rig = self.rig()
        self.send(rig)
        self.send(rig)
        records = rig.records()
        with open(rig.log_path, encoding="utf-8") as handle:
            raw = handle.read()
        for tok in (A, B):
            self.assertNotIn(tok, raw)
            self.assertNotIn(tok, json.dumps(records))

    def test_until_is_the_spent_logins_reset(self):
        rig = self.rig(fallback=[self.work])  # the spent login is not in the list
        self.send(rig)
        until = rig.records()[0]["account"]["until"]
        self.assertAlmostEqual(until, time.time() + 60, delta=10)  # retry-after: 60

    def test_header_only_limit_switches(self):
        self.reply = b"{}"
        rig = self.rig()
        orig = rig.upstream.respond
        rig.upstream.respond = lambda req: (429, {"anthropic-ratelimit-unified-status": "rejected"}, b"{}")             if bearer(req) == A else orig(req)
        self.assertEqual(self.send(rig)[0], 200)
        self.assertEqual([bearer(r) for r in rig.upstream.requests], [A, B])

    def test_status_line_record_without_ts(self):
        cfg = {"accounts": {"fallback": [self.work], "cooldown_minutes": 30}}
        accounts.status_line(cfg, [{"account": {"switched": "w", "reason": "x"}}])  # must not raise

    def test_status_line(self):
        cfg = {"accounts": {"fallback": [self.work, folder(B, expires_in=5)], "cooldown_minutes": 30}}
        line = accounts.status_line(cfg, [{"ts": time.time(), "account": {"switched": "w", "reason": "x"}}])
        self.assertIn("(valid)", line)
        self.assertIn("no valid token", line)
        self.assertIn("in use: w", line)
        self.assertIn("cooling down until", line)
        self.assertTrue(callable(cli._status))


if __name__ == "__main__":
    unittest.main()
