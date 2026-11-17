import json
import os
import tempfile
import unittest

from helpers import Rig

from getalmanac import startup
from getalmanac.prefix import PrefixLayer
from getalmanac.store import Store

from test_startup import AGENT_LISTING, LISTING, tool

LONG = {"type": "ephemeral", "ttl": "1h"}
MCP = "# MCP Server Instructions\n\nThe following MCP servers have provided instructions:\n## docs\nUse docs."
ADVISOR = "# Advisor Tool\n\nYou have an advisor tool."
DEFERRED = startup.DEFER_TEXT + "\nWebFetch\nWebSearch"


def wrap(text):
    return {"type": "text", "text": "<system-reminder>\n" + text + "\n\n</system-reminder>"}


def stable_blocks(skip=()):
    parts = {"mcp": MCP, "agents": AGENT_LISTING, "skills": LISTING, "deferred": DEFERRED, "advisor": ADVISOR}
    return [wrap(t) for k, t in parts.items() if k not in skip]


def body(hook="hook one", prompt="hello there", skip=(), unknown=(), marks=3, extra=()):
    """What Claude Code sends first: volatile blocks in front of and between the stable ones."""
    content = [wrap("SessionStart:startup hook success: " + hook), wrap("Codebase and user instructions are shown below.\n" + hook)]
    stable = stable_blocks(skip)
    content += stable[:2] + [wrap(u) for u in unknown] + stable[2:]
    content += [wrap("Today's date is 2026-01-01."), {"type": "text", "text": prompt}]
    system = [{"type": "text", "text": "You are Claude Code", "cache_control": dict(LONG)}] if marks else [{"type": "text", "text": "You are"}]
    tools = [tool(n) for n in ("Bash", "Read", "ToolSearch")]
    out = {"model": "claude-test", "system": system, "tools": tools,
           "messages": [{"role": "user", "content": content}, *extra]}
    if marks > 1:
        tools[-1]["cache_control"] = dict(LONG)
    for i in range(max(0, marks - 2)):  # the rest ride on the last messages
        out["messages"].append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}", "cache_control": dict(LONG)}]})
        out["messages"].append({"role": "user", "content": [{"type": "text", "text": f"u{i}"}]})
    return out


def first(sent):
    return sent["messages"][0]["content"]


def heads(content):
    return [b["text"].split("\n")[1][:15] if b["text"].startswith("<system-reminder>") else b["text"][:15] for b in content]


def upto_mark(content):
    k = next(i for i, b in enumerate(content) if "cache_control" in b)
    return json.dumps(content[:k + 1])


class Case(unittest.TestCase):
    def rig(self, layers=None, on=True):
        store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-db-"), "a.db"))
        self.addCleanup(store.close)
        layers = [startup.SkillsLayer(), startup.AgentsLayer(), PrefixLayer()] if layers is None else layers
        rig = Rig(layers=layers, store=store, overrides={"startup.stable_first": on})
        self.addCleanup(rig.close)
        return rig


