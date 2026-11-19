import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import Rig, sse

from getalmanac import config, library, startup
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
AGENTS = {
    "claude": "Catch-all for any task that does not fit a more specific agent. (Tools: *)",
    "crew:db-reviewer": "Reviews database schemas, migrations and SQL queries for correctness." + LONG + " (Tools: Read, Grep, Bash)",
    "infra-planner": "Plans terraform infrastructure changes before they are applied." + LONG + " (Tools: Read, Glob)",
    **{f"helper-{i}": f"Handles routine chore number {i}." + LONG + " (Tools: Read, Edit)" for i in range(4)},
}
AGENT_LISTING = startup.AGENT_MARK + "\n" + "\n".join(f"- {n}: {d}" for n, d in AGENTS.items())
SESSION_TEXT = ("SessionStart hook: hi\n\n# Environment\n - Primary working directory: /repo/demo\n\n"
                + startup.DEFER_TEXT + "\nWebFetch\nWebSearch\n\n" + LISTING + "\n\n" + AGENT_LISTING
                + "\n\nToday's date is 2026-01-01.")


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


def listing_of(sent, mark=startup.SKILL_MARK):
    text = sent["messages"][1]["content"][0]["text"]
    return dict(startup.parse_listing(text, mark)[2])


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
        self.assertTrue(SESSION_TEXT[end:].startswith("\n\nAvailable agent types"))

    def test_stub_cuts_at_a_word_and_keeps_short_text(self):
        self.assertEqual(startup.stub("Short one.", 90), "Short one.")
        cut = startup.stub(SKILLS["slides"], 40)
        self.assertTrue(cut.endswith("…") and len(cut) <= 41)
        self.assertTrue(SKILLS["slides"].startswith(cut[:-1]))


class LayerCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-db-"), "a.db"))
        self.rig = Rig(layers=[startup.SkillsLayer(), startup.AgentsLayer(), startup.ToolsLayer()], store=self.store)
        self.addCleanup(self.rig.close)
        self.addCleanup(self.store.close)
        library.setup(self.store)

    def used(self, kind, name, *convos, project="/repo/demo"):
        """Library rows of exchanges in these conversations that used `name`: what the pinning reads."""
        for convo in convos:
            self.store.run(f"insert into library (convo, seq, project, prompt, {kind}s, calls, created, updated) values (?, ?, ?, 'p', ?, 1, 1, 1)",
                           (convo, len(self.store.run("select 1 from library where convo = ?", (convo,))), project, json.dumps([name])))


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

    def test_matched_skill_is_shown_in_full_next_to_the_message(self):
        self.rig.messages(body("make a slide deck for the board"))
        sent = listing_of(self.rig.sent())
        self.assertTrue(sent["slides"].endswith("…"))
        self.assertIn("- slides: " + SKILLS["slides"], self.rig.sent()["messages"][0]["content"][1]["text"])

    def test_decision_is_frozen_for_the_conversation(self):
        first = body("make a slide deck for the board")
        self.rig.messages(first)
        one = self.rig.upstream.requests[-1]["body"]
        self.used("skill", "pdf-tools", 1, 2)  # history changes mid-conversation
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

    def test_new_conversations_share_one_listing_and_the_match_goes_to_the_turn(self):
        self.rig.messages(body("make a slide deck for the board"), session="s1")
        self.rig.messages(body("look inside this pdf"), session="s2")
        a, b = self.rig.sent(-2), self.rig.sent(-1)
        self.assertEqual(a["messages"][1], b["messages"][1])
        self.assertTrue(listing_of({"messages": a["messages"]})["slides"].endswith("…"))
        note = a["messages"][0]["content"][-2]["text"]
        self.assertIn("- slides: " + SKILLS["slides"], note)
        self.assertIn("- pdf-tools: " + SKILLS["pdf-tools"], b["messages"][0]["content"][-2]["text"])
        self.assertEqual(self.rig.records()[-1]["skills"]["first_full"], 0)

    def test_recently_used_skills_stay_full(self):
        self.used("skill", "charts", 1, 2)
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


