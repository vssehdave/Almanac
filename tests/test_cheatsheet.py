import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import Rig, sse
from test_routing import LARGE, MAIN, MEDIUM, SMALL, SetProject, body, convo, sub_headers

from helpers import playbook

from getalmanac import cheatsheet, cli, config, proxy as proxy_mod, request as rq
from getalmanac.cheatsheet import CheatsheetLayer
from getalmanac.routing import RoutingLayer
from getalmanac.store import State, Store

IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
CACHE = {"type": "ephemeral"}


def claude_system():
    """The shape Claude Code sends: a billing line, its identity, then the main prompt, two of them cached."""
    return [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1"},
            {"type": "text", "text": IDENTITY, "cache_control": CACHE},
            {"type": "text", "text": "Main prompt.", "cache_control": CACHE}]


def ctx(klass="main", agent="", agent_type="", mode="on", lean=False, **start):
    cfg = config.load()
    cfg["cheatsheet"]["mode"] = mode
    cfg["startup"]["lean_rules"] = lean  # the other tests are about the model sheets; LeanTest turns it on
    cfg["startup"].update(start)
    return SimpleNamespace(cfg=cfg, kind="messages", klass=klass, agent=agent, agent_type=agent_type)


def transform(work, there):
    there.thread = rq.thread(there, work)
    CheatsheetLayer().transform(work, there)


def run(model, system=None, tools=None, **kw):
    work = body(convo([{"text": "hello"}]), model, tools=tools, system=claude_system() if system is None else system)
    transform(work, ctx(**kw))
    return work


def cache_marks(data):
    return json.dumps(data).count('"cache_control"')


class Fresh(unittest.TestCase):
    def setUp(self):
        cheatsheet.read.cache_clear()
        self.addCleanup(cheatsheet.read.cache_clear)


class TextTest(Fresh):
    def test_each_family_gets_its_own_text(self):
        rest = "\n\n" + cheatsheet.read("common") + "\n\n" + cheatsheet.read("skills")
        self.assertEqual(cheatsheet.text_for(LARGE), cheatsheet.read("opus") + rest)
        self.assertEqual(cheatsheet.text_for(MEDIUM), cheatsheet.read("sonnet") + rest)
        fable = cheatsheet.read("fable") + "\n\n" + cheatsheet.read("skills")  # its own method stays; only the skills are added
        self.assertEqual(cheatsheet.text_for("claude-fable-1"), fable)
        self.assertEqual(cheatsheet.text_for("claude-mythos-2"), fable)
        self.assertEqual(cheatsheet.text_for(SMALL), cheatsheet.read("haiku"))
        self.assertTrue(cheatsheet.read("haiku").startswith("# How to work here (Haiku)"))
        self.assertEqual(cheatsheet.text_for("Claude-OPUS-4-1"), cheatsheet.text_for(LARGE))

    def test_other_models_get_nothing(self):
        for model in ("some-model", ""):
            self.assertEqual(cheatsheet.text_for(model), "")
            self.assertEqual(run(model)["system"], claude_system())

    def test_the_users_copy_wins(self):
        home = tempfile.mkdtemp(prefix="almanac-sheet-")
        os.makedirs(os.path.join(home, "cheatsheets"))
        with open(os.path.join(home, "cheatsheets", "haiku.md"), "w", encoding="utf-8") as handle:
            handle.write("My own rules.\n")
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": home}):
            self.assertEqual(cheatsheet.text_for(SMALL), "My own rules.")
            self.assertIn("(Sonnet)", cheatsheet.text_for(MEDIUM))  # files without a copy stay packaged


