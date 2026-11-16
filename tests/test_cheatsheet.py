import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import Rig, sse
from test_routing import LARGE, MAIN, MEDIUM, SMALL, SetProject, body, convo, sub_headers

from getalmanac import cheatsheet, cli, config, proxy as proxy_mod
from getalmanac.cheatsheet import CheatsheetLayer
from getalmanac.routing import RoutingLayer
from getalmanac.store import Store

IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
CACHE = {"type": "ephemeral"}


def claude_system():
    """The shape Claude Code sends: a billing line, its identity, then the main prompt, two of them cached."""
    return [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1"},
            {"type": "text", "text": IDENTITY, "cache_control": CACHE},
            {"type": "text", "text": "Main prompt.", "cache_control": CACHE}]


def ctx(klass="main", agent="", agent_type="", mode="on"):
    cfg = config.load()
    cfg["cheatsheet"]["mode"] = mode
    return SimpleNamespace(cfg=cfg, kind="messages", klass=klass, agent=agent, agent_type=agent_type)


def run(model, system=None, tools=None, **kw):
    work = body(convo([{"text": "hello"}]), model, tools=tools, system=claude_system() if system is None else system)
    CheatsheetLayer().transform(work, ctx(**kw))
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
            CheatsheetLayer().transform(work, ctx())
            self.assertEqual(work, once)

    def test_threads_get_it_and_other_requests_do_not(self):
        sheet = cheatsheet.text_for(MEDIUM)
        self.assertTrue(run(MEDIUM, klass="subagent", agent="a1")["system"][-1]["text"].endswith(sheet))
        self.assertTrue(run(MEDIUM, klass="subagent", agent="a1", agent_type="fork")["system"][-1]["text"].endswith(sheet))
        self.assertEqual(run(MEDIUM, tools=[])["system"], claude_system())  # titles and quota checks carry no tools
        self.assertEqual(run(MEDIUM, klass="title")["system"], claude_system())

    def test_mode_off(self):
        self.assertEqual(run(LARGE, mode="off")["system"], claude_system())


class ProxyTest(Fresh):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-sheet-"), "a.db"))
        self.addCleanup(self.store.close)

    def start(self, respond=None, mode="off"):
        self.rig = Rig(respond=respond, layers=[SetProject(), RoutingLayer(), CheatsheetLayer()], store=self.store,
                       overrides={"routing.mode": mode})
        self.addCleanup(self.rig.close)

    def system_of(self, index):
        return json.dumps(self.rig.sent(index)["system"])

    def test_a_conversation_sends_the_same_system_bytes_every_time(self):
        self.start()
        first = convo([{"text": "hi"}])
        self.rig.messages(body(first, SMALL, system=claude_system()), extra=MAIN)
        self.rig.messages(body(convo([{"text": "hi"}, {"text": "and more"}]), SMALL, system=claude_system()), extra=MAIN)
        self.rig.messages(body(first, SMALL, system=claude_system()), extra=sub_headers())
        raw = [r["body"] for r in self.rig.upstream.requests]
        cut = [b[b.index(b'"system"'):b.index(b'"tools"')] for b in raw]
        self.assertEqual(len(set(cut)), 1)
        self.assertIn(json.dumps(cheatsheet.text_for(SMALL))[1:-1].encode(), cut[0])

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
        with mock.patch.object(proxy_mod, "Proxy", side_effect=lambda cfg, store, layers: layers):
            layers = [type(layer).__name__ for layer in proxy_mod.build(config.load())]
        self.assertEqual(layers.index("CheatsheetLayer"), layers.index("RoutingLayer") + 1)


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


if __name__ == "__main__":
    unittest.main()
