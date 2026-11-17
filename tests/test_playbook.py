import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import Rig

from getalmanac import cli, mcp, playbook
from getalmanac import request as rq
from getalmanac.memory import Memory
from getalmanac.playbook import PlaybookLayer
from getalmanac.store import Store


def make(name="weekly-report", **over):
    pb = {"name": name, "title": "Weekly report", "when": "The owner asks for the weekly report.",
          "triggers": ["weekly report", "friday summary"], "steps": ["If the folder is missing: ask for it", "Run the script"],
          "skills": ["interfaces:better-layout"], "agents": ["crew:code-reviewer"]}
    pb.update(over)
    return pb


SCRIPT = "import sys, json\nif '--help' in sys.argv:\n    print('usage')\nelse:\n    print(json.dumps({'args': sys.argv[1:]}))\n"


class Home(unittest.TestCase):
    """A fresh ALMANAC_HOME for each test."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="almanac-pb-")
        self.old = os.environ["ALMANAC_HOME"]
        os.environ["ALMANAC_HOME"] = self.home
        self.addCleanup(self.restore)

    def restore(self):
        os.environ["ALMANAC_HOME"] = self.old
        shutil.rmtree(self.home, ignore_errors=True)


class StoreTest(Home):
    def test_names_are_checked_and_path_tricks_refused(self):
        for bad in ("../x", "a/b", "A-b", "a", "-ab", "x" * 60, "", "a b", "..", "con\\x", None, 5):
            self.assertFalse(playbook.valid_name(bad), bad)
            with self.assertRaises(ValueError):
                playbook.save(make(bad))
        self.assertEqual(playbook.names(), [])
        self.assertFalse(playbook.remove("..") if playbook.valid_name("..") else False)
        with self.assertRaises(ValueError):
            playbook.folder("../../etc")

    def test_save_and_load_round_trip_and_defaults(self):
        pb = playbook.save(make())
        self.assertEqual((pb["status"], pb["uses"], pb["source"], pb["script"]), ("approved", 0, "claude", False))
        self.assertTrue((Path(self.home) / "playbooks" / "weekly-report" / "playbook.json").is_file())
        self.assertEqual(playbook.load("weekly-report"), pb)
        self.assertEqual(playbook.save(make(title="Weekly report v2"))["created"], pb["created"])

    def test_caller_cannot_claim_approval_or_uses(self):
        pb = playbook.save(make(status="approved", hash="abc", uses=40), script=SCRIPT)
        self.assertEqual((pb["status"], pb["hash"], pb["uses"]), ("draft", "", 0))

    def test_bad_fields_are_refused_on_save(self):
        for over in ({"title": ""}, {"title": "x" * 200}, {"triggers": []}, {"triggers": ["!!"]}, {"steps": []},
                     {"steps": ["a"] * 31}, {"skills": "x"}, {"args": [{"help": "no name"}]}, {"project": 5},
                     {"source": "robot"}, {"triggers": ["a"] * 11}):
            with self.assertRaises(ValueError, msg=over):
                playbook.save(make(**over))
        with self.assertRaises(ValueError):
            playbook.save(make(), script="x" * (playbook.SCRIPT_LIMIT + 1))
        with self.assertRaises(ValueError):
            playbook.save(make(when="w" * 300, steps=["é" * 399] * 30, skills=["k" * 79] * 20, agents=["g" * 79] * 20))
        self.assertEqual(playbook.names(), [])

    def test_triggers_are_lowercased(self):
        self.assertEqual(playbook.save(make(triggers=["Weekly REPORT"]))["triggers"], ["weekly report"])

    def test_a_bad_playbook_on_disk_is_skipped_not_a_crash(self):
        playbook.save(make("good-one"))
        root = Path(self.home) / "playbooks"
        for name, text in (("bad-json", "{nope"), ("bad-type", json.dumps(make("bad-type", steps="x"))),
                           ("wrong-name", json.dumps(make("other-name"))), ("huge-one", " " * (playbook.JSON_LIMIT + 1))):
            (root / name).mkdir()
            (root / name / "playbook.json").write_text(text, encoding="utf-8")
        (root / "no-script").mkdir()
        (root / "no-script" / "playbook.json").write_text(json.dumps(make("no-script", script=True)), encoding="utf-8")
        (root / "Not A Name").mkdir()
        (root / "stray.txt").write_text("x")
        self.assertEqual([p["name"] for p in playbook.listing()], ["good-one"])
        self.assertIsNone(playbook.load("bad-json"))

    def test_changing_the_script_after_approval_drops_it_to_draft(self):
        playbook.save(make(), script=SCRIPT)
        self.assertEqual(playbook.load("weekly-report")["status"], "draft")
        digest, text = playbook.approve("weekly-report")
        self.assertEqual(text, SCRIPT)
        self.assertEqual(playbook.load("weekly-report")["status"], "approved")
        self.assertEqual(playbook.save(make(title="New title"), script=SCRIPT)["status"], "approved")  # same text
        self.assertEqual(playbook.save(make(title="Newer"))["status"], "approved")  # script kept, not sent again
        changed = playbook.save(make(), script=SCRIPT + "# more\n")
        self.assertEqual((changed["status"], changed["hash"]), ("draft", ""))
        playbook.approve("weekly-report")
        Path(self.home, "playbooks", "weekly-report", "run.py").write_text("print(1)", encoding="utf-8")  # edited by hand
        self.assertFalse(playbook.runnable(playbook.load("weekly-report")))

    def test_dropping_the_script_removes_it(self):
        playbook.save(make(), script=SCRIPT)
        self.assertTrue(playbook.script_path("weekly-report").exists())
        pb = playbook.save(make(script=False))
        self.assertEqual((pb["script"], pb["status"]), (False, "approved"))
        self.assertFalse(playbook.script_path("weekly-report").exists())

    def test_remove(self):
        playbook.save(make())
        self.assertTrue(playbook.remove("weekly-report"))
        self.assertFalse(playbook.remove("weekly-report"))
        self.assertEqual(playbook.names(), [])


class MatchTest(Home):
    def setUp(self):
        super().setUp()
        playbook.save(make("weekly-report", triggers=["weekly report"]))
        playbook.save(make("report-all", triggers=["report"]))
        playbook.save(make("release-notes", title="Notes", triggers=["release notes", "changelog"], project="proj-a"))

    def names(self, text, project=""):
        return [pb["name"] for pb in playbook.find(text, project)]

    def test_a_trigger_needs_every_word(self):
        self.assertEqual(self.names("please send the weekly report now"), ["weekly-report", "report-all"])
        self.assertEqual(self.names("the report for this week"), ["report-all"])
        self.assertEqual(self.names("weekly status"), [])

    def test_words_not_substrings(self):
        self.assertEqual(self.names("reporting on the weekly reports"), [])

    def test_ties_go_to_uses_then_name(self):
        playbook.save(make("zeta-one", triggers=["deploy"]))
        playbook.save(make("alpha-one", triggers=["deploy"]))
        self.assertEqual(self.names("deploy it"), ["alpha-one", "zeta-one"])
        playbook.bump("zeta-one")
        self.assertEqual(self.names("deploy it"), ["zeta-one", "alpha-one"])

    def test_project_scoping(self):
        self.assertEqual(self.names("write the changelog entry"), [])
        self.assertEqual(self.names("write the changelog entry", "proj-b"), [])
        self.assertEqual(self.names("write the changelog entry", "proj-a"), ["release-notes"])

    def test_find_is_capped_at_five(self):
        for i in range(7):
            playbook.save(make(f"deploy-{i}", triggers=["deploy"]))
        self.assertEqual(len(self.names("deploy")), 5)


class RunTest(Home):
    def test_draft_refuses_and_approved_runs(self):
        playbook.save(make(), script=SCRIPT)
        self.assertIn("Not run", playbook.run("weekly-report", ["a"]))
        self.assertIn("almanac playbook approve weekly-report", playbook.run("weekly-report", []))
        playbook.approve("weekly-report")
        out = playbook.run("weekly-report", ["a", "b c"], cwd=self.home)
        self.assertTrue(out.startswith("exit code 0\n--- stdout ---\n"), out)
        self.assertEqual(json.loads(out.split("--- stdout ---\n")[1].split("\n--- stderr")[0]), {"args": ["a", "b c"]})

    def test_args_must_be_strings_and_unknown_names_refuse(self):
        playbook.save(make(), script=SCRIPT)
        playbook.approve("weekly-report")
        for bad in ("x", [1], None):
            with self.assertRaises(ValueError):
                playbook.run("weekly-report", bad)
        for name in ("nope-nope", "../x", "weekly-report-2"):
            with self.assertRaises(ValueError):
                playbook.run(name, [])
        playbook.save(make("text-only"))
        with self.assertRaises(ValueError):
            playbook.run("text-only", [])

    def test_exit_code_stderr_and_timeout_are_reported(self):
        playbook.save(make(), script="import sys\nprint('out'); print('bad', file=sys.stderr); sys.exit(3)\n")
        playbook.approve("weekly-report")
        out = playbook.run("weekly-report", [])
        self.assertIn("exit code 3", out)
        self.assertIn("out", out.split("--- stderr ---")[0])
        self.assertIn("bad", out.split("--- stderr ---")[1])
        playbook.save(make(), script="import time\nprint('start', flush=True)\ntime.sleep(30)\n")
        playbook.approve("weekly-report")
        out = playbook.run("weekly-report", [], timeout=1)
        self.assertIn("timed out after 1 seconds", out)

    def test_output_is_capped(self):
        playbook.save(make(), script="print('x' * 50000)\n")
        playbook.approve("weekly-report")
        out = playbook.run("weekly-report", [])
        self.assertLess(len(out), 21000)
        self.assertIn("[cut: ", out)


class CliTest(Home):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["playbook", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_show_path_remove(self):
        playbook.save(make(), script=SCRIPT)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("weekly-report", out)
        self.assertIn("draft", out)
        self.assertIn("Weekly report", out)
        self.assertIn("Run the script", self.run_cli("show", "weekly-report")[1])
        self.assertEqual(Path(self.run_cli("path")[1].strip()), Path(self.home) / "playbooks")
        self.assertEqual(self.run_cli("show", "../x")[0], 2)
        self.assertEqual(self.run_cli("remove", "weekly-report")[0], 0)
        self.assertEqual(playbook.names(), [])

    def test_approve_prints_the_first_40_lines_and_the_hash(self):
        playbook.save(make(), script="".join(f"# line {i}\n" for i in range(60)))
        code, out, _ = self.run_cli("approve", "weekly-report")
        self.assertEqual(code, 0)
        self.assertIn("# line 39", out)
        self.assertNotIn("# line 40", out)
        self.assertIn("20 more lines", out)
        pb = playbook.load("weekly-report")
        self.assertEqual(pb["status"], "approved")
        self.assertIn(pb["hash"], out)
        self.assertTrue(playbook.runnable(pb))


TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]
ASK = "please send the weekly report for the team"


def human(text):
    return {"role": "user", "content": [{"type": "text", "text": "<system-reminder>rules</system-reminder>"},
                                        {"type": "text", "text": text}]}


def said(text="done"):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def body(*messages, tools=True):
    out = {"model": "claude-test", "system": [{"type": "text", "text": "x\n - Primary working directory: /tmp/p\n"}],
           "messages": list(messages)}
    if tools:
        out["tools"] = TOOLS
    return out


def reminders(msg):
    return [b["text"] for b in rq.blocks(msg) if b.get("type") == "text" and "Almanac playbook" in b.get("text", "")]


class SetProject:
    def __init__(self, key):
        self.key = key

    def transform(self, body, ctx):
        ctx.project = self.key


class LayerTest(Home):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(self.home, "a.db"))
        self.addCleanup(self.store.close)

    def rig(self, project="", overrides=None):
        rig = Rig(layers=[SetProject(project), PlaybookLayer()], store=self.store, overrides=overrides)
        self.addCleanup(rig.close)
        return rig

    def send(self, rig, payload, **kw):
        rig.messages(payload, **kw)
        return rig.sent()

    def test_a_match_adds_a_note_to_the_newest_typed_turn_only(self):
        playbook.save(make(), script=SCRIPT)
        rig = self.rig()
        sent = self.send(rig, body(human("what is in the folder today?"), said(), human(ASK)))
        first, second = sent["messages"][0], sent["messages"][2]
        self.assertEqual(reminders(first), [])
        self.assertEqual(len(reminders(second)), 1)
        self.assertEqual(rq.human_text(second), ASK)  # the typed text is still last and untouched
        self.assertEqual(rq.blocks(second)[-1]["text"], ASK)
        text = reminders(second)[0]
        self.assertTrue(text.startswith('<system-reminder>\nAlmanac playbook "weekly-report" matches this request (Weekly report; used 1 time).'))
        self.assertIn("Steps:\n1. If the folder is missing: ask for it\n2. Run the script\n", text)
        self.assertIn("Skills: interfaces:better-layout. Agents: crew:code-reviewer.", text)
        self.assertIn("Script (draft, not yet approved): run `python", text)
        self.assertIn("record it with `playbook_save`.\n</system-reminder>", text)
        self.assertLessEqual(len(text), 1500)

    def test_an_approved_script_names_the_tool_and_a_plain_string_message_works(self):
        playbook.save(make(args=[{"name": "path", "help": "the folder"}]), script=SCRIPT)
        playbook.approve("weekly-report")
        sent = self.send(self.rig(), body({"role": "user", "content": ASK}))
        self.assertIn('Script: call the tool `playbook_run` with name "weekly-report" and args [<path>]', reminders(sent["messages"][0])[0])
        self.assertEqual(rq.human_text(sent["messages"][0]), ASK)

    def test_no_script_means_no_script_line(self):
        playbook.save(make())
        text = reminders(self.send(self.rig(), body(human(ASK)))["messages"][0])[0]
        self.assertNotIn("Script", text)

    def test_the_note_never_passes_the_cap(self):
        playbook.save(make(steps=["step " + "w" * 300] * 30, title="t" * 120, when="w" * 300,
                           skills=["s" * 79] * 20, agents=["a" * 79] * 20))
        text = reminders(self.send(self.rig(), body(human(ASK)))["messages"][0])[0]
        self.assertLessEqual(len(text), 1500)
        self.assertTrue(text.endswith("</system-reminder>"))

    def test_no_match_on_short_or_follow_up_text_or_other_words(self):
        playbook.save(make(triggers=["run it", "sounds good", "ok"]))
        rig = self.rig()
        for text in ("run it", "ok", "yes, sounds good", "send the monthly numbers"):
            sent = self.send(rig, body(human(text)))
            self.assertEqual(reminders(sent["messages"][0]), [], text)
        self.assertEqual(playbook.load("weekly-report")["uses"], 0)

    def test_a_project_playbook_matches_only_in_its_project(self):
        playbook.save(make("proj-only", project="proj-a"))
        self.assertEqual(reminders(self.send(self.rig("proj-b"), body(human(ASK)), session="s-b")["messages"][0]), [])
        self.assertEqual(len(reminders(self.send(self.rig("proj-a"), body(human(ASK)), session="s-a")["messages"][0])), 1)

    def test_a_sub_agent_and_a_toolless_request_are_left_alone(self):
        playbook.save(make())
        rig = self.rig()
        raw = json.dumps(body(human(ASK))).encode()
        rig.messages(raw, extra={"x-claude-code-agent-id": "agent-7"})
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        raw = json.dumps(body(human(ASK), tools=False)).encode()
        rig.messages(raw)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)

    def test_the_note_is_byte_identical_after_the_playbook_is_edited_and_deleted(self):
        playbook.save(make(), script=SCRIPT)
        rig = self.rig()
        payload = body(human(ASK))
        self.send(rig, payload)
        first = rig.upstream.requests[-1]["body"]
        playbook.save(make(title="Renamed", steps=["Do something else"], triggers=["other words"]))
        self.send(rig, payload)
        self.assertEqual(rig.upstream.requests[-1]["body"], first)
        playbook.remove("weekly-report")
        self.send(rig, payload)
        self.assertEqual(rig.upstream.requests[-1]["body"], first)
        self.assertIn(b"Almanac playbook", first)

    def test_a_later_turn_keeps_the_earlier_note(self):
        playbook.save(make(), script=SCRIPT)
        rig = self.rig()
        first = self.send(rig, body(human(ASK)))
        note = reminders(first["messages"][0])[0]
        self.send(rig, body(human(ASK)))  # the tool loop of the same turn
        playbook.remove("weekly-report")
        later = self.send(rig, body(human(ASK), said(), human("now tidy the notes folder up")))
        self.assertEqual(reminders(later["messages"][0]), [note])
        self.assertEqual(reminders(later["messages"][2]), [])

    def test_uses_is_counted_once_per_frozen_note(self):
        playbook.save(make())
        rig = self.rig()
        for _ in range(3):
            self.send(rig, body(human(ASK)))
        self.assertEqual(playbook.load("weekly-report")["uses"], 1)
        self.send(rig, body(human(ASK), said(), human("send the weekly report again, to the board")))
        self.assertEqual(playbook.load("weekly-report")["uses"], 2)

    def test_a_playbook_saved_during_a_turn_does_not_appear_in_it(self):
        rig = self.rig()
        payload = body(human(ASK))
        plain = self.send(rig, payload)
        self.assertEqual(reminders(plain["messages"][0]), [])
        playbook.save(make())
        self.assertEqual(self.send(rig, payload), plain)

    def test_the_best_match_wins(self):
        playbook.save(make("weekly-report", triggers=["report"]))
        playbook.save(make("friday-report", title="Friday", triggers=["weekly report"]))
        text = reminders(self.send(self.rig(), body(human(ASK)))["messages"][0])[0]
        self.assertIn('"friday-report"', text)

    def test_disabled_or_not_injecting_changes_nothing(self):
        playbook.save(make())
        for key in ("playbooks.enabled", "playbooks.inject"):
            rig = self.rig(overrides={key: False})
            raw = json.dumps(body(human(ASK))).encode()
            rig.messages(raw)
            self.assertEqual(rig.upstream.requests[-1]["body"], raw, key)
        self.assertEqual(playbook.load("weekly-report")["uses"], 0)

    def test_a_broken_playbook_folder_fails_open(self):
        rig = self.rig()
        Path(self.home, "playbooks").write_text("a file where the folder should be")
        raw = json.dumps(body(human(ASK))).encode()
        rig.messages(raw)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)


class ToolTest(Home):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(self.home, "a.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.cwd = tempfile.mkdtemp(prefix="almanac-cwd-")
        self.addCleanup(shutil.rmtree, self.cwd, True)

    def tool(self, tool, **args):
        reply = mcp.handle(self.memory, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": tool, "arguments": args}}, self.cwd)["result"]
        return reply["content"][0]["text"], reply["isError"]

    def test_save_then_find(self):
        text, failed = self.tool("playbook_save", playbook=make(source="owner"), script=SCRIPT)
        self.assertFalse(failed)
        self.assertIn("status: draft", text)
        self.assertIn("almanac playbook approve weekly-report", text)
        self.assertIn(str(playbook.folder("weekly-report")), text)
        self.assertEqual(playbook.load("weekly-report")["source"], "claude")
        found, _ = self.tool("playbook_find", query="send me the weekly report")
        self.assertIn("weekly-report: Weekly report (draft, used 0x)", found)
        self.assertIn("The owner asks for the weekly report.", found)
        self.assertEqual(self.tool("playbook_find", query="unrelated words")[0], "No playbook matches.")

    def test_find_by_exact_name_returns_the_steps(self):
        self.tool("playbook_save", playbook=make())
        full, failed = self.tool("playbook_find", query="weekly-report")
        self.assertFalse(failed)
        self.assertIn("Steps:", full)
        self.assertIn("1. If the folder is missing: ask for it", full)
        self.assertNotIn("system-reminder", full)
        self.assertNotIn("matches this request", full)
        self.assertTrue(full.startswith("weekly-report: Weekly report (approved, used 0x)\nThe owner asks for the weekly report.\nSteps:\n1. "))
        self.assertIn("Skills: interfaces:better-layout", full)
        self.assertIn("Agents: crew:code-reviewer", full)
        listed, _ = self.tool("playbook_find", query="weekly report")  # words, not a name: the short list as before
        self.assertNotIn("Steps:", listed)
        self.assertEqual(self.tool("playbook_find", query="no-such-book")[0], "No playbook matches.")

    def test_find_by_exact_name_is_not_cut_and_names_the_script(self):
        long_steps = [f"step {i} " + "x" * 390 for i in range(30)]  # more than a request note can hold
        self.tool("playbook_save", playbook=make(steps=long_steps), script=SCRIPT)
        full, _ = self.tool("playbook_find", query="weekly-report")
        self.assertTrue(all(step in full for step in long_steps))
        self.assertGreater(len(full), playbook.NOTE_LIMIT)
        self.assertIn("Script (draft, not yet approved)", full)
        playbook.approve("weekly-report")
        self.assertIn('call the tool `playbook_run` with name "weekly-report"', self.tool("playbook_find", query="weekly-report")[0])

    def test_save_without_a_script_needs_no_approval(self):
        text, _ = self.tool("playbook_save", playbook=make())
        self.assertIn("status: approved", text)
        self.assertNotIn("approve weekly-report", text)

    def test_save_refuses_a_bad_playbook(self):
        for bad in (make("../escape"), make(steps=[]), "text"):
            self.assertTrue(self.tool("playbook_save", playbook=bad)[1], bad)
        self.assertEqual(playbook.names(), [])

    def test_run_refuses_a_draft_and_runs_an_approved_script(self):
        self.tool("playbook_save", playbook=make(), script=SCRIPT)
        text, failed = self.tool("playbook_run", name="weekly-report", args=["x"])
        self.assertFalse(failed)
        self.assertTrue(text.startswith("Not run"), text)
        self.assertIn("run.py", text)
        playbook.approve("weekly-report")
        text, _ = self.tool("playbook_run", name="weekly-report", args=["x", "y"])
        self.assertEqual(json.loads(text.split("--- stdout ---\n")[1].split("\n--- stderr")[0]), {"args": ["x", "y"]})

    def test_a_script_changed_after_approval_does_not_run(self):
        self.tool("playbook_save", playbook=make(), script=SCRIPT)
        playbook.approve("weekly-report")
        self.tool("playbook_save", playbook=make(), script=SCRIPT + "print('new')\n")
        text, _ = self.tool("playbook_run", name="weekly-report")
        self.assertTrue(text.startswith("Not run"), text)

    def test_run_rejects_bad_args_and_reports_failure(self):
        self.tool("playbook_save", playbook=make(), script="import sys\nsys.exit(5)\n")
        playbook.approve("weekly-report")
        self.assertTrue(self.tool("playbook_run", name="weekly-report", args="not a list")[1])
        self.assertTrue(self.tool("playbook_run", name="weekly-report", args=[1])[1])
        self.assertIn("exit code 5", self.tool("playbook_run", name="weekly-report")[0])
        self.assertTrue(self.tool("playbook_run", name="nothing-here")[1])

    def test_the_script_runs_in_the_session_folder(self):
        self.tool("playbook_save", playbook=make(), script="import os\nprint(os.getcwd())\n")
        playbook.approve("weekly-report")
        text, _ = self.tool("playbook_run", name="weekly-report")
        self.assertEqual(os.path.realpath(text.split("--- stdout ---\n")[1].split("\n")[0].strip()), os.path.realpath(self.cwd))

    def test_the_launcher_adds_the_server_when_only_playbooks_are_on(self):
        from getalmanac import launcher
        cfg = {"memory": {"enabled": False, "tools": False}, "playbooks": {"enabled": True}}
        self.assertEqual(launcher.with_memory_tools(["-p"], cfg)[0], "--mcp-config")
        cfg["playbooks"]["enabled"] = False
        self.assertEqual(launcher.with_memory_tools(["-p"], cfg), ["-p"])


class BundleTest(unittest.TestCase):
    def test_the_playbook_skill_ships_with_a_manifest_and_licence(self):
        folder = Path(playbook.__file__).parent / "bundle" / "almanac"
        self.assertEqual(json.loads((folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"], "almanac")
        self.assertTrue((folder / "LICENSE").is_file())
        head = (folder / "skills" / "playbook" / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        self.assertIn("name: playbook", head)
        self.assertIn("| `almanac` |", (folder.parent / "SOURCES.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
