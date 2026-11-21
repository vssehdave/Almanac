"""Opt-in prompt-cache keepalive: a tiny request that reads a paused conversation's cached prefix.

A cache entry lives for its ttl (5 minutes or 1 hour) counted from its last read. While a conversation
is paused, a request that repeats the last body with `stream` off and almost no output reads the prefix
and starts the ttl again, for about a tenth of the prefix's price, instead of a 1.25 to 2 times rewrite
when the person comes back. Only the main thread and sub-agents are kept alive, never titles or classifiers.

What is kept: the exact body sent upstream and the headers it went out with, per lane, in memory only.
They are never written to disk or to the log, and are gone when the proxy exits.

A lane stops being pinged on: a 429 (a usage limit: never ping into it), any other 4xx, 3 failures in
a row, a reply that read nothing from the cache (the entry is already dead), `max_idle_minutes` since its
last real request, or shutdown. A tick is skipped while a real request on the lane is in flight.
"""
import json
import threading
import time
from collections import OrderedDict

from . import request as rq
from .report import TTL_SECONDS, cache_marks

FACTOR = 0.9  # ping at this share of the shortest ttl, so the entry is read before it lapses
MAX_FAILS = 3
BUSY_RETRY = 5  # seconds before trying again when a real request is in flight
PING_TIMEOUT = 60


def delay_for(ttls):
    """Seconds until the ping: 0.9 x the shortest ttl of the body's cache marks, or None when it has none."""
    return FACTOR * min(TTL_SECONDS.get(t, 300) for t in ttls) if ttls else None


def ping_tokens(body) -> int:
    """The smallest max_tokens that leaves the request valid and its cached prefix intact.

    max_tokens is not part of the cached prefix (the thinking and effort settings are, so those are never touched).
    0 would be cheaper, but the API rejects it with extended thinking, a forced tool_choice or structured
    output, so 1 is used. Extended thinking with a budget needs max_tokens above that budget."""
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled" and isinstance(thinking.get("budget_tokens"), int):
        return thinking["budget_tokens"] + 1
    return 1


def ping_body(sent: bytes) -> bytes:
    """The body kept, with only `stream` and `max_tokens` changed."""
    body = json.loads(sent)
    body["stream"] = False
    body["max_tokens"] = ping_tokens(body)
    return rq.dumps(body)


class Entry:
    def __init__(self, lane, body, headers, path, ttls, now):
        self.lane, self.body, self.headers, self.path, self.ttls = lane, body, headers, path, ttls
        self.armed, self.fails, self.timer, self.pings = now, 0, None, 0
        self.session, self.model = "", ""


class Keepalive:
    def __init__(self, proxy, clock=time.time, timer=threading.Timer):
        self.proxy, self.clock, self.timer = proxy, clock, timer
        self.lanes = OrderedDict()  # lane -> Entry, least recently armed first
        self.busy = {}  # lane -> real requests in flight
        self.lock = threading.RLock()

    def settings(self):
        cfg = (self.proxy.cfg.get("keepalive") or {})
        return cfg.get("enabled") is True, cfg.get("max_idle_minutes", 240) * 60, max(1, cfg.get("max_lanes", 4))

    def begin(self, lane):
        if lane:
            with self.lock:
                self.busy[lane] = self.busy.get(lane, 0) + 1

    def end(self, lane):
        if lane:
            with self.lock:
                self.busy[lane] = max(0, self.busy.get(lane, 0) - 1)

    def arm(self, ctx, sent, headers, path, status):
        """After a real request answered: remember its body and schedule the ping. Never raises."""
        try:
            on, _, limit = self.settings()
            if not on or not ctx.lane or ctx.kind != "messages" or ctx.thread not in ("main", "sub", "fork"):
                return
            if status == 429:  # a usage limit: do not ping into it
                return self.drop(ctx.lane)
            if not 200 <= status < 300:
                return
            parsed = json.loads(sent)
            ttls = cache_marks(parsed)["ttl"]
            if not ttls:
                return self.drop(ctx.lane)
            entry = Entry(ctx.lane, sent, dict(headers), path, ttls, self.clock())
            entry.session, entry.model = ctx.session, parsed.get("model") or ctx.model  # the model routing sent, which the ping reads
            with self.lock:
                self.drop(ctx.lane)
                while len(self.lanes) >= limit:
                    self.drop(next(iter(self.lanes)))
                self.lanes[ctx.lane] = entry
                self._schedule(entry, delay_for(ttls))
        except Exception:
            pass

    def _schedule(self, entry, seconds):
        entry.timer = self.timer(seconds, lambda: self.tick(entry))
        entry.timer.daemon = True
        entry.timer.start()

    def drop(self, lane):
        with self.lock:
            entry = self.lanes.pop(lane, None)
        if entry and entry.timer:
            entry.timer.cancel()

    def stop_all(self):
        for lane in list(self.lanes):
            self.drop(lane)

    def tick(self, entry):
        """One scheduled ping for one lane."""
        try:
            with self.lock:
                if self.lanes.get(entry.lane) is not entry:  # re-armed or stopped since this timer was set
                    return
                if self.clock() - entry.armed > self.settings()[1]:
                    return self.drop(entry.lane)
                if self.busy.get(entry.lane):  # the real request refreshes the cache itself
                    return self._schedule(entry, BUSY_RETRY)
            status, usage = self._send(entry)
            with self.lock:
                if self.lanes.get(entry.lane) is not entry:
                    return
                entry.pings += 1
                self._log(entry, status, usage)
                if status and 400 <= status < 500:  # 429 and every other refusal alike
                    return self.drop(entry.lane)
                if status and 200 <= status < 300:
                    entry.fails = 0
                    if not (usage or {}).get("cache_read_input_tokens"):  # nothing was read: the entry is already gone
                        return self.drop(entry.lane)
                else:
                    entry.fails += 1
                    if entry.fails >= MAX_FAILS:
                        return self.drop(entry.lane)
                self._schedule(entry, delay_for(entry.ttls))
        except Exception:
            self.drop(entry.lane)

    def _send(self, entry):
        """(status, usage) straight to the upstream, through the same corporate proxy and CA settings. (0, None) on a network error."""
        conn = None
        try:
            conn = self.proxy.connect()
            conn.timeout = PING_TIMEOUT
            body = ping_body(entry.body)
            conn.putrequest("POST", self.proxy.base + entry.path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", self.proxy.netloc)
            for key, value in entry.headers.items():
                conn.putheader(key, value)
            conn.putheader("Accept-Encoding", "identity")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            data = resp.read()
            try:
                usage = json.loads(data).get("usage")
            except (ValueError, AttributeError):
                usage = None
            return resp.status, usage if isinstance(usage, dict) else None
        except Exception:
            return 0, None
        finally:
            if conn:
                conn.close()

    def _log(self, entry, status, usage):
        record = {"ts": round(self.clock(), 3), "kind": "keepalive", "session": entry.session, "lane": entry.lane,
                  "model": entry.model, "status": status}
        if usage:
            record["usage"] = {k: v for k, v in usage.items() if isinstance(v, int)}
        self.proxy.log.write(record)