class LayerTest(Fresh):
    def test_appended_to_the_last_text_block_without_touching_cache_marks(self):
        work = run(MEDIUM)
        before = claude_system()
        self.assertEqual(len(work["system"]), 3)
        self.assertEqual([b.get("cache_control") for b in work["system"]], [b.get("cache_control") for b in before])
        self.assertEqual(work["system"][:2], before[:2])
        self.assertEqual(work["system"][2]["text"], "Main prompt.\n\n" + cheatsheet.text_for(MEDIUM))
        self.assertEqual(cache_marks(work), cache_marks(body(convo([{"text": "hello"}]), MEDIUM, system=before)))

    def test_a_string_or_single_block_system_gets_a_block_after_it(self):
        sheet = cheatsheet.text_for(SMALL)
        self.assertEqual(run(SMALL, system=IDENTITY)["system"], [{"type": "text", "text": IDENTITY}, {"type": "text", "text": sheet}])
        one = [{"type": "text", "text": IDENTITY, "cache_control": CACHE}]
        self.assertEqual(run(SMALL, system=copy.deepcopy(one))["system"], one + [{"type": "text", "text": sheet}])

    def test_never_added_twice(self):
        for system in (claude_system(), IDENTITY):
            work = run(LARGE, system=system)
            once = copy.deepcopy(work)
            transform(work, ctx())
            self.assertEqual(work, once)

    def test_threads_get_it_and_other_requests_do_not(self):
        sheet = cheatsheet.text_for(MEDIUM)
        self.assertTrue(run(MEDIUM, klass="subagent", agent="a1")["system"][-1]["text"].endswith(cheatsheet.text_for(MEDIUM, True)))
        self.assertTrue(run(MEDIUM, klass="subagent", agent="a1", agent_type="fork")["system"][-1]["text"].endswith(sheet))
        self.assertEqual(run(MEDIUM, tools=[])["system"], claude_system())  # titles and quota checks carry no tools
        self.assertEqual(run(MEDIUM, klass="title")["system"], claude_system())

    def test_main_thread_text_is_the_full_sheet_and_a_fork_gets_the_same(self):
        for model in (LARGE, MEDIUM, SMALL):
            full = "Main prompt.\n\n" + cheatsheet.text_for(model)
            self.assertEqual(run(model)["system"][-1]["text"], full)
            self.assertEqual(run(model, klass="subagent", agent="a1", agent_type="fork")["system"][-1]["text"], full)

    def test_a_sub_agent_gets_the_short_rules_and_the_skills_except_on_haiku(self):
        agent, skills = cheatsheet.read("agent"), cheatsheet.skills(sub=True)
        for model in (LARGE, MEDIUM):
            text = run(model, klass="subagent", agent="a1")["system"][-1]["text"]
            self.assertEqual(text, "Main prompt.\n\n" + agent + "\n\n" + skills)
        text = run(SMALL, klass="subagent", agent="a1")["system"][-1]["text"]
        self.assertEqual(text, "Main prompt.\n\n" + agent)
        self.assertNotIn(cheatsheet.read("opus"), json.dumps(run(LARGE, klass="subagent", agent="a1")))
        self.assertNotIn("(Opus)", json.dumps(run(LARGE, klass="subagent", agent="a1")))
        self.assertEqual(run("some-model", klass="subagent", agent="a1")["system"], claude_system())

    def test_the_users_agent_copy_is_used(self):
        home = tempfile.mkdtemp(prefix="almanac-sheet-")
        os.makedirs(os.path.join(home, "cheatsheets"))
        with open(os.path.join(home, "cheatsheets", "agent.md"), "w", encoding="utf-8") as handle:
            handle.write("Be brief.\n")
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": home}):
            self.assertEqual(cheatsheet.text_for(SMALL, True), "Be brief.")
            self.assertEqual(cheatsheet.text_for(LARGE, True), "Be brief.\n\n" + cheatsheet.skills(sub=True))
            self.assertIn("(Opus)", cheatsheet.text_for(LARGE))  # the full sheet is untouched

    def test_mode_off(self):
        self.assertEqual(run(LARGE, mode="off")["system"], claude_system())


