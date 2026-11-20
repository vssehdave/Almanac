"""Route-based playbook offers, playbooks that include playbooks, structured results and one playbook per route."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import Rig, playbook
from test_playbook import SetProject, Home, body, human, make, result

from getalmanac import request as rq
from getalmanac.playbook_layer import PlaybookLayer
from getalmanac.store import Store

SHIP = ["test", "commit", "push", "pr", "ci", "merge"]


def use(command, text="", ident="t1", name="Bash", args=None):
    """An assistant message: optional narration, then one tool call."""
    content = [{"type": "text", "text": text}] if text else []
    content.append({"type": "tool_use", "id": ident, "name": name, "input": args if args is not None else {"command": command}})
    return {"role": "assistant", "content": content}


def offers(msg):
    return [b["text"] for b in rq.blocks(msg) if b.get("type") == "text" and "covers the rest of this" in b.get("text", "")]


def ship(**over):
    return make("ship-change", title="Ship a change", triggers=["committing and merging"], route=SHIP, skills=[], agents=[],
                args=[{"name": "--message", "help": "commit message"}], **over)


class RouteTest(Home):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(self.home, "a.db"))
        self.addCleanup(self.store.close)
        self.ahead = mock.patch("getplaybook.offer._ahead", return_value=True)
        self.ahead.start()
        self.addCleanup(self.ahead.stop)

    def rig(self):
        rig = Rig(layers=[SetProject(""), PlaybookLayer()], store=self.store)
        self.addCleanup(rig.close)
        return rig

    def send(self, rig, payload, extra=None, session="s1"):
        rig.messages(payload, session=session, extra=extra)
        return rig.sent()

    def ladder(self, say="Committing and merging", prompt="Continue"):
        return body(human(prompt), use("python -m unittest", "Running the tests", "t1"), result("t1"),
                    use("git commit -m done", say, "t2"), result("t2"))

    def test_tool_calls_and_narration_offer_the_playbook_with_a_useless_prompt(self):
        playbook.save(ship(), script="print(1)\n")
        playbook.approve("ship-change")
        rig = self.rig()
        sent = self.send(rig, self.ladder())
        (note,) = offers(sent["messages"][-1])
        self.assertEqual(rq.blocks(sent["messages"][-1])[-1]["text"], note)  # at the end of the newest message
        self.assertIn('playbook "ship-change" covers the rest of this: call playbook_run {"name": "ship-change", "args": [<--message>]}', note)
        self.assertIn("--message: commit message", note)
        self.assertEqual([offers(m) for m in sent["messages"][:-1]], [[]] * 4)
        (record,) = [r for r in rig.records() if "playbook_offer" in r]
        self.assertEqual((record["playbook_offer"], record["signals"]), ("ship-change", ["route", "narration", "repo"]))

    def test_one_signal_alone_is_not_an_offer(self):
        playbook.save(ship(), script="print(1)\n")
        playbook.approve("ship-change")
        rig = self.rig()
        with mock.patch("getplaybook.offer._ahead", return_value=False):
            for payload in (self.ladder(say="Ok"),  # the route alone
                            body(human("Continue"), use("ls", "Committing and merging", "t1"), result("t1"))):  # the narration alone
                self.assertEqual(offers(self.send(rig, payload, session=str(len(payload["messages"])))["messages"][-1]), [])

    def test_the_offer_is_byte_stable_on_a_retry_and_stays_put_as_the_turn_goes_on(self):
        playbook.save(ship(), script="print(1)\n")
        playbook.approve("ship-change")
        rig = self.rig()
        payload = self.ladder()
        self.send(rig, payload)
        first = rig.upstream.requests[-1]["body"]
        with mock.patch("getplaybook.offer._ahead", return_value=False):  # the facts moved on: the decision does not
            self.send(rig, payload)
        self.assertEqual(rig.upstream.requests[-1]["body"], first)
        later = body(*payload["messages"], use("git push", "", "t3"), result("t3"))
        sent = self.send(rig, later)
        self.assertEqual(json.dumps(sent["messages"][4]), json.dumps(json.loads(first)["messages"][4]))  # the cached prefix is untouched
        self.assertEqual(sum(len(offers(m)) for m in sent["messages"]), 1)  # once per turn

    def test_an_earlier_turns_offer_stays_when_the_next_message_is_typed(self):
        playbook.save(ship(), script="print(1)\n")
        playbook.approve("ship-change")
        rig = self.rig()
        payload = self.ladder()
        self.send(rig, payload)
        first = json.loads(rig.upstream.requests[-1]["body"])["messages"]
        sent = self.send(rig, body(*payload["messages"], {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}, human("next, the docs please")))
        self.assertEqual(json.dumps(sent["messages"][:5]), json.dumps(first))  # the cached prefix, offer included, is byte for byte

    def test_a_route_written_as_a_set_of_phases_matches_in_any_order(self):
        playbook.save(make("ship-old", triggers=["committing and merging"], phases=["branch", "commit", "edit", "pr", "push", "test"],
                           skills=[], agents=[]))  # the miner stored the phases sorted
        sent = self.send(self.rig(), self.ladder())
        self.assertEqual(len(offers(sent["messages"][-1])), 1)

    def test_a_sub_agent_playbook_is_offered_only_to_its_type_and_never_to_main(self):
        playbook.save(make("verify", title="Run the tests", triggers=["running the tests"], route=["test"], agent_type="verifier", skills=[], agents=[]))
        playbook.save(make("main-verify", title="Run the tests", triggers=["running the tests"], route=["test"], skills=[], agents=[]))
        rig = self.rig()
        payload = body(human("Check the change in the billing module"), use("python -m unittest", "Running the tests", "t1"), result("t1"))

        def names(extra, session):
            return [n for m in self.send(rig, payload, extra, session)["messages"] for t in offers(m) for n in ("main-verify", '"verify"') if n in t]

        agent = {"x-claude-code-agent-id": "agent-7", "x-claude-code-agent-type": "verifier"}
        self.assertEqual(names(agent, "a"), ['"verify"'])
        self.assertEqual(names(dict(agent, **{"x-claude-code-agent-type": "other"}), "b"), [])
        self.assertEqual(names(None, "c"), ["main-verify"])  # the main thread gets the one with no agent type

    def test_an_inner_playbook_is_offered_on_its_own_and_not_after_its_outer_was_taken(self):
        playbook.save(make("verify", title="Run the tests", triggers=["running the tests"], route=["test"], skills=[], agents=[]), script="print(1)\n")
        playbook.save(ship(includes=["verify"]), script="print(2)\n")
        playbook.approve("verify")
        playbook.approve("ship-change")
        rig = self.rig()
        alone = body(human("Continue"), use("python -m unittest", "Running the tests", "t1"), result("t1"))
        self.assertIn('"verify"', offers(self.send(rig, alone, session="a")["messages"][-1])[0])  # no outer workflow was recognised
        taken = body(human("Continue"), use("", "", "t0", "mcp__almanac__playbook_run", {"name": "ship-change"}), result("t0"),
                     use("python -m unittest", "Running the tests", "t1"), result("t1"))
        self.assertEqual([offers(m) for m in self.send(rig, taken, session="b")["messages"]], [[]] * 5)

    def test_adherence_is_logged_when_the_offer_is_taken(self):
        playbook.save(ship(), script="print(1)\n")
        playbook.approve("ship-change")
        rig = self.rig()
        payload = self.ladder()
        self.send(rig, payload)
        self.send(rig, body(*payload["messages"], use("", "", "t3", "mcp__almanac__playbook_run", {"name": "ship-change"}), result("t3")))
        self.send(rig, body(*payload["messages"], use("", "", "t3", "mcp__almanac__playbook_run", {"name": "ship-change"}), result("t3"), use("ls", "", "t4"), result("t4")))
        records = rig.records()
        self.assertEqual([r["playbook_taken"] for r in records if "playbook_taken" in r], ["ship-change"])  # said once
        self.assertEqual(len([r for r in records if "playbook_offer" in r]), 1)

    def test_a_draft_or_a_playbook_of_another_project_is_not_offered(self):
        playbook.save(ship(source="claude"))  # a draft
        rig = self.rig()
        self.assertEqual(offers(self.send(rig, self.ladder())["messages"][-1]), [])








if __name__ == "__main__":
    unittest.main()