class AgentsLayerTest(LayerCase):
    def agents(self):
        return listing_of(self.rig.sent(), startup.AGENT_MARK)

    def alone(self, layer=None):
        """A rig where only the agents layer runs, so an untouched request is byte for byte what was sent."""
        rig = Rig(layers=[layer or startup.AgentsLayer()], store=self.store)
        self.addCleanup(rig.close)
        return rig

    def test_unmatched_agents_collapse_and_every_name_survives(self):
        self.rig.messages(body("fix the failing test in parser.py"))
        sent = self.agents()
        self.assertEqual(list(sent), list(AGENTS))
        stubbed = sent["crew:db-reviewer"]
        self.assertTrue(stubbed.endswith("…") and "Tools" not in stubbed and len(stubbed) <= 61)
        self.assertEqual(sent["claude"], AGENTS["claude"])  # already short: kept whole, with its tools
        text = self.rig.sent()["messages"][1]["content"][0]["text"]
        self.assertIn(startup.AGENT_NOTE, text)
        self.assertTrue(text.endswith("Today's date is 2026-01-01."))
        self.assertGreater(self.rig.records()[-1]["agents"]["saved_chars"], 400)
        self.assertTrue(listing_of(self.rig.sent())["slides"].endswith("…"))  # the skills layer still ran

    def test_agents_used_in_two_conversations_stay_full_in_a_new_one(self):
        self.used("agent", "infra-planner", 1, 2)
        self.used("agent", "crew:db-reviewer", 3)  # one conversation is not a habit
        self.rig.messages(body("fix the failing test in parser.py"))
        sent = self.agents()
        self.assertEqual(sent["infra-planner"], AGENTS["infra-planner"])
        self.assertTrue(sent["crew:db-reviewer"].endswith("…"))

    def test_matched_agent_is_shown_in_full_next_to_the_message(self):
        self.rig.messages(body("review this database migration query"))
        sent = self.agents()
        self.assertTrue(sent["crew:db-reviewer"].endswith("…"))
        self.assertIn("- crew:db-reviewer: " + AGENTS["crew:db-reviewer"], " ".join(b["text"] for b in self.rig.sent()["messages"][0]["content"]))

    def test_same_request_is_rewritten_to_the_same_bytes(self):
        request = body("fix the failing test")
        self.rig.messages(copy.deepcopy(request))
        self.rig.messages(copy.deepcopy(request))
        a, b = (r["body"] for r in self.rig.upstream.requests[-2:])
        self.assertEqual(a, b)

    def test_later_message_restores_the_full_line_once(self):
        later = [{"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                 {"role": "user", "content": [{"type": "text", "text": "now plan the terraform infrastructure changes"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                 {"role": "user", "content": "and the terraform infrastructure changes again"}]
        self.rig.messages(body("fix the failing test", extra=later))
        sent = self.rig.sent()["messages"]
        note, human = sent[3]["content"]
        self.assertIn("- infra-planner: " + AGENTS["infra-planner"], note["text"])
        self.assertEqual(human["text"], "now plan the terraform infrastructure changes")
        self.assertEqual(sent[5]["content"], "and the terraform infrastructure changes again")

    def test_listing_without_entries_is_untouched(self):
        request = body("hello")
        request["messages"][1]["content"][0]["text"] = startup.AGENT_MARK + "\n(none)\n\nToday's date is 2026-01-01."
        raw = json.dumps(request).encode()
        rig = self.alone()
        rig.messages(raw)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)

    def test_zero_turns_the_layer_off(self):
        self.rig.cfg["startup"]["agent_chars"] = 0
        self.rig.messages(body("hello"))
        self.assertEqual(self.agents(), AGENTS)
        self.assertNotIn("agents", self.rig.records()[-1])

    def test_disable_list_names_agents(self):
        with mock.patch.dict(os.environ, {"ALMANAC_DISABLE": "agents"}):
            cfg = config.load()
        self.assertEqual(cfg["startup"]["agent_chars"], 0)
        self.assertTrue(cfg["skills"]["enabled"])

    def test_disable_all_leaves_no_layer_that_changes_a_request(self):
        with mock.patch.dict(os.environ, {"ALMANAC_DISABLE": "all"}):
            cfg = config.load()
        self.assertEqual([cfg[name]["enabled"] for name in ("skills", "tools", "memory", "compaction", "playbooks")], [False] * 5)
        self.assertEqual((cfg["startup"]["agent_chars"], cfg["cheatsheet"]["mode"]), (0, "off"))

    def test_a_malformed_block_fails_open(self):
        request = body("hello")
        request["messages"][1]["content"][0]["text"] = 5
        raw = json.dumps(request).encode()
        rig = self.alone()
        rig.messages(raw)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        self.assertIn("TypeError", rig.records()[-1]["error"])

    def test_a_layer_that_raises_fails_open(self):
        layer = startup.AgentsLayer()
        layer.shorten = lambda *args: 1 / 0
        rig = self.alone(layer)
        raw = json.dumps(body("hello")).encode()
        rig.messages(raw)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        self.assertIn("ZeroDivisionError", rig.records()[-1]["error"])


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

    def test_a_tool_the_message_names_is_still_deferred(self):  # ToolSearch loads it; the tool list must not depend on the text
        self.rig.messages(body("run this in powershell please"))
        self.assertEqual(self.deferred(), ["PowerShell", "ScheduleWakeup"])

    def test_a_tool_already_called_stays_loaded(self):
        called = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "PowerShell", "input": {}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.rig.messages(body("fix it", extra=called))
        self.assertEqual(self.deferred(), ["ScheduleWakeup"])

    def test_use_in_two_conversations_promotes_a_tool(self):
        self.used("tool", "ScheduleWakeup", 1, 2)
        with mock.patch.object(startup, "_today", return_value="2099-01-01"):  # the project's snapshot is taken per day
            self.rig.messages(body("third conversation"))
        self.assertEqual(self.deferred(), ["PowerShell"])

    def test_new_conversations_of_a_project_get_identical_tools(self):
        self.rig.messages(body("fix the failing test"), session="s1")
        self.used("tool", "ScheduleWakeup", 1, 2)  # usage changes between them
        self.rig.messages(body("run this in powershell please"), session="s2")
        a, b = (json.dumps(self.rig.sent(i)["tools"]) for i in (-2, -1))
        self.assertEqual(a, b)

    def test_a_fork_sends_the_same_tools_as_its_parent(self):  # it shares the parent's cached prefix
        called = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "PowerShell", "input": {}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.rig.messages(body("fix it"))
        self.rig.messages(body("fix it", extra=called), extra={
            "x-claude-code-agent-id": "a1", "x-claude-code-agent-type": "fork", "x-claude-code-request-class": "subagent"})
        parent, fork = (json.dumps(self.rig.sent(i)["tools"]) for i in (-2, -1))
        self.assertEqual(parent, fork)

    def test_a_fork_of_a_raw_parent_is_sent_raw_too(self):  # a parent sent unchanged must not get a rewritten fork
        called = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.rig.messages(body("fix it"))
        self.rig.proxy.fuse(self.rig.records()[-1]["lane"], "refused")
        self.rig.messages(body("fix it"))
        self.rig.messages(body("fix it", extra=called), extra={
            "x-claude-code-agent-id": "a1", "x-claude-code-agent-type": "fork", "x-claude-code-request-class": "subagent"})
        parent, fork = (json.dumps(self.rig.sent(i)["tools"]) for i in (-2, -1))
        self.assertEqual(parent, fork)

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