class LeanTest(Fresh):
    def sheet(self, model, **kw):
        return run(model, **{"lean": True, **kw})["system"][-1]["text"]

    def test_main_thread_sub_agent_and_fork_all_get_the_rules_first(self):
        lean = cheatsheet.lean_text(ctx(lean=True).cfg)
        self.assertIn("Lean changes", lean)
        for model in (LARGE, MEDIUM, SMALL):
            self.assertEqual(self.sheet(model), "Main prompt.\n\n" + lean + "\n\n" + cheatsheet.text_for(model))
            self.assertEqual(self.sheet(model, klass="subagent", agent="a1"),
                             "Main prompt.\n\n" + lean + "\n\n" + cheatsheet.text_for(model, True))
            self.assertEqual(self.sheet(model, klass="subagent", agent="a1", agent_type="fork"), self.sheet(model))

    def test_every_model_gets_it_and_a_request_without_tools_does_not(self):
        lean = cheatsheet.lean_text(ctx(lean=True).cfg)
        self.assertTrue(self.sheet("some-model").endswith(lean))
        self.assertTrue(self.sheet(LARGE, mode="off").endswith(lean))
        self.assertEqual(run(MEDIUM, tools=[], lean=True)["system"], claude_system())

    def test_the_cache_marks_stay_and_the_bytes_never_change_between_requests(self):
        first, second = run(LARGE, lean=True), run(LARGE, lean=True)
        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertEqual(cache_marks(first), cache_marks(claude_system()))
        once = copy.deepcopy(first)
        transform(first, ctx(lean=True))
        self.assertEqual(first, once)

    def test_off_and_a_text_of_your_own(self):
        self.assertEqual(run(LARGE, lean=False, mode="off")["system"], claude_system())
        self.assertEqual(self.sheet("some-model", lean_rules_text="Keep it small."), "Main prompt.\n\nKeep it small.")

    def test_levels(self):
        full = cheatsheet.lean_text(ctx(lean=True).cfg)
        lite = cheatsheet.lean_text(ctx(lean=True, lean_level="lite").cfg)
        ultra = cheatsheet.lean_text(ctx(lean=True, lean_level="ultra").cfg)
        self.assertTrue(lite.startswith(full) and "Level: lite" in lite)
        self.assertTrue(ultra.startswith(full) and "question the request" in ultra)
        self.assertEqual(cheatsheet.lean_text(ctx(lean=True, lean_level="nonsense").cfg), full)

    def test_the_rules_say_things_once(self):
        lean, common = cheatsheet.read("lean"), cheatsheet.read("common")
        for line in common.splitlines():
            if len(line) > 40:
                self.assertNotIn(line.strip(), lean)
        self.assertNotIn("smallest change that solves", common)


