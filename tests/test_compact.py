import copy
import json
import os
import tempfile
import unittest

from helpers import Rig, sse

from getalmanac import compact, startup
from getalmanac.compact import CompactLayer
from getalmanac.memory import Memory, MemoryLayer, ProjectLayer
from getalmanac.store import State, Store

SESSION_TEXT = "# Environment\n - Primary working directory: /nowhere/demo\n"
TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]


def summary(prefix):
    """The summary blocks of a built or sent prefix (outline generations, then the recent exchanges) as one text."""
    return "\n\n".join(b["text"] for b in prefix[0]["content"][1:])


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
        opening, gen, recent = prefix[0]["content"]  # the reminders, one outline generation, the recent exchanges
        self.assertEqual(opening["text"], "<system-reminder>rules v1</system-reminder>")
        self.assertTrue(gen["text"].startswith("<system-reminder>\nAlmanac compacted this conversation"))
        self.assertIn("- [41] Question 1: how does part 1 of the importer work? -> Part 1 parses rows", gen["text"])
        self.assertIn("--- Exchange 6 [46] ---", recent["text"])
        self.assertIn("[tool Bash]", recent["text"])
        self.assertIn("codeword is zebra6", recent["text"])
        self.assertEqual(prefix[1]["content"][0]["text"], SESSION_TEXT)
        self.assertEqual(prefix[1]["content"][1]["tool"]["name"], "extra")
        self.assertNotIn("cache_control", json.dumps(prefix))
        self.assertNotIn("thinking", json.dumps(prefix))

    def test_a_small_budget_still_keeps_the_last_exchange_and_says_what_is_missing(self):
        messages = history(30)
        text = summary(compact.build_prefix(messages, len(messages) - 1, {}, budget_chars=3000))
        self.assertIn("--- Exchange 30 ---", text)
        self.assertIn("earlier exchanges not listed", text)


class OutlineByStatusTest(unittest.TestCase):
    def outline(self, statuses, turns=12, budget=12000, told=None, ids=None):
        messages = history(turns)
        prefix = compact.build_prefix(messages, len(messages) - 1, ids or {n: 100 + n for n in range(turns)}, budget, told, statuses)
        return summary(prefix)

    def test_related_hits_take_their_room_from_the_budget_once(self):
        hits = [{"id": 100 + n, "title": f"Question {n + 1}", "snippet": "x" * 190} for n in range(5)]
        messages, ids = history(60), {n: 100 + n for n in range(60)}
        def done(related):
            text = summary(compact.build_prefix(messages, len(messages) - 1, ids, 12000, None, {n: "done" for n in range(60)}, related))
            return text.split("Done, newest first:")[1].split("\n\n")[0].count("\n- [")
        note = len(compact.RELATED_HEAD) + sum(len(f"- [{h['id']}] {h['title']} -> {h['snippet']}") + 1 for h in hits)
        self.assertGreater(done([]), 5)
        self.assertLessEqual(done([]) - done(hits), note // 100 + 1)  # a done line is about 100 characters

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
        self.assertIn("Earlier exchanges, oldest first:", summary(old))
        self.assertNotIn("Standing decisions", summary(old))


class LayerCase(unittest.TestCase):
    trigger = 2000

    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(prefix="almanac-cmp-"), "a.db"))
        self.state = State(self.store.path.with_name("state.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, self.state)
        layers = [ProjectLayer(self.memory), CompactLayer(self.memory), MemoryLayer(self.memory),
                  startup.SkillsLayer(), startup.ToolsLayer()]
        self.rig = Rig(layers=layers, store=self.store, state=self.state,
                       overrides={"compaction.trigger_tokens": self.trigger, "compaction.target_tokens": 1500,
                                  "memory.recall": False, "memory.summary_model": ""})
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
        self.assertIn("Almanac compacted this conversation", summary(sent))
        self.assertLess(len(json.dumps(sent)), len(json.dumps(messages)) * 0.6)
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["covered"], record["fresh"]), (len(messages) - 1, True))

    def test_the_stored_statuses_shape_the_prefix_and_a_later_change_waits_for_the_next_compaction(self):
        messages = history(8)
        messages[9]["content"][1]["text"] = "Question 3: from now on answer every question with a codeword"  # turn 3 is a decision
        first = self.send(messages)
        text = summary(first)
        self.assertIn("Standing decisions, oldest first:", text)
        self.assertIn("Question 3: from now on", text.split("Done, newest first:")[0])
        lane = self.rig.records()[-1]["lane"]
        self.store.run("update node set status = 'noise'")  # a status that changes later
        grown = messages + [{"role": "assistant", "content": [{"type": "text", "text": "The exporter mirrors it."}]},
                            human("Thanks, one more thing")]
        self.assertEqual(json.dumps(self.send(grown)[:3]), json.dumps(first[:3]))  # the stored prefix does not move
        self.assertEqual(set(self.memory.statuses(lane).values()), {"noise"})

    def test_the_outline_lists_earlier_exchanges_related_to_the_new_message(self):
        messages = history(40, last="fix the rate limiter again")
        messages[9]["content"][1]["text"] = "Question 3: why does the rate limiter drop bursts?"
        text = summary(self.send(messages))
        heading = "Earlier exchanges related to your new message:"
        self.assertIn(heading, text)
        self.assertIn("Question 3: why does the rate limiter drop bursts?", text.split(heading)[1].split("\n\n")[0])
        lane = self.rig.records()[-1]["lane"]
        first = json.dumps(self.state.get(f"compact:{lane}")["prefix"])
        self.state.run("delete from state where key in (?, ?)", (f"compact:{lane}", f"usage:{lane}"))
        self.send(messages)  # built again from the same store state
        self.assertEqual(json.dumps(self.state.get(f"compact:{lane}")["prefix"]), first)

    def test_without_related_exchanges_the_outline_is_unchanged(self):
        messages = history(8, last="zzqq xxww")
        self.assertNotIn("related to your new message", summary(self.send(messages)))
        plain = compact.build_prefix(messages, len(messages) - 1, {0: 41}, 6000)
        self.assertEqual(compact.build_prefix(messages, len(messages) - 1, {0: 41}, 6000, related=[]), plain)

    def test_covered_exchanges_can_be_read_back_from_memory(self):
        sent = self.send(history(8))
        first = int(summary(sent).split("] Question 1:", 1)[0].rsplit("[", 1)[1])  # the done section lists the newest first
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
                            human("Thanks, one more thing", marker=True)]
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

    def test_a_refused_compaction_is_tried_again_after_the_conversation_grows(self):
        from getalmanac import compact
        lane = "x"
        ctx = type("C", (), {"state": self.state, "lane": lane})()
        self.state.put(f"nocompact:{lane}", {"detail": "bad block", "at": 10})
        self.assertTrue(compact.blocked(ctx, [{}] * (10 + compact.RETRY_AFTER - 1)))
        self.assertFalse(compact.blocked(ctx, [{}] * (10 + compact.RETRY_AFTER)))
        self.state.put(f"nocompact:{lane}", "bad block")  # a mark from before the count was kept lapses at once
        self.assertFalse(compact.blocked(ctx, [{}] * 11))

    def test_a_size_refusal_does_not_switch_compaction_off_but_another_one_does(self):
        refusal = [b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 250000 tokens > 200000 maximum"}}']

        def respond(req):
            if b"Almanac compacted this conversation" in req["body"]:
                return 400, {"content-type": "application/json"}, refusal[0]
            return 200, {"content-type": "text/event-stream"}, sse()

        self.rig.upstream.respond = respond
        self.send(history(8))
        lane = self.rig.records()[-1]["lane"]
        self.assertIsNone(self.state.get(f"nocompact:{lane}"))
        self.assertIn("prompt is too long", self.rig.records()[-1]["fallback"])
        refusal[0] = b'{"type":"error","error":{"type":"invalid_request_error","message":"bad block"}}'
        other = history(8)
        other[0]["content"][1]["text"] = "A different first question"  # a new conversation: the first one is no longer rewritten
        self.send(other)
        lane = self.rig.records()[-1]["lane"]
        self.assertIn("bad block", self.state.get(f"nocompact:{lane}")["detail"])

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
        self.assertIn("--- Exchange", summary(sent))

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


MODEL = "claude-haiku-5-5"


def note(lines):
    message = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": json.dumps({"lines": lines})}],
               "usage": {"input_tokens": 50, "output_tokens": 20}}
    return 200, {"content-type": "application/json"}, json.dumps(message).encode()