class PrefixTest(Case):
    def test_two_conversations_share_a_byte_identical_prefix_up_to_the_breakpoint(self):
        rig = self.rig()
        rig.messages(body("hook one", "fix the parser"), session="s-1")
        rig.messages(body("hook two, much longer " * 20, "write the docs"), session="s-2")
        a, b = (first(rig.sent(i)) for i in (0, 1))
        self.assertEqual(upto_mark(a), upto_mark(b))
        k = len(json.loads(upto_mark(a)))
        self.assertEqual(k, 5)  # all five stable blocks, in the fixed order
        self.assertEqual(a[k - 1]["cache_control"], LONG)
        self.assertEqual([h for h in heads(a)[:5]], ["# MCP Server In", "# Advisor Tool", "The following d", "Available agent", "The following s"])
        self.assertNotEqual(json.dumps(a), json.dumps(b))
        rec = rig.records()[0]["prefix"]
        self.assertEqual((rec["moved"], rec["mark"]), (3, True))  # agents and skills already sat at 3 and 4
        self.assertGreater(rec["chars"], 1000)

    def test_a_listing_chosen_by_the_typed_text_goes_after_the_breakpoint(self):
        rig = self.rig()
        rig.messages(body(prompt="make a slide deck presentation"), session="s-1")
        rig.messages(body(prompt="hello there"), session="s-2")
        a, b = (first(rig.sent(i)) for i in (0, 1))
        self.assertIn("slides", a[4]["text"])
        self.assertNotIn("cache_control", a[4])
        self.assertIn("cache_control", a[3])
        self.assertEqual(heads(a)[:5], ["# MCP Server In", "# Advisor Tool", "The following d", "Available agent", "The following s"])
        self.assertEqual(a[:3], b[:3])  # the other conversation, whose listing was not chosen, goes on to mark its listing
        self.assertEqual(a[3]["text"], b[3]["text"])

    def test_one_conversation_sends_the_same_first_message_every_time(self):
        rig = self.rig()
        grow = [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, {"role": "user", "content": "go on"}]
        for n in range(3):
            rig.messages(body(extra=grow * n), session="s-1")
        sent = [json.dumps(rig.sent(i)["messages"][0]) for i in range(3)]
        self.assertEqual(len(set(sent)), 1)
        self.assertIn("cache_control", sent[0])

    def test_off_by_default_changes_nothing(self):
        off, plain = self.rig(on=False), self.rig(layers=[startup.SkillsLayer(), startup.AgentsLayer()])
        off.messages(body())
        plain.messages(body())
        self.assertEqual(off.upstream.requests[0]["body"], plain.upstream.requests[0]["body"])
        self.assertNotIn("prefix", off.records()[0])

    def test_never_more_than_four_breakpoints(self):
        for marks, want in ((2, 3), (3, 4), (4, 4), (5, 5)):
            rig = self.rig()
            rig.messages(body(marks=marks), session="s-1")
            sent = json.dumps(rig.sent())
            self.assertEqual(sent.count("cache_control"), want, marks)
            self.assertEqual(rig.records()[0]["prefix"]["mark"], marks < 4, marks)
        rig = self.rig()  # no breakpoint to copy a ttl from: reorder only
        rig.messages(body(marks=0), session="s-1")
        self.assertNotIn("cache_control", json.dumps(rig.sent()))
        self.assertEqual(heads(first(rig.sent()))[0], "# MCP Server In")

    def test_missing_and_unknown_blocks(self):
        rig = self.rig()
        rig.messages(body(skip=("skills", "advisor"), unknown=("alpha note", "beta note")), session="s-1")
        got = first(rig.sent())
        self.assertEqual(heads(got), ["# MCP Server In", "The following d", "Available agent", "SessionStart:st", "Codebase and us", "alpha note", "beta note", "Today's date is", "hello there"])
        self.assertIn("cache_control", got[2])
        rig.messages(body(skip=("mcp", "agents", "skills", "deferred", "advisor")), session="s-2")
        self.assertEqual(json.dumps(rig.sent()["messages"]), json.dumps(body(skip=("mcp", "agents", "skills", "deferred", "advisor"))["messages"]))
        mixed = body()
        mixed["messages"][0]["content"][2] = wrap(MCP + "\n\nToday's date is 2026-01-01.")  # two sections in one block: not recognised
        rig.messages(mixed, session="s-3")
        self.assertEqual(heads(first(rig.sent()))[:2], ["# Advisor Tool", "The following d"])

    def test_a_changed_first_message_is_left_alone(self):
        rig = self.rig()
        rig.messages(body(), session="s-1")
        changed = body()
        changed["messages"][0]["content"][2]["text"] += " (changed)"  # same conversation, a stable block's bytes moved on
        rig.messages(changed, session="s-1")
        again = first(rig.sent())
        self.assertEqual(heads(again), heads(changed["messages"][0]["content"]))
        self.assertNotIn("cache_control", json.dumps(again))
        short = body(skip=("mcp",))
        rig.messages(short, session="s-1")
        self.assertEqual(heads(first(rig.sent())), heads(short["messages"][0]["content"]))

    def test_sub_agents_are_untouched(self):
        rig = self.rig()
        rig.messages(body(), session="s-1", extra={"x-claude-code-agent-id": "agent-7"})
        self.assertEqual(heads(first(rig.sent())), heads(body()["messages"][0]["content"]))
        self.assertNotIn("prefix", rig.records()[0])


if __name__ == "__main__":
    unittest.main()