class ProxyTest(Fresh):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-sheet-"), "a.db"))
        self.state = State(self.store.path.with_name("state.db"))
        self.addCleanup(self.store.close)

    def start(self, respond=None, mode="off"):
        self.rig = Rig(respond=respond, layers=[SetProject(), RoutingLayer(), CheatsheetLayer()], store=self.store, state=self.state,
                       overrides={"routing.mode": mode, "startup.lean_rules": False})
        self.addCleanup(self.rig.close)

    def system_of(self, index):
        return json.dumps(self.rig.sent(index)["system"])

    def test_a_conversation_sends_the_same_system_bytes_every_time(self):
        self.start()
        first = convo([{"text": "hi"}])
        self.rig.messages(body(first, SMALL, system=claude_system()), extra=MAIN)
        self.rig.messages(body(convo([{"text": "hi"}, {"text": "and more"}]), SMALL, system=claude_system()), extra=MAIN)
        raw = [r["body"] for r in self.rig.upstream.requests]
        cut = [b[b.index(b'"system"'):b.index(b'"tools"')] for b in raw]
        self.assertEqual(len(set(cut)), 1)
        self.assertIn(json.dumps(cheatsheet.text_for(SMALL))[1:-1].encode(), cut[0])

    def test_a_sub_agent_sends_the_same_system_bytes_every_time(self):
        self.start()
        self.rig.messages(body(convo([{"text": "hi"}]), LARGE, system=claude_system()), extra=sub_headers())
        self.rig.messages(body(convo([{"text": "hi"}, {"text": "and more"}]), LARGE, system=claude_system()), extra=sub_headers())
        raw = [r["body"] for r in self.rig.upstream.requests]
        cut = [b[b.index(b'"system"'):b.index(b'"tools"')] for b in raw]
        self.assertEqual(len(cut), 2)
        self.assertEqual(cut[0], cut[1])
        self.assertIn(json.dumps(cheatsheet.text_for(LARGE, True))[1:-1].encode(), cut[0])
        self.assertNotIn(b"(Opus)", cut[0])

    def test_the_sheet_follows_the_routed_model_and_the_fallback(self):
        def respond(req):
            if json.loads(req["body"])["model"] == LARGE:
                return 404, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"model"}}'
            return 200, {"content-type": "text/event-stream"}, sse()
        self.start(respond, mode="all")
        status, _, _ = self.rig.messages(body(convo([{"text": "design a plugin system"}]), MEDIUM, system=claude_system()), extra=MAIN)
        self.assertEqual(status, 200)
        sent = [json.loads(r["body"]) for r in self.rig.upstream.requests]
        self.assertEqual([s["model"] for s in sent], [LARGE, MEDIUM])
        self.assertTrue(sent[0]["system"][-1]["text"].endswith(cheatsheet.text_for(LARGE)))
        self.assertTrue(sent[1]["system"][-1]["text"].endswith(cheatsheet.text_for(MEDIUM)))
        self.assertNotIn("(Opus)", json.dumps(sent[1]))

    def test_a_refused_sheet_goes_out_as_the_original(self):
        def respond(req):
            if b"How to work here" in req["body"]:
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"bad"}}'
            return 200, {"content-type": "text/event-stream"}, sse()
        self.start(respond)
        raw = json.dumps(body(convo([{"text": "hi"}]), MEDIUM, system=claude_system())).encode()
        status, _, _ = self.rig.messages(raw, extra=MAIN)
        self.assertEqual(status, 200)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_the_full_proxy_includes_the_layer_after_routing(self):
        with mock.patch.object(proxy_mod, "Proxy", side_effect=lambda cfg, layers, **_: layers):
            layers = [type(layer).__name__ for layer in proxy_mod.build(config.load())]
        self.assertEqual(layers.index("CheatsheetLayer"), layers.index("RoutingLayer") + 1)


