import json
import os
import unittest

from helpers import Rig, playbook
from test_playbook import ASK, Home, SetProject, body, call, human, make, result, said
from getalmanac import request as rq
from getalmanac.memory import Memory
from getalmanac.playbook_layer import PlaybookLayer
from getalmanac.store import Store

OLD = "refactor the parser module so it streams"
NEW = "now tidy the changelog wording a bit"


def nudges(sent):
    return [b["text"] for m in sent["messages"] for b in rq.blocks(m) if "has no playbook" in b.get("text", "")]


class NudgeTest(Home):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(self.home, "a.db"))
        Memory(self.store)  # makes the tables, the library's included, as the proxy's own store has them
        self.addCleanup(self.store.close)

    def rig(self, **overrides):
        rig = Rig(layers=[SetProject(""), PlaybookLayer()], store=self.store, overrides=overrides or None)
        self.addCleanup(rig.close)
        return rig

    def loop(self, count, names=()):
        names = list(names) + ["Bash"] * (count - len(names))
        return [m for i, n in enumerate(names) for m in (call(n, f"t{i}"), result(f"t{i}"))]

    def finish_turn(self, rig, first, count, names=()):
        """Send the first request of a turn, then the one after `count` tool calls, and return the next turn's first request."""
        rig.messages(body(human(first)))
        return self.loop(count, names)

    def next_turn(self, rig, first, count, names=(), second=NEW):
        loop = self.finish_turn(rig, first, count, names)
        rig.messages(body(human(first), *loop, said(), human(second)))
        return rig.sent()

    def test_a_long_turn_with_no_playbook_gets_the_sentence(self):
        sent = self.next_turn(self.rig(), OLD, 30)
        (text,) = nudges(sent)
        self.assertEqual(text, "<system-reminder>\nThe previous task took 30 tool calls and has no playbook. If it will come up again, "
                               "record it with the `playbook` skill once this request is answered.\n</system-reminder>")
        last = sent["messages"][-1]
        self.assertEqual(rq.blocks(last)[-1]["text"], NEW)  # typed text untouched and last
        self.assertEqual(nudges({"messages": sent["messages"][:-1]}), [])  # earlier messages carry nothing

    def test_not_after_a_short_turn(self):
        self.assertEqual(nudges(self.next_turn(self.rig(), OLD, 10)), [])

    def test_not_when_playbook_save_was_called(self):
        self.assertEqual(nudges(self.next_turn(self.rig(), OLD, 30, names=["mcp__almanac__playbook_save"])), [])

    def test_not_when_a_playbook_matched_that_turn(self):
        playbook.save(make())
        self.assertEqual(nudges(self.next_turn(self.rig(), ASK, 30)), [])

    def test_not_when_a_draft_or_a_candidate_already_covers_it(self):
        playbook.save(make(source="claude", triggers=["refactor parser"]))  # a draft: nobody approved it yet
        self.assertEqual(nudges(self.next_turn(self.rig(), OLD, 30)), [])
        self.assertEqual(len(nudges(self.next_turn(self.rig(), "write the release notes for friday", 30))), 1)  # the control
        from helpers import mine
        mine._save(mine.candidates_path(), {"candidates": [{"id": "c1", "tools": [], "examples": ["write the release notes for friday"]}]})
        self.assertEqual(nudges(self.next_turn(self.rig(), "write the release notes for friday again", 30)), [])

    def test_same_bytes_on_the_next_request_of_the_turn_and_after(self):
        rig = self.rig()
        loop = self.finish_turn(rig, OLD, 30)
        start = [human(OLD), *loop, said(), human(NEW)]
        rig.messages(body(*start))
        first = rig.sent()
        more = [call("Read", "u1"), result("u1")]
        rig.messages(body(*start, *more))
        second = rig.sent()
        self.assertEqual(second["messages"][:len(first["messages"])], first["messages"])
        self.assertEqual(len(nudges(second)), 1)
        playbook.save(make(triggers=["tidy", "changelog"]))  # frozen: a changed playbook does not alter the turn
        rig.messages(body(*start, *more, said(), human("and a third thing please")))
        third = rig.sent()
        self.assertEqual(third["messages"][:len(first["messages"])], first["messages"])  # the cached prefix holds
        self.assertEqual(len(nudges(third)), 1)

    def test_zero_turns_it_off(self):
        self.assertEqual(nudges(self.next_turn(self.rig(**{"playbooks.nudge_after": 0}), OLD, 30)), [])
        self.assertEqual(len(nudges(self.next_turn(self.rig(**{"playbooks.nudge_after": 5}), "another long job to do here", 6))), 1)

    def test_not_on_a_sub_agent_nor_when_inject_is_off(self):
        rig = self.rig()
        raw = json.dumps(body(human(OLD), *self.loop(30), said(), human(NEW))).encode()
        rig.messages(raw, extra={"x-claude-code-agent-id": "agent-7"})
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        self.assertEqual(nudges(self.next_turn(self.rig(**{"playbooks.inject": False}), "yet another long job here", 30)), [])
        self.assertEqual(nudges(self.next_turn(self.rig(**{"playbooks.enabled": False}), "one more long job here", 30)), [])


if __name__ == "__main__":
    unittest.main()
