import threading
import time
import unittest

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


def settle(secs=0.1):
    time.sleep(secs)


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
        time.sleep(0.02)


class SwarmGate(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = swarm.Gate(clock=self.clock)

    def test_first_enter_leads_and_other_key_does_not_wait(self):
        out = []
        threads = [spawn(self.gate, "a", out), spawn(self.gate, "b", out)]
        join_all(threads)
        self.assertEqual([t.mode for t in out], ["lead", "lead"])

    def test_waiters_released_together_by_done_true(self):
        lead = self.gate.enter("k")
        out = []
        threads = [spawn(self.gate, "k", out) for _ in range(3)]
        settle()
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
        settle()
        lead.done(False)
        wait_for(lambda: any(t.mode == "lead" for t in out))
        settle()
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
        settle()
        self.clock.now = 1.0
        wait_for(lambda: any(t.mode == "lead" for t in out))
        settle()
        self.assertEqual([t.mode for t in out].count("lead"), 1)
        out[[t.mode for t in out].index("lead")].done(True)
        join_all(threads)

    def test_deposed_leader_late_done_false_does_not_clear_new_leader(self):
        old = self.gate.enter("k", wait=0.2, cap=5.0)
        out = []
        threads = [spawn(self.gate, "k", out, wait=0.2, cap=5.0) for _ in range(2)]
        settle()
        self.clock.now = 1.0
        wait_for(lambda: any(t.mode == "lead" for t in out))
        settle()
        old.done(False)
        extra_out = []
        extra = spawn(self.gate, "k", extra_out, wait=0.2, cap=5.0)
        settle(0.3)
        self.assertTrue(extra.is_alive())
        self.assertEqual(extra_out, [])
        out[[t.mode for t in out].index("lead")].done(True)
        join_all(threads + [extra])
        self.assertTrue(extra_out[0].mode.startswith("waited:"), extra_out[0].mode)

    def test_deposed_leader_late_done_true_releases_and_marks_warm(self):
        old = self.gate.enter("k", wait=0.2, cap=5.0)
        out = []
        threads = [spawn(self.gate, "k", out, wait=0.2, cap=5.0) for _ in range(2)]
        settle()
        self.clock.now = 1.0
        wait_for(lambda: any(t.mode == "lead" for t in out))
        settle()
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
        settle()
        lead.done(False)
        lead.done(False)
        wait_for(lambda: any(t.mode == "lead" for t in out))
        settle()
        self.assertEqual(len(out), 1)
        out[0].done(True)
        join_all(threads)

    def test_waiter_times_out_at_cap(self):
        lead = self.gate.enter("k", wait=100, cap=0.6)
        out = []
        threads = [spawn(self.gate, "k", out, wait=100, cap=0.6)]
        settle()
        self.clock.now = 1.0
        join_all(threads)
        self.assertEqual(out[0].mode, "timeout")
        lead.done(True)

    def test_idle_entries_are_evicted(self):
        self.gate.enter("a").done(True)
        self.clock.now += swarm.WARM_SECONDS + 1
        self.gate.enter("b").done(True)
        self.assertEqual(self.gate.size(), 1)


if __name__ == "__main__":
    unittest.main()
