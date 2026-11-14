import copy
import json
import os
import tempfile
import unittest

from helpers import Rig, sse

from getalmanac import compact, startup
from getalmanac.compact import CompactLayer
from getalmanac.memory import Memory, MemoryLayer, ProjectLayer
from getalmanac.store import Store

SESSION_TEXT = "# Environment\n - Primary working directory: /nowhere/demo\n"
TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]


def human(text, marker=False):
    block = {"type": "text", "text": text}
    if marker:
        block["cache_control"] = {"type": "ephemeral"}
    return {"role": "user", "content": [{"type": "text", "text": "<system-reminder>rules v1</system-reminder>"}, block]}


def turn(n):
    """One finished exchange: a question, a tool call, its result, a final answer."""
    return [human(f"Question {n}: how does part {n} of the importer work?"),
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "", "signature": f"sig{n}"},
                {"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": f"cat part{n}.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": f"source of part {n} " * 30}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"Part {n} parses rows and the codeword is zebra{n}."}]}]


def history(turns, last="And what about the exporter?"):
    first = turn(1)
    system = {"role": "system", "content": [
        {"type": "text", "text": SESSION_TEXT},
        {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "extra"}, "cache_control": {"type": "ephemeral"}}]}
    messages = [first[0], system, *first[1:]]
    for n in range(2, turns + 1):
        messages += turn(n)
    return messages + [human(last, marker=True)]


def body(messages):
    return {"model": "m", "system": "You are Claude Code", "tools": TOOLS, "messages": messages}


class FingerprintTest(unittest.TestCase):
    def test_ignores_cache_markers_reminders_and_thinking(self):
        a = history(2)
        b = copy.deepcopy(a)
        b[0]["content"][0]["text"] = "<system-reminder>rules v2</system-reminder>"
        b[0]["content"][1]["cache_control"] = {"type": "ephemeral"}
        b[2]["content"][0]["signature"] = "changed"
        self.assertEqual(compact.fingerprint(a), compact.fingerprint(b))
        b[5]["content"][0]["text"] = "edited answer"
        self.assertNotEqual(compact.fingerprint(a), compact.fingerprint(b))


class PrefixTest(unittest.TestCase):
    def test_prefix_keeps_session_context_and_recent_turns(self):
        messages = history(6)
        prefix = compact.build_prefix(messages, len(messages) - 1, {0: 41, 5: 46}, budget_chars=6000)
        self.assertEqual([m["role"] for m in prefix], ["user", "system", "assistant"])
        self.assertEqual(prefix[2]["content"][0]["text"], compact.ACK)
        opening, summary = prefix[0]["content"]
        self.assertEqual(opening["text"], "<system-reminder>rules v1</system-reminder>")
        self.assertTrue(summary["text"].startswith("<system-reminder>\nAlmanac compacted this conversation"))
        self.assertIn("- [41] Question 1: how does part 1 of the importer work? -> Part 1 parses rows", summary["text"])
        self.assertIn("--- Exchange 6 [46] ---", summary["text"])
        self.assertIn("[tool Bash]", summary["text"])
        self.assertIn("codeword is zebra6", summary["text"])
        self.assertEqual(prefix[1]["content"][0]["text"], SESSION_TEXT)
        self.assertEqual(prefix[1]["content"][1]["tool"]["name"], "extra")
        self.assertNotIn("cache_control", json.dumps(prefix))
        self.assertNotIn("thinking", json.dumps(prefix))

    def test_a_small_budget_still_keeps_the_last_exchange_and_says_what_is_missing(self):
        messages = history(30)
        text = compact.build_prefix(messages, len(messages) - 1, {}, budget_chars=3000)[0]["content"][1]["text"]
        self.assertIn("--- Exchange 30 ---", text)
        self.assertIn("earlier exchanges not listed", text)


class LayerCase(unittest.TestCase):
    trigger = 2000

    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-cmp-"), "a.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        layers = [ProjectLayer(self.memory), CompactLayer(self.memory), MemoryLayer(self.memory),
                  startup.SkillsLayer(), startup.ToolsLayer()]
        self.rig = Rig(layers=layers, store=self.store,
                       overrides={"compaction.trigger_tokens": self.trigger, "compaction.target_tokens": 1500,
                                  "memory.recall": False})
        self.addCleanup(self.rig.close)

    def send(self, messages):
        self.rig.messages(body(copy.deepcopy(messages)))
        return self.rig.sent()["messages"]


