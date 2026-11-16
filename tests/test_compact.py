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


class OutlineByStatusTest(unittest.TestCase):
    def outline(self, statuses, turns=12, budget=12000, told=None, ids=None):
        messages = history(turns)
        prefix = compact.build_prefix(messages, len(messages) - 1, ids or {n: 100 + n for n in range(turns)}, budget, told, statuses)
        return prefix[0]["content"][-1]["text"]

    def test_sections_come_in_order_and_superseded_and_noise_are_only_counted(self):
        statuses = {0: "decision", 1: "superseded", 2: "noise", 3: "open", 4: "done", 5: "done", 6: "decision"}
        text = self.outline(statuses, budget=6000)
        marks = ["Standing decisions, oldest first:", "Open work:", "Done, newest first:",
                 "2 earlier exchanges marked superseded or noise are not listed", "The most recent exchanges before this point, in full:"]
        at = [text.index(m) for m in marks]
        self.assertEqual(at, sorted(at))
        decisions, rest = text.split("Open work:")[0], text.split("Open work:")[1]
        self.assertLess(decisions.index("[100] Question 1"), decisions.index("[106] Question 7"))  # oldest first
        done = rest.split("Done, newest first:")[1].split("2 earlier exchanges")[0]
        self.assertLess(done.index("[105] Question 6"), done.index("[104] Question 5"))  # newest first
        for hidden in ("Question 2:", "Question 3:"):
            self.assertNotIn(f"] {hidden}", text)
        self.assertIn("through the memory tools", text)

    def test_open_work_keeps_the_longer_summary_and_done_is_cut(self):
        long = "The retry cap is five and the backoff doubles each time. " * 8
        told = {3: ("Retry helper", long), 4: ("Cache helper", long)}
        text = self.outline({3: "open", 4: "done"}, told=told, budget=6000)
        opened = [l for l in text.splitlines() if l.startswith("- [103]")][0]
        closed = [l for l in text.splitlines() if l.startswith("- [104]")][0]
        self.assertGreater(len(opened), len(closed) + 100)
        self.assertTrue(closed.endswith("…"))

    def test_decisions_stay_when_the_room_is_short(self):
        statuses = {n: "decision" if n in (0, 5) else "done" for n in range(30)}
        text = self.outline(statuses, turns=30, budget=3000)
        self.assertIn("[100] Question 1", text)
        self.assertIn("[105] Question 6", text)
        self.assertRegex(text, r"\(\d+ more finished exchanges not listed; find them with memory_search\)")
        self.assertNotIn("more standing decisions", text)

    def test_when_decisions_alone_overflow_the_oldest_stay(self):
        text = self.outline({n: "decision" for n in range(40)}, turns=40, budget=6000)
        listed = [l for l in text.splitlines() if l.startswith("- [1")]
        self.assertLess(len(listed), 39)
        self.assertTrue(listed[0].startswith("- [100] Question 1"))
        self.assertRegex(text, r"\(\d+ more standing decisions not listed")
        self.assertNotIn("Done, newest first", text)

    def test_without_statuses_the_outline_is_the_old_one(self):
        messages = history(12)
        old = compact.build_prefix(messages, len(messages) - 1, {0: 41}, 6000)
        self.assertEqual(compact.build_prefix(messages, len(messages) - 1, {0: 41}, 6000, None, {}), old)
        self.assertEqual(compact.build_prefix(messages, len(messages) - 1, {0: 41}, 6000, {}, None), old)
        self.assertIn("Earlier exchanges, oldest first:", old[0]["content"][-1]["text"])
        self.assertNotIn("Standing decisions", old[0]["content"][-1]["text"])


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

    def test_the_stored_statuses_shape_the_prefix_and_a_later_change_waits_for_the_next_compaction(self):
        messages = history(8)
        messages[9]["content"][1]["text"] = "Question 3: from now on answer every question with a codeword"  # turn 3 is a decision
        first = self.send(messages)
        text = first[0]["content"][-1]["text"]
        self.assertIn("Standing decisions, oldest first:", text)
        self.assertIn("Question 3: from now on", text.split("Done, newest first:")[0])
        lane = self.rig.records()[-1]["lane"]
        self.store.run("update node set status = 'noise'")  # a status that changes later
        grown = messages + [{"role": "assistant", "content": [{"type": "text", "text": "The exporter mirrors it."}]},
                            human("Thanks, one more thing")]
        self.assertEqual(json.dumps(self.send(grown)[:3]), json.dumps(first[:3]))  # the stored prefix does not move
        self.assertEqual(set(self.memory.statuses(lane).values()), {"noise"})

    def test_covered_exchanges_can_be_read_back_from_memory(self):
        sent = self.send(history(8))
        summary = sent[0]["content"][-1]["text"]
        first = int(summary.split("] Question 1:", 1)[0].rsplit("[", 1)[1])  # the done section lists the newest first
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

    def test_a_size_refusal_does_not_switch_compaction_off_but_another_one_does(self):
        refusal = [b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 250000 tokens > 200000 maximum"}}']

        def respond(req):
            if b"Almanac compacted this conversation" in req["body"]:
                return 400, {"content-type": "application/json"}, refusal[0]
            return 200, {"content-type": "text/event-stream"}, sse()

        self.rig.upstream.respond = respond
        self.send(history(8))
        lane = self.rig.records()[-1]["lane"]
        self.assertIsNone(self.store.get(f"nocompact:{lane}"))
        self.assertIn("prompt is too long", self.rig.records()[-1]["fallback"])
        refusal[0] = b'{"type":"error","error":{"type":"invalid_request_error","message":"bad block"}}'
        other = history(8)
        other[0]["content"][1]["text"] = "A different first question"  # a new conversation: the first one is no longer rewritten
        self.send(other)
        lane = self.rig.records()[-1]["lane"]
        self.assertIn("bad block", self.store.get(f"nocompact:{lane}"))

    def test_an_edit_to_covered_history_drops_the_prefix(self):
        messages = history(8)
        self.send(messages)
        edited = copy.deepcopy(messages[:9]) + [human("a short different question")]
        edited[5]["content"][0]["text"] = "A rewritten earlier answer."
        self.rig.cfg["compaction"]["trigger_tokens"] = 10 ** 9
        sent = self.send(edited)
        self.assertEqual(sent, edited)

    def test_an_edit_in_place_does_not_keep_trusting_the_old_count(self):
        messages = history(8)
        self.send(messages)
        self.rig.records()  # the reply stored a count that described the compacted body
        edited = copy.deepcopy(messages)
        edited[5]["content"][0]["text"] = "A rewritten earlier answer."  # same length: only the content changed
        sent = self.send(edited)
        self.assertEqual([m["role"] for m in sent], ["user", "system", "assistant", "user"])  # compacted again
        self.assertEqual(self.rig.records()[-1]["compaction"]["fresh"], True)

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