class IndexTest(Fresh):
    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="almanac-sheet-")
        patch = mock.patch.dict(os.environ, {"ALMANAC_HOME": self.home})
        patch.start()
        for undo in (patch.stop, cheatsheet.index.cache_clear, lambda: shutil.rmtree(self.home, True)):
            self.addCleanup(undo)
        cheatsheet.index.cache_clear()

    def save(self, name="weekly-report", **over):
        playbook.save({"name": name, "title": "Weekly report", "when": "w", "triggers": ["weekly report"], "steps": ["Run it"], "source": "owner", **over})

    def sent(self, inject=True, model=MEDIUM, enabled=True, **kw):
        work = body(convo([{"text": "hello"}]), model, system=claude_system())
        there = ctx(**kw)
        there.cfg["playbooks"].update(inject=inject, enabled=enabled)
        transform(work, there)
        return work

    def test_nothing_without_playbooks(self):
        self.assertEqual(cheatsheet.index(), "")
        self.assertEqual(self.sent()["system"][-1]["text"], "Main prompt.\n\n" + cheatsheet.text_for(MEDIUM))

    def test_the_sheet_ends_with_the_playbooks_on_hand(self):
        self.save("b-report")
        self.save("a-report")
        listing = cheatsheet.index()
        self.assertIn("# Playbooks on hand", listing)
        self.assertLess(listing.index("- a-report: Weekly report"), listing.index("- b-report: Weekly report"))
        self.assertTrue(self.sent()["system"][-1]["text"].endswith(cheatsheet.text_for(MEDIUM) + "\n\n" + listing))

    def test_the_list_is_by_name_only_for_playbooks_that_apply_everywhere_with_one_line_titles(self):
        self.save("b-report", title="Weekly\nreport   here")
        self.save("a-report")
        self.save("c-report", project="some/project")
        playbook.bump("a-report")  # use counts must not change the order
        playbook.bump("b-report")
        playbook.bump("b-report")
        lines = cheatsheet.index().splitlines()[-2:]
        self.assertEqual(lines, ["- a-report: Weekly report", "- b-report: Weekly report here"])
        self.assertNotIn("c-report", cheatsheet.index())

    def test_main_thread_and_forks_get_the_list_other_threads_and_haiku_do_not(self):
        self.save()
        plain = cheatsheet.text_for(MEDIUM)
        self.assertIn("Playbooks on hand", self.sent()["system"][-1]["text"])
        fork = self.sent(klass="subagent", agent="a1", agent_type="fork")["system"][-1]["text"]
        self.assertEqual(fork, self.sent()["system"][-1]["text"])  # a fork shares its parent's cached prefix
        self.assertEqual(self.sent(klass="subagent", agent="a1")["system"][-1]["text"], "Main prompt.\n\n" + cheatsheet.text_for(MEDIUM, True))
        self.assertEqual(self.sent(model=SMALL)["system"][-1]["text"], "Main prompt.\n\n" + cheatsheet.text_for(SMALL))

    def test_not_added_when_inject_or_the_playbooks_are_off(self):
        self.save()
        self.assertIn("Playbooks on hand", json.dumps(self.sent()))
        self.assertEqual(self.sent(inject=False)["system"][-1]["text"], "Main prompt.\n\n" + cheatsheet.text_for(MEDIUM))
        self.assertEqual(self.sent(enabled=False)["system"][-1]["text"], "Main prompt.\n\n" + cheatsheet.text_for(MEDIUM, playbooks=False))  # playbooks off: the sheet stops pointing at them

    def test_the_list_follows_the_playbook_settings_not_the_cheatsheet(self):
        self.save()
        alone = "Main prompt.\n\n" + cheatsheet.index()
        self.assertEqual(self.sent(mode="off")["system"][-1]["text"], alone)  # the sheet is off, the list is not
        self.assertEqual(self.sent(model="some-model")["system"][-1]["text"], alone)  # a model with no sheet still gets it
        self.assertEqual(self.sent(mode="off", enabled=False)["system"], claude_system())

    def test_a_broken_playbook_folder_gives_nothing(self):
        Path(self.home, "playbooks").write_text("a file where the folder should be")
        self.assertEqual(cheatsheet.index(), "")


class CommandTest(Fresh):
    def test_show_and_path(self):
        with mock.patch("builtins.print") as shown:
            self.assertEqual(cli._cheatsheet(["show", "haiku"]), 0)
            self.assertEqual(cli._cheatsheet(["path"]), 0)
            self.assertEqual(cli._cheatsheet(["show", "gpt"]), 2)
        text = "\n".join(str(c.args[0]) for c in shown.call_args_list)
        self.assertIn(cheatsheet.text_for(SMALL), text)
        self.assertIn("haiku: about", text)
        self.assertIn(str(config.home() / "cheatsheets"), text)

    def test_show_agent(self):
        with mock.patch("builtins.print") as shown:
            self.assertEqual(cli._cheatsheet(["show", "agent"]), 0)
        text = "\n".join(str(c.args[0]) for c in shown.call_args_list)
        self.assertIn(cheatsheet.text_for(LARGE, True), text)
        self.assertIn("agent, Haiku: about", text)


class SkillsSheetTest(unittest.TestCase):
    def test_a_subagent_is_not_told_to_hand_work_on_and_playbooks_off_drops_their_line(self):
        self.assertEqual(cheatsheet.skills(), cheatsheet.read("skills"))
        self.assertNotIn("Hand a self-contained", cheatsheet.skills(sub=True))
        self.assertIn("almanac:adopt", cheatsheet.skills(sub=True))
        self.assertIn("playbook_run", cheatsheet.skills())
        self.assertNotIn("playbook_run", cheatsheet.skills(playbooks=False))


if __name__ == "__main__":
    unittest.main()

