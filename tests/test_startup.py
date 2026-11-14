import copy
import json
import os
import tempfile
import unittest

from helpers import Rig, sse

from getalmanac import startup
from getalmanac.match import Index, tokens
from getalmanac.store import Store

LONG = " It also covers many adjacent situations and edge cases that take a lot of words to describe in full."
SKILLS = {
    "slides": "Create or edit a PowerPoint slide deck or presentation from an outline." + LONG * 3,
    "pdf-tools": "Read, merge, split or fill PDF documents and forms." + LONG * 3,
    "review:deep": "Review a pull request for correctness bugs before merging." + LONG * 3,
    "tiny": "Short one.",
    "charts": "Build a chart, graph or plot from tabular data.\nTRIGGER when the user mentions a dashboard." + LONG * 3,
}
LISTING = startup.SKILL_MARK + "\n\n" + "\n".join(f"- {n}: {d}" for n, d in SKILLS.items())
SESSION_TEXT = ("SessionStart hook: hi\n\n# Environment\n - Primary working directory: /repo/demo\n\n"
                + startup.DEFER_TEXT + "\nWebFetch\nWebSearch\n\n" + LISTING + "\n\nToday's date is 2026-01-01.")


def tool(name, text="does a thing " * 40):
    return {"name": name, "description": text, "input_schema": {"type": "object", "properties": {}},
            "eager_input_streaming": True}


def body(prompt="hello there", extra=(), tools=None):
    names = tools or ["Bash", "PowerShell", "Read", "ScheduleWakeup", "ToolSearch"]
    return {"model": "claude-test", "system": [{"type": "text", "text": "You are Claude Code"}],
            "tools": [tool(n) for n in names],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>rules</system-reminder>"},
                                                       {"type": "text", "text": prompt}]},
                         {"role": "system", "content": [{"type": "text", "text": SESSION_TEXT}]}, *extra]}


def listing_of(sent):
    text = sent["messages"][1]["content"][0]["text"]
    return dict(startup.parse_listing(text)[2])


class MatchTest(unittest.TestCase):
    def test_tokens_stem_and_drop_stop_words(self):
        self.assertEqual(tokens("Reviewing the pull requests"), ["review", "pull", "request"])

    def test_search_finds_the_relevant_entry_only(self):
        index = Index(SKILLS)
        self.assertEqual([n for n, _ in index.search("please make a slide deck for the board")], ["slides"])
        self.assertEqual(index.search("fix the failing test in parser.py"), [])

    def test_a_name_word_is_enough(self):
        self.assertEqual([n for n, _ in Index(SKILLS).search("look inside this pdf")], ["pdf-tools"])


class ListingTest(unittest.TestCase):
    def test_parse_reads_names_and_multiline_descriptions(self):
        start, end, entries = startup.parse_listing(SESSION_TEXT)
        self.assertEqual([n for n, _ in entries], list(SKILLS))
        self.assertEqual(dict(entries)["charts"], SKILLS["charts"])
        self.assertTrue(SESSION_TEXT[end:].startswith("\n\nToday's date"))

    def test_stub_cuts_at_a_word_and_keeps_short_text(self):
        self.assertEqual(startup.stub("Short one.", 90), "Short one.")
        cut = startup.stub(SKILLS["slides"], 40)
        self.assertTrue(cut.endswith("…") and len(cut) <= 41)
        self.assertTrue(SKILLS["slides"].startswith(cut[:-1]))


class LayerCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-db-"), "a.db"))
        self.rig = Rig(layers=[startup.SkillsLayer(), startup.ToolsLayer()], store=self.store)
        self.addCleanup(self.rig.close)
        self.addCleanup(self.store.close)


