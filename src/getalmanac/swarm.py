"""Let one request per prompt prefix go first so parallel sub-agents read its cache write."""
import threading
import time

WARM_SECONDS = 270


class Ticket:
    def __init__(self, mode, gate=None, entry=None, gen=None):
        self.mode = mode
        self._gate = gate
        self._entry = entry
        self._gen = gen
        self._finished = False

    def done(self, ok):
        if self._entry is None:
            return
        gate = self._gate
        with gate._cond:
            if self._finished:
                return
            self._finished = True
            entry = self._entry
            if ok:
                # A deposed leader's late success still warms the prefix.
                entry["warm_until"] = gate._clock() + WARM_SECONDS
            if entry["leader_gen"] == self._gen:
                entry["leader_gen"] = None
                gate._cond.notify_all()
            elif ok:
                gate._cond.notify_all()


class Gate:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._entries = {}

    def enter(self, key, wait=10.0, cap=30.0):
        with self._cond:
            start = now = self._clock()
            for k in [k for k, e in self._entries.items()
                      if e["leader_gen"] is None and not e["waiters"] and e["warm_until"] <= now]:
                del self._entries[k]
            entry = self._entries.setdefault(key, {"gen": 0, "leader_gen": None, "lead_since": 0.0,
                                                   "warm_until": 0.0, "waiters": 0})
            waited = False
            while True:
                now = self._clock()
                if now < entry["warm_until"]:
                    return Ticket(f"waited:{int((now - start) * 1000)}" if waited else "warm")
                if entry["leader_gen"] is None or now - entry["lead_since"] >= wait:
                    entry["gen"] += 1
                    entry["leader_gen"] = entry["gen"]
                    entry["lead_since"] = now
                    return Ticket("lead", self, entry, entry["gen"])
                if now - start >= cap:
                    return Ticket("timeout")
                entry["waiters"] += 1
                self._cond.wait(max(0.01, min(entry["lead_since"] + wait, start + cap) - now))
                entry["waiters"] -= 1
                waited = True

    def size(self) -> int:
        with self._cond:
            return len(self._entries)