class CompactLayerTest(LayerCase):
    def test_small_conversations_are_untouched(self):
        raw = json.dumps(body(history(1))).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_a_large_conversation_is_compacted_at_a_human_turn(self):
        messages = history(8)
        sent = self.send(messages)
        # a system message is only accepted directly before an assistant message
        self.assertEqual([m["role"] for m in sent], ["user", "system", "assistant", "user"])
        self.assertEqual(sent[-1], messages[-1])  # the new message goes out exactly as typed
        self.assertIn("Almanac compacted this conversation", sent[0]["content"][-1]["text"])
        self.assertLess(len(json.dumps(sent)), len(json.dumps(messages)) * 0.6)
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["covered"], record["fresh"]), (len(messages) - 1, True))

    def test_covered_exchanges_can_be_read_back_from_memory(self):
        sent = self.send(history(8))
        summary = sent[0]["content"][-1]["text"]
        first = int(summary.split("- [", 1)[1].split("]", 1)[0])
        self.assertIn("source of part 1", self.memory.detail(first))
        self.assertIn("zebra1", self.memory.read(first)["text"])

    def test_not_in_the_middle_of_a_tool_loop(self):
        messages = history(8)[:-1] + turn(9)[:3]  # ends on a tool result
        raw = json.dumps(body(messages)).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_the_prefix_is_reused_byte_for_byte_as_the_conversation_grows(self):
        messages = history(8)
        first = self.send(messages)
        grown = messages + [{"role": "assistant", "content": [{"type": "text", "text": "The exporter mirrors it."}]},
                            human("Thanks, one more thing")]
        grown[-3]["content"][1].pop("cache_control")  # the client moves its cache marker along
        second = self.send(grown)
        self.assertEqual(json.dumps(second[:3]), json.dumps(first[:3]))
        self.assertEqual(second[3:], grown[len(messages) - 1:])
        self.assertFalse(self.rig.records()[-1]["compaction"]["fresh"])

    def test_it_waits_for_growth_before_compacting_again(self):
        # The reply reports a size over the trigger, but almost nothing was added since.
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"},
                                                 sse(usage={"input_tokens": self.trigger * 3}))
        messages = history(8)
        self.send(messages)
        self.rig.records()
        grown = messages + [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, human("and then?")]
        self.send(grown)
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["covered"], record["fresh"]), (len(messages) - 1, False))

    def test_a_refused_compaction_is_dropped_but_other_savings_stay(self):
        def respond(req):
            if b"Almanac compacted this conversation" in req["body"]:
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"type":"invalid_request_error"}}'
            return 200, {"content-type": "text/event-stream"}, sse()

        self.rig.upstream.respond = respond
        messages = history(8)
        messages[1]["content"][0]["text"] += ("\n" + startup.SKILL_MARK + "\n\n- deck: "
                                              + "Make a slide deck from an outline. " * 12 + "\n\nToday's date is 2026-01-01.")
        sent = self.send(messages)
        self.assertEqual(len(self.rig.upstream.requests), 2)
        self.assertEqual(len(sent), len(messages))  # the full history went out
        self.assertIn("deck: Make a slide deck", sent[1]["content"][0]["text"])
        self.assertIn("…", sent[1]["content"][0]["text"])  # still collapsed by the skills layer
        record = self.rig.records()[-1]
        self.assertIn("invalid_request_error", record["fallback"])
        self.assertNotIn("compaction", record)
        self.send(messages)  # and it is not tried again for this conversation
        self.assertEqual(len(self.rig.upstream.requests), 3)

    def test_an_edit_to_covered_history_drops_the_prefix(self):
        messages = history(8)
        self.send(messages)
        edited = copy.deepcopy(messages[:9]) + [human("a short different question")]
        edited[5]["content"][0]["text"] = "A rewritten earlier answer."
        self.rig.cfg["compaction"]["trigger_tokens"] = 10 ** 9
        sent = self.send(edited)
        self.assertEqual(sent, edited)

    def test_it_compacts_again_when_it_outgrows_the_trigger(self):
        messages = history(8)
        self.send(messages)
        grown = messages + [m for n in range(20, 34) for m in turn(n)[1:] + [human(f"Next question {n} about the importer")]]
        sent = self.send(grown)
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["covered"], record["fresh"]), (len(grown) - 1, True))
        self.assertEqual(sent[-1], grown[-1])
        self.assertIn("--- Exchange", sent[0]["content"][-1]["text"])

    def test_the_last_reply_s_exact_count_decides(self):
        self.rig.cfg["compaction"]["trigger_tokens"] = 50000
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"},
                                                 sse(usage={"input_tokens": 49990, "cache_read_input_tokens": 0}))
        messages = history(3)
        self.assertEqual(self.send(messages), messages)  # small by size, and no count yet
        self.rig.records()
        grown = messages + [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, human("go on then please")]
        self.assertEqual([m["role"] for m in self.send(grown)], ["user", "system", "assistant", "user"])

    def test_subagents_are_left_alone(self):
        raw = json.dumps(body(history(8))).encode()
        self.rig.messages(raw, extra={"x-claude-code-agent-id": "a1"})
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_disabled(self):
        self.rig.cfg["compaction"]["enabled"] = False
        messages = history(8)
        self.assertEqual(self.send(messages), messages)


if __name__ == "__main__":
    unittest.main()