def summarize_used(store):
    return compact.summarize.today(store)["calls"]


class GenerationTest(LayerCase):
    """The outline is built in generations: a compaction adds one to the end, and only a roll-up rewrites earlier bytes."""

    def setUp(self):
        super().setUp()
        self.rig.cfg["compaction"].update(outline_share=0.9, trigger_tokens=1000)  # short stretches, and the outline has the room
        self.rig.cfg["memory"]["summary_model"] = MODEL
        self.calls, self.verdict = [], lambda sent: note(["[1] codewords are required from now on (exchange 3)"])
        self.rig.upstream.respond = self.respond

    def respond(self, req):
        sent = json.loads(req["body"])
        if sent.get("model") == MODEL:
            self.calls.append(sent)
            return self.verdict(sent)
        return 200, {"content-type": "text/event-stream"}, sse()

    @staticmethod
    def convo(n):
        messages = history(n)
        messages[9]["content"][1]["text"] = "Question 3: from now on answer every question with a codeword"  # a standing decision
        return messages

    def compact_state(self):
        return self.state.get(f"compact:{self.rig.records()[-1]['lane']}")

    def test_a_later_compaction_adds_a_generation_and_keeps_every_earlier_byte(self):
        first = self.send(self.convo(8))
        one = self.compact_state()
        second = self.send(self.convo(13))
        two = self.compact_state()
        kept = one["nopen"] + len(one["gens"])
        self.assertEqual(json.dumps(two["prefix"][0]["content"][:kept]), json.dumps(one["prefix"][0]["content"][:kept]))
        self.assertEqual(two["prefix"][1], one["prefix"][1])  # the session-start system message is as it was
        self.assertEqual([g["kind"] for g in two["gens"]], ["append", "append"])
        self.assertEqual(two["gens"][1]["lo"], one["recent_from"])  # the new one starts where the verbatim exchanges began
        self.assertEqual(two["gens"][1]["hi"], two["recent_from"])
        text = two["prefix"][0]["content"][kept]["text"]
        self.assertIn(f"] Question {one['recent_from'] + 1}:", text)  # an exchange that was verbatim is now in the outline
        self.assertIn("--- Exchange 13", two["prefix"][0]["content"][-1]["text"])  # and the recent exchanges are new
        # what went out: identical up to the end of the first generation, apart from the cache breakpoint that moves with the newest
        self.assertEqual(json.dumps(compact._strip(second[0]["content"][:kept])), json.dumps(compact._strip(first[0]["content"][:kept])))
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["kind"], record["gens"], record["kept_chars"]), ("append", 2, len(json.dumps(one["prefix"][0]["content"][:kept]))))
        self.assertLess(record["kept_chars"], record["prefix_chars"])

    def test_a_cache_breakpoint_sits_on_the_newest_generation_only_where_it_fits(self):
        sent = self.send(self.convo(8))
        state = self.compact_state()
        at = state["nopen"] + len(state["gens"]) - 1
        self.assertEqual(sent[0]["content"][at]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", json.dumps(state["prefix"]))  # the stored prefix never holds it
        self.assertEqual(json.dumps(compact._strip(sent[0]["content"])), json.dumps(state["prefix"][0]["content"]))
        crowded = body(self.convo(8))
        crowded["system"] = [{"type": "text", "text": f"rule {n}", "cache_control": {"type": "ephemeral"}} for n in range(3)]
        self.rig.messages(crowded)  # three breakpoints in the system prompt and one on the last message: no room for a fifth
        self.assertNotIn("cache_control", json.dumps(self.rig.sent()["messages"][0]["content"][at]))
        other = self.convo(8)
        other[0]["content"][1]["text"] = "A different first question"
        other[-1]["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        self.assertEqual(self.send(other)[0]["content"][at]["cache_control"], {"type": "ephemeral", "ttl": "1h"})  # never after a longer one

    def test_a_joined_note_heads_the_generation_and_is_asked_for_once(self):
        self.send(self.convo(8))
        self.assertEqual(len(self.calls), 1)
        asked = self.calls[0]
        self.assertEqual(asked["system"][-1]["text"], compact.summarize.JOIN_INSTRUCTION)
        self.assertEqual(asked["system"][0], {"type": "text", "text": "You are Claude Code"})
        self.assertIn("(decision) Question 3", asked["messages"][0]["content"])
        state = self.compact_state()
        gen = state["prefix"][0]["content"][state["nopen"]]["text"]
        self.assertIn(compact.JOINED + "\n- [1] codewords are required from now on (exchange 3)", gen)
        self.assertEqual(self.rig.records()[-1]["compaction"]["synth"], "ok")
        again = self.send(self.convo(8) + [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, human("one more thing", marker=True)])
        self.assertEqual(len(self.calls), 1)  # a later request replays the stored bytes and asks nothing
        self.assertEqual(again[0]["content"][state["nopen"]]["text"], gen)
        self.assertEqual(summarize_used(self.state), 1)  # and the one call counted against the daily cap

    def test_when_the_note_fails_the_generation_is_the_rule_written_outline(self):
        self.verdict = lambda sent: (500, {"content-type": "application/json"}, b"{}")
        sent = self.send(self.convo(8))
        self.assertEqual(sent[-1], self.convo(8)[-1])  # the request went out compacted all the same
        state = self.compact_state()
        gen = state["prefix"][0]["content"][state["nopen"]]["text"]
        self.assertNotIn(compact.JOINED, gen)
        self.assertIn("Standing decisions, oldest first:", gen)
        self.assertEqual(self.rig.records()[-1]["compaction"]["synth"], "status 500")
        self.assertEqual(state["gens"][0]["lines"], [])
        calls = len(self.calls)
        self.send(self.convo(8))
        self.assertEqual(len(self.calls), calls)  # a sent generation is not edited, so it is not asked again

    def test_a_reply_that_is_not_json_or_a_paused_account_is_the_same_fallback(self):
        self.verdict = lambda sent: (200, {"content-type": "application/json"}, json.dumps(
            {"type": "message", "content": [{"type": "text", "text": "no json here"}], "usage": {}}).encode())
        self.send(self.convo(8))
        self.assertEqual(self.rig.records()[-1]["compaction"]["synth"], "parse")
        self.state.put("summary:pause", 10 ** 12)
        self.calls.clear()
        self.send(self.convo(13))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rig.records()[-1]["compaction"]["synth"], "paused")
        self.assertEqual(len(self.compact_state()["gens"]), 2)

    def test_with_no_summary_model_no_call_is_made(self):
        self.rig.cfg["memory"]["summary_model"] = ""
        self.send(self.convo(8))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rig.records()[-1]["compaction"]["synth"], "off")

    def test_the_generations_roll_up_at_the_budget_and_not_before(self):
        self.rig.cfg["memory"]["summary_model"] = ""
        outline = 1500 * 3 * 0.9
        kinds = []
        for n in range(8, 70, 5):
            self.send(self.convo(n))
            state, record = self.compact_state(), self.rig.records()[-1]["compaction"]
            kinds.append(record["kind"])
            texts = [b["text"] for b in state["prefix"][0]["content"][state["nopen"]:state["nopen"] + len(state["gens"])]]
            if record["kind"] == "rollup":
                self.assertEqual(state["gens"][0]["kind"], "rollup")
                self.assertEqual(record["kept_chars"], len(json.dumps(state["prefix"][0]["content"][:state["nopen"]])))  # earlier bytes rewritten
                self.assertIn("Earlier work, rolled up", texts[0])
                self.assertIn("Question 3: from now on", texts[0])  # a standing decision survives the roll-up
                self.assertIn("Almanac compacted this conversation", texts[0])  # the one that is now first carries the header
                self.assertLess(sum(map(len, texts)), outline * 0.75 + len(compact.HEADER))  # well under the budget after it
            else:
                self.assertLessEqual(sum(map(len, texts)), outline + len(compact.HEADER))  # an append stays inside it
            self.assertEqual([g["lo"] for g in state["gens"]][1:], [g["hi"] for g in state["gens"]][:-1])  # the generations tile the exchanges
            self.assertEqual((state["gens"][0]["lo"], state["gens"][-1]["hi"]), (0, state["recent_from"]))
        self.assertEqual(kinds[0], "append")
        self.assertIn("rollup", kinds)
        self.assertLess(kinds.count("rollup"), len(kinds) - 1)  # rare: most compactions are appends

    def test_a_rollup_asks_the_small_model_for_a_summary_of_summaries(self):
        self.verdict = lambda sent: note(["[1] rolled up: codewords stay required"]) if sent["system"][-1]["text"] == compact.summarize.ROLLUP_INSTRUCTION \
            else note(["[2] a note for the stretch"])
        for n in range(8, 70, 5):
            self.send(self.convo(n))
            if self.rig.records()[-1]["compaction"]["kind"] == "rollup":
                break
        record, state = self.rig.records()[-1]["compaction"], self.compact_state()
        self.assertEqual((record["kind"], record["rollup_synth"]), ("rollup", "ok"))
        asked = [c for c in self.calls if c["system"][-1]["text"] == compact.summarize.ROLLUP_INSTRUCTION]
        self.assertEqual(len(asked), 1)
        self.assertIn("(decision) Question 3", asked[0]["messages"][0]["content"])
        first = state["prefix"][0]["content"][state["nopen"]]["text"]
        self.assertIn(compact.JOINED + "\n- [1] rolled up: codewords stay required", first)
        self.assertEqual(state["gens"][0]["lines"], ["[1] rolled up: codewords stay required"])

    def test_a_rollup_with_the_model_down_is_the_rule_outline(self):
        self.verdict = lambda sent: (529, {"content-type": "application/json"}, b"{}")
        for n in range(8, 70, 5):
            self.send(self.convo(n))
            if self.rig.records()[-1]["compaction"]["kind"] == "rollup":
                break
        state = self.compact_state()
        self.assertEqual(state["gens"][0]["kind"], "rollup")
        self.assertIn("Standing decisions, oldest first:", state["prefix"][0]["content"][state["nopen"]]["text"])
        self.assertEqual(state["gens"][0]["lines"], [])

    def test_a_rewind_drops_every_generation(self):
        self.send(self.convo(8))
        self.send(self.convo(13))
        self.assertEqual(len(self.compact_state()["gens"]), 2)
        edited = self.convo(13)
        edited[5]["content"][0]["text"] = "A rewritten earlier answer."
        self.rig.cfg["compaction"]["trigger_tokens"] = 10 ** 9
        self.assertEqual(self.send(edited), edited)
        self.assertIsNone(self.compact_state())
        self.rig.cfg["compaction"]["trigger_tokens"] = 1000
        self.state.run("delete from state where key like 'usage:%'")  # the last reply's small count described the uncompacted body
        self.send(edited)  # a first generation again, over the new history
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["kind"], record["gens"], record["kept_chars"]), ("append", 1, 0))

    def test_the_generations_survive_a_restart(self):
        self.send(self.convo(8))
        one = self.compact_state()
        lane = self.rig.records()[-1]["lane"]
        path = self.store.path
        self.rig.close()
        self.store.close()
        self.store = Store(path)
        self.state = State(self.store.path.with_name("state.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, self.state)
        self.rig = Rig(respond=self.respond, store=self.store, state=self.state, layers=[ProjectLayer(self.memory), CompactLayer(self.memory), MemoryLayer(self.memory)],
                       overrides={"compaction.trigger_tokens": 1000, "compaction.target_tokens": 1500, "compaction.outline_share": 0.9,
                                  "memory.recall": False, "memory.summary_model": MODEL})
        self.addCleanup(self.rig.close)
        self.assertEqual(self.state.get(f"compact:{lane}")["gens"], one["gens"])
        replay = self.send(self.convo(8))  # the same conversation: the stored prefix is replayed as it was
        self.assertEqual(json.dumps(compact._strip(replay[:3])), json.dumps(one["prefix"]))
        self.send(self.convo(13))
        two = self.state.get(f"compact:{lane}")
        kept = one["nopen"] + len(one["gens"])
        self.assertEqual(json.dumps(two["prefix"][0]["content"][:kept]), json.dumps(one["prefix"][0]["content"][:kept]))
        self.assertEqual(len(two["gens"]), 2)

    def test_a_state_stored_before_generations_is_rebuilt_as_the_first_one(self):
        self.send(self.convo(8))
        state, lane = self.compact_state(), self.rig.records()[-1]["lane"]
        for key in ("gens", "recent_from", "nopen", "log"):
            state.pop(key)
        self.state.put(f"compact:{lane}", state)
        self.send(self.convo(13))
        after = self.compact_state()
        self.assertEqual([g["kind"] for g in after["gens"]], ["append"])
        self.assertEqual((after["gens"][0]["lo"], after["gens"][0]["hi"]), (0, after["recent_from"]))

    def test_the_report_counts_appends_and_rollups(self):
        from getalmanac import report
        records = [{"kind": "messages", "compaction": {"fresh": True, "kind": "append", "kept_chars": 800, "prefix_chars": 1000, "synth": "ok"}},
                   {"kind": "messages", "compaction": {"fresh": True, "kind": "append", "kept_chars": 900, "prefix_chars": 1200, "synth": "status 500"}},
                   {"kind": "messages", "compaction": {"fresh": True, "kind": "rollup", "kept_chars": 10, "prefix_chars": 1000, "rollup_synth": "ok"}},
                   {"kind": "messages", "compaction": {"fresh": False, "kind": "append", "kept_chars": 1, "prefix_chars": 1}}]
        text = report.render_compaction(records)
        self.assertIn("2 appends, 1 roll-up", text)
        self.assertIn("53%", text)  # (800 + 900 + 10) / 3200 of the prefix bytes kept
        self.assertIn("2 joined by the small model, 1 fell back", text)
        self.assertEqual(report.render_compaction([]), "")


def loop(n, start=0, size=3000, special=None):
    """A tool loop: `n` tool calls and their results, ids u<start>... `special` maps an index to overrides for that result block."""
    out = []
    for i in range(start, start + n):
        block = {"type": "tool_result", "tool_use_id": f"u{i}", "content": f"r{i} " + "x" * size}
        block.update((special or {}).get(i, {}))
        out += [{"role": "assistant", "content": [{"type": "text", "text": f"step {i}"},
                                                  {"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": f"run {i}"}}]},
                {"role": "user", "content": [block]}]
    return out


def long_turn(n, **kw):
    return history(2, last="Do the long job please") + loop(n, **kw)


def results(messages):
    """The tool results of the tool loop (the ones whose id starts with u)."""
    return [b for m in messages if m.get("role") == "user" for b in compact.rq.blocks(m)
            if b.get("type") == "tool_result" and str(b.get("tool_use_id")).startswith("u")]


def stubbed(messages):
    return [b["tool_use_id"] for b in results(messages) if str(b.get("content")).startswith("[Almanac removed")]


class LongTurnTest(LayerCase):
    window = 30000  # tokens; a pass starts at 85% of it

    def setUp(self):
        super().setUp()
        self.rig.cfg["routing"]["windows"]["m"] = self.window
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"},
                                                 sse(usage={"input_tokens": len(req["body"]) // 3}))

    def go(self, messages):
        """Send, wait for the reply to be recorded, and return the messages the upstream received."""
        self.send(messages)
        self.rig.records()
        return self.rig.sent()["messages"]

    def lane(self):
        return self.rig.records()[-1]["lane"]

    def test_below_the_share_nothing_changes(self):
        raw = json.dumps(body(long_turn(10))).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_old_results_are_stubbed_past_the_share_and_the_newest_are_kept(self):
        messages = long_turn(30, special={3: {"is_error": True}, 4: {"content": "short"}})
        sent = self.go(messages)
        self.assertEqual(stubbed(sent), [f"u{i}" for i in range(18) if i not in (3, 4)])  # not the newest 12, the error or the small one
        self.assertEqual(len(sent), len(messages))
        self.assertEqual(json.dumps(results(sent)[18:]), json.dumps(results(messages)[18:]))  # the newest are as sent
        self.assertEqual(results(sent)[0]["content"], compact.STUB_RAW.format(n=3003, id="u0"))
        self.assertIn('memory_read tool: {"result": "u0"}', results(sent)[0]["content"])
        self.assertEqual(results(sent)[3], results(messages)[3])  # the error stays, with its flag
        self.assertEqual(results(sent)[4], results(messages)[4])
        for before, after in zip(messages, sent):  # roles, ids and tool calls are exactly as sent
            self.assertEqual(before["role"], after["role"])
            if before["role"] == "assistant":
                self.assertEqual(before, after)
            else:
                self.assertEqual([b.get("tool_use_id") for b in before["content"]], [b.get("tool_use_id") for b in after["content"]])
        self.assertEqual(sent[:5], messages[:5])  # nothing before the last human turn is touched
        self.assertEqual(self.rig.records()[-1]["compaction"]["stubbed"], 16)
        # the count the reply gave describes the rewritten body
        self.assertLess(self.state.get(f"usage:{self.lane()}")["tokens"], len(json.dumps(messages)) / 3 * 0.7)

    def test_consecutive_requests_stub_the_same_results_byte_for_byte(self):
        first = self.go(long_turn(30))
        second = self.go(long_turn(33))  # three results arrive: still under the share, so nothing new is stubbed
        self.assertEqual(stubbed(second), stubbed(first))
        self.assertEqual(json.dumps(second[:len(first)]), json.dumps(first))

    def test_a_second_pass_stubs_more_and_leaves_the_first_stubs_as_they_were(self):
        first = self.go(long_turn(30))
        second = self.go(long_turn(44))
        self.assertEqual(len(stubbed(first)), 18)
        self.assertEqual(len(stubbed(second)), 32)
        self.assertEqual(stubbed(second)[:18], stubbed(first))
        self.assertEqual(json.dumps(second[:2 * 18]), json.dumps(first[:2 * 18]))

    def test_the_kept_count_steps_down_to_four_when_one_pass_is_not_enough(self):
        self.rig.cfg["routing"]["windows"]["m"] = 8000
        sent = self.go(long_turn(20))
        self.assertEqual(len(results(sent)) - len(stubbed(sent)), 4)
        self.assertEqual(self.state.get(f"stubs:{self.lane()}")["keep"], 4)

    def test_it_is_off_at_zero_and_with_compaction_off(self):
        messages = long_turn(30)
        self.rig.cfg["compaction"]["window_share"] = 0
        self.assertEqual(self.go(messages), messages)
        self.rig.cfg["compaction"]["window_share"] = 0.85
        self.rig.cfg["compaction"]["enabled"] = False
        self.assertEqual(self.go(messages), messages)

    def test_a_malformed_block_does_not_stop_the_request_or_the_other_stubs(self):
        messages = long_turn(30, special={2: {"content": None}, 3: {"content": 7}, 4: {"content": {"odd": "x" * 3000}}})
        messages[11]["content"][0].pop("tool_use_id")  # a result that names no call
        messages[13]["content"][0]["tool_use_id"] = ["u6"]
        messages[15]["content"].append("not a block")
        self.assertEqual(self.rig.messages(body(copy.deepcopy(messages)))[0], 200)
        sent = self.rig.sent()["messages"]
        self.assertNotIn("error", self.rig.records()[-1])
        self.assertIn("u5", stubbed(sent))
        self.assertIn("u4", stubbed(sent))  # a result that is not text counts by its size
        self.assertEqual(sent[11], messages[11])
        self.assertEqual(sent[13], messages[13])

    def test_a_failure_while_deciding_sends_the_request_unchanged(self):
        from unittest import mock

        messages = long_turn(30)
        with mock.patch.object(compact, "estimate", side_effect=RuntimeError("boom")):
            self.assertEqual(self.go(messages), messages)

    def test_it_composes_with_the_stored_prefix(self):
        messages = history(8)
        first = self.go(messages)  # compacted at this human turn
        sent = self.go(messages + loop(30))
        self.assertEqual(json.dumps(sent[:3]), json.dumps(first[:3]))  # the prefix is replayed as stored
        self.assertEqual(len(stubbed(sent)), 18)
        self.assertEqual(len(sent), 3 + 1 + 60)
        record = self.rig.records()[-1]["compaction"]
        self.assertEqual((record["fresh"], record["stubbed"]), (False, 18))

    def test_the_next_human_turn_rebuilds_the_prefix_and_clears_the_stubs(self):
        messages = long_turn(30)
        self.go(messages)
        key = f"stubs:{self.lane()}"
        self.assertTrue(self.state.get(key))
        sent = self.go(messages + [{"role": "assistant", "content": [{"type": "text", "text": "Done."}]}, human("Now the next thing")])
        self.assertTrue(self.rig.records()[-1]["compaction"]["fresh"])
        self.assertIsNone(self.state.get(key))
        self.assertNotIn("Almanac removed this tool result", json.dumps(sent))

    def test_without_a_rebuild_the_next_human_turn_keeps_the_same_stubs(self):
        self.rig.cfg["compaction"]["trigger_tokens"] = 10 ** 9
        messages = long_turn(30)
        first = self.go(messages)
        sent = self.go(messages + [{"role": "assistant", "content": [{"type": "text", "text": "Done."}]}, human("Now the next thing")])
        self.assertEqual(json.dumps(sent[:len(first)]), json.dumps(first))

    def test_an_edit_of_covered_history_drops_the_stubs(self):
        messages = history(8)
        self.go(messages)
        self.go(messages + loop(30))
        key = f"stubs:{self.lane()}"
        self.assertTrue(self.state.get(key))
        edited = copy.deepcopy(messages)
        edited[5]["content"][0]["text"] = "A rewritten earlier answer."
        self.go(edited)
        self.assertIsNone(self.state.get(key))

    def test_a_lagging_usage_count_does_not_cause_a_second_pass(self):
        first = self.go(long_turn(30))
        self.state.put(f"usage:{self.lane()}", {"tokens": 10 ** 6, "messages": 7})  # an earlier request's count, written late
        second = self.go(long_turn(32))
        self.assertEqual(stubbed(second), stubbed(first))

    def test_a_refused_body_goes_out_as_it_was(self):
        def respond(req):
            if b"Almanac removed this tool result" in req["body"]:
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"type":"invalid_request_error","message":"bad block"}}'
            return 200, {"content-type": "text/event-stream"}, sse()

        self.rig.upstream.respond = respond
        messages = long_turn(30)
        self.assertEqual(self.go(messages), messages)
        self.assertEqual(len(self.rig.upstream.requests), 2)
        self.assertIn("bad block", self.state.get(f"nocompact:{self.lane()}")["detail"])
        self.assertIn("bad block", self.rig.records()[-1]["fallback"])
        longer = long_turn(31)
        self.assertEqual(self.go(longer), longer)  # and it is not tried again for this conversation
        self.assertEqual(len(self.rig.upstream.requests), 3)

    def test_four_hundred_tool_results_in_one_turn_stay_inside_the_window(self):
        self.rig.cfg["routing"]["windows"]["m"] = 60000
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"},  # a tool loop: each reply asks for another call
                                                 sse(usage={"input_tokens": len(req["body"]) // 3}, tool=("Bash", {"command": "next"})))
        sizes, passes, last, messages = [], 0, [], history(2, last="Do the long job please")
        for i in range(400):
            messages += loop(1, start=i, size=2100)
            self.rig.messages(body(copy.deepcopy(messages)))  # no wait for the reply's count: it may arrive late
            sent = json.loads(self.rig.upstream.requests[-1]["body"])["messages"]
            sizes.append(len(self.rig.upstream.requests[-1]["body"]) / compact.CHARS_PER_TOKEN)
            now = stubbed(sent)
            passes += now != last
            self.assertEqual(now[:len(last)], last)  # the stubs only grow
            last = now
        self.assertLess(max(sizes), 0.85 * 60000 + 1000)
        self.assertGreaterEqual(len(results(sent)) - len(last), 4)
        self.assertLess(passes, 40)  # a pass frees a good share of the room, so the cache is not broken at every request

    def test_a_size_refusal_teaches_the_real_window_and_the_turn_goes_on(self):
        self.go(long_turn(30))  # stubs are stored, so the next request is a rewritten one
        self.rig.cfg["routing"]["windows"]["m"] = 1000000  # the table now overstates this conversation's window
        real, seen = 15000, []

        def respond(req):
            tokens = len(req["body"]) // 3
            seen.append(tokens)
            if tokens > real:
                refusal = {"type": "error", "error": {"type": "invalid_request_error",
                                                      "message": f"prompt is too long: {tokens} tokens > {real} maximum"}}
                return 400, {"content-type": "application/json"}, json.dumps(refusal).encode()
            return 200, {"content-type": "text/event-stream"}, sse(usage={"input_tokens": tokens})

        self.rig.upstream.respond = respond
        sent = self.go(long_turn(34))
        self.assertEqual([tokens > real for tokens in seen], [True, False])  # refused once, then inside the window
        self.assertEqual(self.state.get(f"window:{self.lane()}"), real)
        self.assertGreater(len(stubbed(sent)), 18)
        self.go(long_turn(36))  # later requests are relieved before the upstream has to refuse
        self.assertEqual(len(seen), 3)
        self.assertLessEqual(seen[-1], real)

    def test_a_long_sub_agent_gets_the_same_relief(self):
        # Claude Code's own compaction is off for sub-agents as well; this checks the stubs alone (no outline).
        self.rig.cfg["compaction"]["agent_outline"] = False
        messages = long_turn(30)
        extra = {"x-claude-code-agent-id": "a1"}
        self.rig.messages(body(copy.deepcopy(messages)), extra=extra)
        first = json.loads(self.rig.upstream.requests[-1]["body"])["messages"]
        self.assertEqual(stubbed(first), [f"u{i}" for i in range(18)])
        self.assertEqual(len(first), len(messages))
        self.rig.messages(body(copy.deepcopy(long_turn(32))), extra=extra)
        second = json.loads(self.rig.upstream.requests[-1]["body"])["messages"]
        self.assertEqual(json.dumps(second[:len(first)]), json.dumps(first))  # the same bytes on the next request


    def test_a_sub_agent_in_a_huge_window_is_stubbed_at_stub_tokens(self):
        self.rig.cfg["routing"]["windows"]["m"] = 1000000  # 85% of this would never be reached
        self.rig.cfg["compaction"].update(stub_tokens=25000, agent_outline=False)
        extra = {"x-claude-code-agent-id": "a1"}
        self.rig.messages(body(copy.deepcopy(long_turn(30))), extra=extra)
        self.assertTrue(stubbed(json.loads(self.rig.upstream.requests[-1]["body"])["messages"]))
        self.rig.cfg["compaction"]["stub_tokens"] = 0
        self.rig.messages(body(copy.deepcopy(long_turn(30))), extra={"x-claude-code-agent-id": "a2"})
        self.assertEqual(stubbed(json.loads(self.rig.upstream.requests[-1]["body"])["messages"]), [])


    def test_a_long_sub_agent_gets_an_outline_and_keeps_it(self):
        self.rig.cfg["routing"]["windows"]["m"] = 1000000
        self.rig.cfg["compaction"]["stub_tokens"] = 6000
        extra = {"x-claude-code-agent-id": "a1"}
        self.rig.messages(body(copy.deepcopy(long_turn(40))), extra=extra)
        first = json.loads(self.rig.upstream.requests[-1]["body"])["messages"]
        full = long_turn(40)
        self.assertLess(len(first), len(full))
        self.assertEqual(first[0]["role"], "user")
        self.assertEqual(first[1]["role"], "assistant")
        outline = first[0]["content"][-1]["text"]
        self.assertTrue(outline.startswith(compact.AGENT_HEADER))
        self.assertIn("- ran ", outline)
        self.assertEqual([m["role"] for m in first[1:]], [m["role"] for m in full[len(full) - len(first) + 1:]])  # the newest steps, in order
        self.rig.messages(body(copy.deepcopy(long_turn(41))), extra=extra)
        second = json.loads(self.rig.upstream.requests[-1]["body"])["messages"]
        self.assertEqual(json.dumps(second[0]), json.dumps(first[0]))  # the outline is sent byte for byte

    def test_the_outline_is_off_with_agent_outline_false(self):
        self.rig.cfg["routing"]["windows"]["m"] = 1000000
        self.rig.cfg["compaction"].update(stub_tokens=25000, agent_outline=False)
        self.rig.messages(body(copy.deepcopy(long_turn(40))), extra={"x-claude-code-agent-id": "a1"})
        sent = json.loads(self.rig.upstream.requests[-1]["body"])["messages"]
        self.assertEqual(len(sent), len(long_turn(40)))


class ColdTest(LayerCase):
    """A request back after its cache expired is compacted from twice target_tokens (1500 here), not from the trigger."""
    trigger = 10 ** 6

    def setUp(self):
        super().setUp()
        self.rig.cfg["routing"]["windows"]["m"] = 30000
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"},
                                                 sse(usage={"input_tokens": len(req["body"]) // 3}))

    def cold(self, messages, extra=None, gap=4000):
        """Send once, let `gap` seconds pass for that lane, then send `messages`; return what the upstream got last."""
        self.rig.messages(body(copy.deepcopy(messages)), extra=extra)
        lane = self.rig.records()[-1]["lane"]
        self.rig.proxy.last_seen[lane] -= gap
        self.rig.messages(body(copy.deepcopy(messages)), extra=extra)
        return self.rig.sent()["messages"], self.rig.records()[-1]

    def test_a_cold_main_thread_is_compacted_under_the_trigger(self):
        messages = history(14)
        sent, record = self.cold(messages, gap=4000)
        self.assertIn("Almanac compacted this conversation", summary(sent))
        self.assertEqual(record["compaction"]["fresh"], True)
        self.assertGreater(record["cold"]["before"], record["cold"]["after"])
        self.assertEqual(record["cold"]["before"], compact.estimate(body(messages), None, messages, None))

    def test_a_second_cold_return_counts_against_the_stored_prefix_not_the_raw_history(self):
        messages = history(14)
        self.cold(messages)
        state = self.state.get(f"compact:{self.rig.records()[-1]['lane']}")
        grown = messages[:-1] + sum((turn(n) for n in range(20, 30)), []) + [messages[-1]]
        _, record = self.cold(grown)
        current = state["prefix"] + grown[state["covered"]:]
        self.assertEqual(record["cold"]["before"], compact.estimate(body(grown), None, current, None))
        self.assertLess(record["cold"]["before"], compact.estimate(body(grown), None, grown, None))

    def test_a_refused_cold_compaction_is_not_counted(self):
        self.rig.upstream.respond = lambda req: (
            (400, {"content-type": "application/json"}, b'{"type":"error","error":{"type":"invalid_request_error"}}')
            if b"Almanac compacted this conversation" in req["body"] else (200, {"content-type": "text/event-stream"}, sse()))
        messages = history(14)
        sent, record = self.cold(messages)
        self.assertEqual(sent, messages)  # the original went out
        self.assertNotIn("cold", record)

    def test_the_next_warm_request_keeps_the_same_prefix_and_is_not_counted_again(self):
        messages = history(14)
        sent, _ = self.cold(messages)
        self.rig.messages(body(copy.deepcopy(messages)))
        self.assertEqual(json.dumps(self.rig.sent()["messages"]), json.dumps(sent))
        self.assertNotIn("cold", self.rig.records()[-1])

    def test_a_warm_request_of_the_same_size_is_untouched(self):
        messages = history(14)
        sent, record = self.cold(messages, gap=100)  # inside the 5 minute life
        self.assertEqual(sent, messages)
        self.assertNotIn("cold", record)

    def test_a_longer_ttl_makes_a_short_pause_warm(self):
        messages = history(14)
        for msg in messages:
            for block in msg["content"]:
                if "cache_control" in block:
                    block["cache_control"]["ttl"] = "1h"
        sent, record = self.cold(messages, gap=2000)  # past 5m, inside 1h
        self.assertEqual(sent, messages)

    def test_a_small_conversation_is_not_worth_compacting(self):
        sent, record = self.cold(history(2))
        self.assertEqual(sent, history(2))
        self.assertNotIn("cold", record)

    def test_it_can_be_switched_off(self):
        self.rig.cfg["compaction"]["on_cold"] = False
        messages = history(14)
        sent, record = self.cold(messages)
        self.assertEqual(sent, messages)
        self.assertNotIn("cold", record)

    def test_compaction_off_wins(self):
        self.rig.cfg["compaction"]["enabled"] = False
        messages = history(14)
        sent, record = self.cold(messages)
        self.assertEqual(sent, messages)
        self.assertNotIn("cold", record)

    def test_a_cold_long_turn_is_stubbed_below_the_window_share(self):
        sent, record = self.cold(long_turn(10))
        self.assertTrue(stubbed(sent))
        self.assertGreater(record["cold"]["before"], record["cold"]["after"])

    def test_a_cold_sub_agent_is_stubbed_too(self):
        extra = {"x-claude-code-agent-id": "a1"}
        sent, record = self.cold(long_turn(10), extra=extra)
        self.assertTrue(stubbed(sent))
        self.assertGreater(record["cold"]["before"], record["cold"]["after"])
        warm, _ = self.cold(long_turn(10), extra={"x-claude-code-agent-id": "a2"}, gap=10)
        self.assertEqual(stubbed(warm), [])


if __name__ == "__main__":
    unittest.main()
