import threading
import time
import unittest

from helpers import SESSION, Rig, sse

from getalmanac import swarm


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def spawn(gate, key, out, wait=10.0, cap=30.0):
    t = threading.Thread(target=lambda: out.append(gate.enter(key, wait=wait, cap=cap)), daemon=True)
    t.start()
    return t


def join_all(threads):
    for t in threads:
        t.join(5)
        if t.is_alive():
            raise AssertionError("thread still blocked after 5s")


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


class SwarmGate(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = swarm.Gate(clock=self.clock)

    def waiters(self, key="k"):
        return self.gate._entries[key]["waiters"]

    def test_first_enter_leads_and_other_key_does_not_wait(self):
        out = []
        threads = [spawn(self.gate, "a", out), spawn(self.gate, "b", out)]
        join_all(threads)
        self.assertEqual([t.mode for t in out], ["lead", "lead"])

    def test_waiters_released_together_by_done_true(self):
        lead = self.gate.enter("k")
        out = []
        threads = [spawn(self.gate, "k", out) for _ in range(3)]
        wait_for(lambda: self.waiters() == 3)
        lead.done(True)
        join_all(threads)
        self.assertEqual(len(out), 3)
        for t in out:
            self.assertTrue(t.mode.startswith("waited:"), t.mode)

    def test_warm_for_270_seconds_then_lead_again(self):
        lead = self.gate.enter("k")
        lead.done(True)
        self.assertEqual(self.gate.enter("k").mode, "warm")
        self.clock.now += 271
        self.assertEqual(self.gate.enter("k").mode, "lead")

    def test_done_false_hands_leadership_to_one_waiter(self):
        lead = self.gate.enter("k")
        out = []
        threads = [spawn(self.gate, "k", out) for _ in range(3)]
        wait_for(lambda: self.waiters() == 3)
        lead.done(False)
        wait_for(lambda: any(t.mode == "lead" for t in out))
        wait_for(lambda: self.waiters() == 2)
        self.assertEqual(len(out), 1)
        new_lead = out[0]
        self.assertEqual(new_lead.mode, "lead")
        new_lead.done(True)
        join_all(threads)
        self.assertEqual(sorted(t.mode.split(":")[0] for t in out), ["lead", "waited", "waited"])

    def test_stalled_leader_is_deposed_by_one_waiter(self):
        self.gate.enter("k", wait=0.2, cap=5.0)
        out = []
        threads = [spawn(self.gate, "k", out, wait=0.2, cap=5.0) for _ in range(2)]
        wait_for(lambda: self.waiters() == 2)
        self.clock.now = 1.0
        wait_for(lambda: any(t.mode == "lead" for t in out))
        self.assertEqual([t.mode for t in out].count("lead"), 1)
        out[[t.mode for t in out].index("lead")].done(True)
        join_all(threads)

    def test_deposed_leader_late_done_false_does_not_clear_new_leader(self):
        old = self.gate.enter("k", wait=0.2, cap=5.0)
        out = []
        threads = [spawn(self.gate, "k", out, wait=0.2, cap=5.0) for _ in range(2)]
        wait_for(lambda: self.waiters() == 2)
        self.clock.now = 1.0
        wait_for(lambda: any(t.mode == "lead" for t in out))
        old.done(False)
        extra_out = []
        extra = spawn(self.gate, "k", extra_out, wait=0.2, cap=5.0)
        wait_for(lambda: self.waiters() == 2)
        self.assertTrue(extra.is_alive())
        self.assertEqual(extra_out, [])
        out[[t.mode for t in out].index("lead")].done(True)
        join_all(threads + [extra])
        self.assertTrue(extra_out[0].mode.startswith("waited:"), extra_out[0].mode)

    def test_deposed_leader_late_done_true_releases_and_marks_warm(self):
        old = self.gate.enter("k", wait=0.2, cap=5.0)
        out = []
        threads = [spawn(self.gate, "k", out, wait=0.2, cap=5.0) for _ in range(2)]
        wait_for(lambda: self.waiters() == 2)
        self.clock.now = 1.0
        wait_for(lambda: any(t.mode == "lead" for t in out))
        old.done(True)
        join_all(threads)
        waited = [t.mode for t in out if t.mode != "lead"]
        self.assertEqual(len(waited), 1)
        self.assertTrue(waited[0].startswith("waited:"), waited[0])
        self.assertEqual(self.gate.enter("k").mode, "warm")

    def test_done_twice_is_harmless(self):
        lead = self.gate.enter("k")
        lead.done(True)
        lead.done(True)
        self.assertEqual(self.gate.enter("k").mode, "warm")

    def test_done_false_twice_hands_off_only_once(self):
        lead = self.gate.enter("k")
        out = []
        threads = [spawn(self.gate, "k", out) for _ in range(2)]
        wait_for(lambda: self.waiters() == 2)
        lead.done(False)
        lead.done(False)
        wait_for(lambda: any(t.mode == "lead" for t in out))
        wait_for(lambda: self.waiters() == 1)
        self.assertEqual(len(out), 1)
        out[0].done(True)
        join_all(threads)

    def test_waiter_times_out_at_cap(self):
        lead = self.gate.enter("k", wait=100, cap=0.6)
        out = []
        threads = [spawn(self.gate, "k", out, wait=100, cap=0.6)]
        wait_for(lambda: self.waiters() == 1)
        self.clock.now = 1.0
        join_all(threads)
        self.assertEqual(out[0].mode, "timeout")
        lead.done(True)

    def test_idle_entries_are_evicted(self):
        self.gate.enter("a").done(True)
        self.clock.now += swarm.WARM_SECONDS + 1
        self.gate.enter("b").done(True)
        self.assertEqual(self.gate.size(), 1)


def sub(agent):
    return {"x-claude-code-request-class": "subagent", "x-claude-code-agent-id": agent}


SUB_BODY = {"model": "claude-test", "system": [{"type": "text", "text": "You are Claude Code"}],
            "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "find the bug"}]}]}


class ProxyGate(unittest.TestCase):
    def messages_modes(self, rig):
        return [r.get("swarm") for r in rig.records() if r.get("kind") == "messages"]

    def test_second_sub_agent_is_sent_only_after_first_reply_starts(self):
        release = threading.Event()

        def hold(req):
            release.wait(10)
            return 200, {"content-type": "text/event-stream"}, sse()

        rig = Rig(respond=hold)
        self.addCleanup(rig.close)
        out = []
        threads = [threading.Thread(target=lambda a=a: out.append(rig.messages(SUB_BODY, extra=sub(a)))) for a in ("a", "b")]
        threads[0].start()
        wait_for(lambda: len(rig.upstream.requests) == 1)
        threads[1].start()
        wait_for(lambda: any(e["waiters"] == 1 for e in rig.proxy.gate._entries.values()))
        self.assertEqual(len(rig.upstream.requests), 1)  # the second is held while the first has no reply yet
        release.set()
        join_all(threads)
        self.assertEqual([status for status, _, _ in out], [200, 200])
        self.assertEqual(len(rig.upstream.requests), 2)
        modes = sorted(m.split(":")[0] for m in self.messages_modes(rig))
        self.assertEqual(modes, ["lead", "waited"])

    def test_leader_error_still_sends_the_waiter_and_clears_the_lead(self):
        release = threading.Event()
        error = b'{"type":"error","error":{"type":"api_error","message":"boom"}}'

        def respond(req):
            if len(rig.upstream.requests) == 1:
                release.wait(10)
                return 500, {"content-type": "application/json"}, error
            return 200, {"content-type": "text/event-stream"}, sse()

        rig = Rig(respond=respond)
        self.addCleanup(rig.close)
        out = []
        threads = [threading.Thread(target=lambda a=a: out.append((a, rig.messages(SUB_BODY, extra=sub(a))))) for a in ("a", "b")]
        threads[0].start()
        wait_for(lambda: len(rig.upstream.requests) == 1)
        threads[1].start()
        wait_for(lambda: any(e["waiters"] == 1 for e in rig.proxy.gate._entries.values()))
        release.set()
        join_all(threads)
        replies = dict(out)
        self.assertEqual(replies["a"][0], 500)
        self.assertEqual(replies["a"][2], error)
        self.assertEqual(replies["b"][0], 200)
        self.assertEqual(len(rig.upstream.requests), 2)
        wait_for(lambda: all(e["leader_gen"] is None for e in rig.proxy.gate._entries.values()))
        self.assertEqual(len(self.messages_modes(rig)), 2)

    def test_swarm_wait_zero_sets_no_swarm_stat(self):
        rig = Rig(overrides={"startup.swarm_wait_seconds": 0})
        self.addCleanup(rig.close)
        rig.messages(SUB_BODY, extra=sub("a"))
        modes = self.messages_modes(rig)
        self.assertEqual(len(modes), 1)
        self.assertIsNone(modes[0])

    def test_count_tokens_sets_no_swarm_stat(self):
        rig = Rig(respond=lambda req: (200, {"content-type": "application/json"}, b'{"input_tokens":5}'))
        self.addCleanup(rig.close)
        headers = {"content-type": "application/json", "authorization": "Bearer test-token",
                   "x-claude-code-session-id": SESSION, **sub("a")}
        status = rig.request("POST", "/v1/messages/count_tokens", SUB_BODY, headers)[0]
        rig.expected += 1
        self.assertEqual(status, 200)
        recs = [r for r in rig.records() if r.get("kind") == "count_tokens"]
        self.assertEqual(len(recs), 1)
        self.assertNotIn("swarm", recs[0])


if __name__ == "__main__":
    unittest.main()