class SkillsLayerTest(LayerCase):
    def test_unmatched_skills_collapse_and_every_name_survives(self):
        self.rig.messages(body("fix the failing test in parser.py"))
        sent = listing_of(self.rig.sent())
        self.assertEqual(list(sent), list(SKILLS))
        self.assertTrue(sent["slides"].endswith("…"))
        self.assertEqual(sent["tiny"], "Short one.")
        text = self.rig.sent()["messages"][1]["content"][0]["text"]
        self.assertIn(startup.SKILL_NOTE, text)
        self.assertTrue(text.endswith("Today's date is 2026-01-01."))
        self.assertGreater(self.rig.records()[-1]["skills"]["saved_chars"], 500)

    def test_matched_skill_keeps_its_full_description(self):
        self.rig.messages(body("make a slide deck for the board"))
        sent = listing_of(self.rig.sent())
        self.assertEqual(sent["slides"], SKILLS["slides"])
        self.assertTrue(sent["pdf-tools"].endswith("…"))

    def test_decision_is_frozen_for_the_conversation(self):
        first = body("make a slide deck for the board")
        self.rig.messages(first)
        one = self.rig.upstream.requests[-1]["body"]
        self.store.mark_used("skill", "pdf-tools", "/repo/demo", "x1")  # history changes mid-conversation
        self.store.mark_used("skill", "pdf-tools", "/repo/demo", "x2")
        self.rig.messages(first)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], one)

    def test_later_message_gets_the_full_description_once(self):
        later = [{"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                 {"role": "user", "content": [{"type": "text", "text": "now merge these pdf forms"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                 {"role": "user", "content": "and fill another pdf form"}]
        self.rig.messages(body("fix the failing test", extra=later))
        sent = self.rig.sent()["messages"]
        note, human = sent[3]["content"]
        self.assertIn("- pdf-tools: " + SKILLS["pdf-tools"], note["text"])
        self.assertEqual(human["text"], "now merge these pdf forms")
        self.assertEqual(sent[5]["content"], "and fill another pdf form")  # already shown: not repeated

    def test_recently_used_skills_stay_full(self):
        self.store.mark_used("skill", "charts", "/repo/demo", "x1")
        self.store.mark_used("skill", "charts", "/repo/demo", "x2")
        self.rig.messages(body("fix the failing test"))
        self.assertEqual(listing_of(self.rig.sent())["charts"], SKILLS["charts"])

    def test_always_full_setting(self):
        self.rig.cfg["skills"]["always_full"] = ["review:deep"]
        self.rig.messages(body("hello"))
        self.assertEqual(listing_of(self.rig.sent())["review:deep"], SKILLS["review:deep"])

    def test_request_without_a_listing_is_untouched(self):
        raw = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_disabled_layer_leaves_the_listing(self):
        self.rig.cfg["skills"]["enabled"] = False
        self.rig.messages(body("hello"))
        self.assertEqual(listing_of(self.rig.sent())["slides"], SKILLS["slides"])


class ToolsLayerTest(LayerCase):
    def deferred(self):
        return sorted(t["name"] for t in self.rig.sent()["tools"] if t.get("defer_loading"))

    def test_cold_tools_are_deferred_and_announced(self):
        self.rig.messages(body("fix the failing test"))
        self.assertEqual(self.deferred(), ["PowerShell", "ScheduleWakeup"])
        text = self.rig.sent()["messages"][1]["content"][0]["text"]
        self.assertIn("\nWebFetch\nWebSearch\nPowerShell\nScheduleWakeup\n\n" + startup.SKILL_MARK, text)
        tools = {t["name"]: t for t in self.rig.sent()["tools"]}
        self.assertEqual(tools["PowerShell"]["description"], tool("x")["description"])  # schema is kept whole
        self.assertEqual(self.rig.records()[-1]["tools"]["deferred"], ["PowerShell", "ScheduleWakeup"])

    def test_announcement_is_added_when_the_client_has_no_list(self):
        request = body("fix the failing test")
        request["messages"][1]["content"][0]["text"] = "# Environment\n - Primary working directory: /repo/demo"
        self.rig.messages(request)
        first = self.rig.sent()["messages"][0]["content"][0]["text"]
        self.assertIn(startup.DEFER_MARK, first)
        self.assertIn("PowerShell\nScheduleWakeup", first)

    def test_powershell_stays_when_it_is_the_only_shell(self):
        self.rig.messages(body("hi", tools=["PowerShell", "Read", "ToolSearch"]))
        self.assertEqual(self.deferred(), [])

    def test_nothing_is_deferred_without_tool_search(self):
        self.rig.messages(body("hi", tools=["Bash", "PowerShell", "ScheduleWakeup"]))
        self.assertEqual(self.deferred(), [])

    def test_a_tool_the_message_names_stays_loaded(self):
        self.rig.messages(body("run this in powershell please"))
        self.assertEqual(self.deferred(), ["ScheduleWakeup"])

    def test_a_tool_already_called_stays_loaded(self):
        called = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "PowerShell", "input": {}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.rig.messages(body("fix it", extra=called))
        self.assertEqual(self.deferred(), ["ScheduleWakeup"])

    def test_use_in_two_conversations_promotes_a_tool(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"},
                                                 sse(tool=("ScheduleWakeup", {"delaySeconds": 60})))
        self.rig.messages(body("first conversation"))
        self.rig.messages(body("second conversation"))
        self.rig.records()
        self.rig.messages(body("third conversation"))
        self.assertEqual(self.deferred(), ["PowerShell"])

    def test_hot_setting_wins(self):
        self.rig.cfg["tools"]["hot"] = ["PowerShell"]
        self.rig.messages(body("hi"))
        self.assertEqual(self.deferred(), ["ScheduleWakeup"])

    def test_same_request_is_rewritten_to_the_same_bytes(self):
        request = body("make a slide deck")
        self.rig.messages(copy.deepcopy(request))
        self.rig.messages(copy.deepcopy(request))
        a, b = (r["body"] for r in self.rig.upstream.requests[-2:])
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
