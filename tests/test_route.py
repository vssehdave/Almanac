"""Route-based playbook offers, playbooks that include playbooks, structured results and one playbook per route."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import Rig
from test_playbook import SetProject, Home, body, human, make, result

from getalmanac import playbook
from getalmanac import request as rq
from getalmanac.playbook import PlaybookLayer
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
        self.ahead = mock.patch("getalmanac.playbook._ahead", return_value=True)
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
        with mock.patch("getalmanac.playbook._ahead", return_value=False):
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
        with mock.patch("getalmanac.playbook._ahead", return_value=False):  # the facts moved on: the decision does not
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


class ResultTest(Home):
    def approved(self, name, script, **over):
        playbook.save(make(name, **over), script=script)
        playbook.approve(name)

    def test_pass_and_fail_lead_with_one_result_line(self):
        self.approved("weekly-report", "print('all good')\n")
        out = playbook.run("weekly-report", [])
        self.assertEqual(out.split("\n")[0], "RESULT: pass")
        self.assertIn("all good", out)
        failing = ("import sys\nsys.stderr.write('noise\\n' + '=' * 70 + '\\nFAIL: test_a (m.T)\\n' + '-' * 70 + '\\nTraceback (most recent call last):\\n"
                   "  File x\\nAssertionError: boom\\n\\n' + '-' * 70 + '\\nRan 3 tests in 0.1s\\n\\nFAILED (failures=1)\\n')\nsys.exit(1)\n")
        self.approved("weekly-report", failing)
        out = playbook.run("weekly-report", [])
        self.assertEqual(out.split("\n")[0], "RESULT: fail (exit 1)")
        self.assertIn("Failing: test_a (m.T)", out)
        self.assertIn("AssertionError: boom", out)
        self.assertNotIn("noise", out)
        self.assertNotIn("Ran 3 tests", out)
        self.assertNotIn("RESUME", out)  # one step: nothing to go on from

    def test_a_failure_with_no_test_output_shows_the_end_of_it_and_a_timeout_is_a_failure(self):
        self.approved("weekly-report", "import sys\nprint('\\n'.join(str(i) for i in range(100)))\nsys.exit(2)\n")
        out = playbook.run("weekly-report", [])
        self.assertTrue(out.startswith("RESULT: fail (exit 2)\n"))
        self.assertIn("99", out)
        self.assertNotIn("\n10\n", out)
        self.approved("weekly-report", "import time\ntime.sleep(30)\n")
        self.assertTrue(playbook.run("weekly-report", [], timeout=1).startswith("RESULT: fail (timed out after 1 seconds"))


    def test_a_failure_report_printed_on_stdout_is_kept_and_a_pass_keeps_its_last_lines(self):
        self.approved("weekly-report", "import json, sys\nprint(json.dumps({'ok': False, 'failed': 'git push'}))\nsys.exit(1)\n")
        out = playbook.run("weekly-report", [])
        self.assertIn('{"ok": false, "failed": "git push"}', out)
        self.approved("weekly-report", "print('x' * 5000)\nprint('https://example.test/pull/7')\n")
        self.assertIn("https://example.test/pull/7", playbook.run("weekly-report", []))


class ComposeTest(Home):
    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="almanac-comp-")
        count = "open('count.txt', 'a').write('x')\n"
        for name, script, over in (
                ("verify", count + "print('verified')\n", {}),
                ("wait-ci", "import os, sys\nif not os.path.exists('green'):\n    print('ci is red', file=sys.stderr)\n    sys.exit(1)\nprint('ci green')\n", {}),
                ("ship-change", "print('shipped')\n", {"includes": ["verify", "wait-ci"]})):
            playbook.save(make(name, **over), script=script)
            playbook.approve(name)

    def test_inner_playbooks_run_in_order_and_stop_at_the_first_failure(self):
        out = playbook.run("ship-change", [], self.cwd)
        lines = out.split("\n")
        self.assertEqual(lines[:2], ["RESULT: fail (exit 1)", "FAILED STEP: wait-ci (2 of 3)"])
        self.assertIn("ci is red", out)
        self.assertEqual(json.loads(lines[-1].split("RESUME: playbook_run ")[1]), {"name": "ship-change", "from": "wait-ci"})
        self.assertNotIn("shipped", out)
        Path(self.cwd, "green").write_text("")
        out = playbook.run("ship-change", [], self.cwd)
        self.assertTrue(out.startswith("RESULT: pass\nSteps run: verify, wait-ci, ship-change\n"))
        self.assertIn("shipped", out)

    def test_resume_goes_on_from_the_step_and_does_not_run_the_ones_before(self):
        playbook.run("ship-change", [], self.cwd)  # fails at wait-ci after verify ran once
        Path(self.cwd, "green").write_text("")
        for start in ("wait-ci", "2"):
            out = playbook.run("ship-change", [], self.cwd, start=start)
            self.assertTrue(out.startswith("RESULT: pass\nSteps run: wait-ci, ship-change\n"), out)
        self.assertEqual(Path(self.cwd, "count.txt").read_text(), "x")  # verify ran once in all
        with self.assertRaises(ValueError):
            playbook.run("ship-change", [], self.cwd, start="nope-nope")
        with self.assertRaises(ValueError):
            playbook.run("ship-change", [], self.cwd, start="9")

    def test_the_outer_arguments_go_to_its_own_script_and_the_resume_line_keeps_them(self):
        playbook.save(make("ship-change", includes=["wait-ci"], args=[{"name": "--msg", "help": "m"}]),
                      script="import sys\nprint(sys.argv[1:])\n")
        playbook.approve("ship-change")
        out = playbook.run("ship-change", ["--msg", "x"], self.cwd)
        self.assertEqual(json.loads(out.split("RESUME: playbook_run ")[1]), {"name": "ship-change", "from": "wait-ci", "args": ["--msg", "x"]})
        Path(self.cwd, "green").write_text("")
        self.assertIn("['--msg', 'x']", playbook.run("ship-change", ["--msg", "x"], self.cwd, start="ship-change"))

    def test_loops_missing_inner_playbooks_and_an_unapproved_inner_one_are_refused_or_reported(self):
        with self.assertRaises(ValueError):
            playbook.save(make("self-loop", includes=["self-loop"]))
        playbook.save(make("a-one", includes=["b-one"]), script="print(1)\n")
        playbook.save(make("b-one", includes=["a-one"]), script="print(1)\n")
        with self.assertRaises(ValueError):
            playbook.run("a-one", [])
        playbook.save(make("c-one", includes=["gone-one"]))
        with self.assertRaises(ValueError):
            playbook.run("c-one", [])
        playbook.save(make("draft-inner"), script="print(1)\n")  # a draft script
        playbook.save(make("d-one", includes=["draft-inner"]))
        self.assertTrue(playbook.run("d-one", []).startswith("RESULT: fail (not run)"))


class DedupeTest(Home):
    def save(self, name, triggers, **over):
        return playbook.save(make(name, **dict(dict(triggers=triggers, route=["commit", "push", "pr"], skills=[], agents=[]), **over)))

    def test_near_identical_drafts_merge_and_approved_ones_are_left_alone(self):
        self.save("ship-a", ["ship a change"], source="claude")
        self.save("ship-b", ["push a pr"], source="miner")
        self.save("ship-c", ["merge the pr"])  # the owner's: approved
        self.save("other-way", ["clean up"], route=["test", "install"], source="claude")
        lines = []
        self.assertEqual(playbook.dedupe(out=lines.append), 1)
        self.assertEqual(lines, ["merged ship-b into ship-a"])
        self.assertEqual(playbook.names(), ["other-way", "ship-a", "ship-c"])
        self.assertEqual(playbook.load("ship-a")["triggers"], ["ship a change", "push a pr"])
        self.assertEqual(playbook.load("ship-c")["triggers"], ["merge the pr"])
        self.assertEqual(playbook.dedupe(include_approved=True, out=lines.append), 1)
        self.assertEqual(playbook.names(), ["other-way", "ship-c"])  # the approved one is the one kept
        self.assertEqual(playbook.load("ship-c")["triggers"], ["merge the pr", "ship a change", "push a pr"])
        self.assertEqual(playbook.dedupe(), 0)

    def test_a_different_agent_type_or_project_is_a_different_playbook(self):
        self.save("ship-a", ["ship a change"], source="claude")
        self.save("ship-b", ["push a pr"], source="claude", agent_type="shipper")
        self.save("ship-c", ["merge the pr"], source="claude", project="elsewhere")
        self.assertEqual(playbook.dedupe(), 0)

    def test_one_that_others_include_keeps_its_name_and_the_cli_prints_what_it_merged(self):
        self.save("ship-a", ["ship a change"], source="claude")
        self.save("ship-b", ["push a pr"], source="claude")
        playbook.save(make("outer-one", includes=["ship-b"], source="claude", route=["test", "install"], skills=[], agents=[]))
        self.assertEqual(playbook.dedupe(), 0)
        import contextlib
        import io
        from getalmanac import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["playbook", "dedupe"]), 0)
        self.assertEqual(out.getvalue().strip(), "nothing to merge")


if __name__ == "__main__":
    unittest.main()
