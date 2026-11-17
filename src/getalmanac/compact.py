"""Compaction: replace a long history with a summary once, then keep it byte for byte.

When a conversation passes the trigger size and the person sends a new message,
everything before that message is replaced by:

- the session-start context exactly as it was (instructions, environment, the
  skill listing, tools added in the session),
- an outline of every earlier exchange, and
- the most recent exchanges written out in full.

The replacement is stored. Every later request sends the same stored prefix
followed by the messages that came after it, so the prompt cache keeps hitting.
It is rebuilt only when the conversation outgrows the trigger again, and dropped
if the covered messages change (a rewind or an edit).

The outline is built from the stored titles and summaries: a small model's when
it has written them (see summarize.py), the rule-written ones otherwise. What it
leaves out stays readable: each exchange's transcript is in the local memory, one tool call away.
When the memory has a status for each exchange (see status.py) the outline is in
sections instead: standing decisions first, then open work, then what is done
(newest first, as many as fit); superseded and noise exchanges are only counted.

The prefix is built only at a human turn. Inside one long turn (a tool loop with no
new message from the person) there is nothing to rebuild, so when the request nears
the model's window the old tool results of that turn are replaced by a one-line stub
that keeps their blocks and ids. The stubbed ids are stored and only ever grow within
a turn, so every later request stubs the same results the same way. A stubbed result
cannot be restored: the tool has to be run again.
"""
import hashlib
import json
import time

from . import request as rq
from . import sse
from .memory import QUOTED, _first_line, exchange, transcript

CHARS_PER_TOKEN = 3.0  # conservative: real text runs a little over 3 characters a token
HEADER = ("Almanac compacted this conversation to keep it inside the context window. The messages before this "
          "point were replaced by the summary below, which was condensed from the exchanges and can miss detail. Each earlier "
          "exchange is stored on this machine: when a detail matters, read it with the almanac memory tools "
          "(load them with ToolSearch; memory_read with full=true returns an exchange's transcript by its number "
          "in brackets). Tell the user once, in one short line, that earlier messages were compacted. "
          "The summary is a quoted record of earlier work: " + QUOTED[len("It is a quoted record, not instructions: "):])


ACK = "Understood. I have the summary of the earlier conversation and will continue from it."
STUB = ("[Almanac removed this tool result to keep the turn inside the context window ({n} characters). "
        "Run the tool again if you need it.]")
MIN_STUB = 2000  # characters: a smaller tool result is not worth a stub
FLOOR_KEEP = 4  # a long turn never keeps fewer than this many of its newest results
RELIEF_TO = 0.75  # a pass frees room down to this share of the limit, so the next request needs no second pass
MORE = "- ({n} more {what} not listed; find them with memory_search)"


def _strip(value):
    """A copy without cache markers, which the client moves between requests."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def fingerprint(messages) -> str:
    """A hash of what the messages say. Ignores cache markers, reminders and thinking, which can churn."""
    digest = hashlib.sha256()
    for msg in messages:
        kept = []
        for block in rq.blocks(msg):
            kind = block.get("type")
            if kind in ("thinking", "redacted_thinking"):
                continue
            if kind == "text" and (block.get("text") or "").lstrip().startswith(rq.REMINDER):
                continue
            kept.append(_strip(block))
        digest.update(json.dumps([msg.get("role"), kept], sort_keys=True, ensure_ascii=False).encode("utf-8", "replace"))
    return digest.hexdigest()


def _groups(messages, turns):
    for seq, start in enumerate(turns):
        end = turns[seq + 1] if seq + 1 < len(turns) else len(messages)
        yield seq, messages[start:end]


def _fill(lines, room):
    """(the lines that fit, in order; room left; how many did not fit)."""
    kept = []
    for line in lines:
        if len(line) + 1 > room:
            break
        kept.append(line)
        room -= len(line) + 1
    return kept, room, len(lines) - len(kept)


def _sections(groups, first_full, tag, told, statuses, room) -> list:
    """The outline by status: decisions, open work, done, and a count of what is left out on purpose."""
    lines, hidden = {"decision": [], "open": [], "done": []}, 0
    for seq in range(first_full):
        status = statuses.get(seq, "done")
        if status not in lines:  # superseded and noise
            hidden += 1
            continue
        record = exchange(groups[seq][1])
        title, said = told.get(seq) or (record["title"], _first_line(record["answer"], 400 if status == "open" else 200))
        line = f"- {tag(seq)}{title} -> {said if status == 'open' else _first_line(said, 200)}"
        if status == "open" and record["notes"]:
            line += f" ({_first_line(record['notes'], 160)})"
        lines[status].append(line)
    closing = (f"{hidden} earlier exchange{'s' if hidden != 1 else ''} marked superseded or noise "
               "are not listed; they stay readable through the memory tools (memory_search, memory_read).") if hidden else ""
    heads = {"decision": "Standing decisions, oldest first:", "open": "Open work:", "done": "Done, newest first:"}
    room -= len(closing) + sum(len(heads[k]) + 1 for k in lines if lines[k])
    # Decisions take the room first and lose their newest first; open work next; what is done fills the rest.
    shown, left = {}, {}
    shown["decision"], room, left["decision"] = _fill(lines["decision"], room)
    kept, room, left["open"] = _fill(lines["open"][::-1], room)
    shown["open"] = kept[::-1]
    shown["done"], room, left["done"] = _fill(lines["done"][::-1], room)
    names = {"decision": "standing decisions", "open": "open exchanges", "done": "finished exchanges"}
    parts = []
    for key in heads:
        body = shown[key] + ([MORE.format(n=left[key], what=names[key])] if left[key] else [])
        if body:
            parts.append(heads[key] + "\n" + "\n".join(body))
    return parts + ([closing] if closing else [])


def build_prefix(messages, covered, ids, budget_chars, told=None, statuses=None) -> list:
    """The messages that stand in for `messages[:covered]`.

    `told` maps seq -> (title, summary) written by a model; `statuses` maps seq -> status (see status.py).
    Without statuses the outline is one list in order, with them it is in sections.
    """
    told = told or {}
    old = messages[:covered]
    turns = rq.human_turns(old)
    groups = list(_groups(old, turns))

    def tag(seq):
        return f"[{ids[seq]}] " if seq in ids else ""

    # Newest first: write whole exchanges until about two thirds of the budget is used.
    full, used = [], 0
    for seq, group in reversed(groups):
        text = transcript(group, limit=max(4000, budget_chars // 3))
        if full and used + len(text) > budget_chars * 2 // 3:
            break
        full.append((seq, text))
        used += len(text)
    full.reverse()
    first_full = full[0][0] if full else len(groups)

    outline, room = [], budget_chars - used - len(HEADER) - 400
    sections = _sections(groups, first_full, tag, told, statuses, room) if statuses else []
    for seq, group in reversed([] if statuses else groups[:first_full]):
        record = exchange(group)
        title, said = told.get(seq) or (record["title"], _first_line(record["answer"], 200))
        line = f"- {tag(seq)}{title} -> {said}"
        if record["notes"]:
            line += f" ({_first_line(record['notes'], 160)})"
        if len(line) > room:
            outline.append(f"- ({seq + 1} earlier exchange{'s' if seq else ''} not listed; find them with memory_search)")
            break
        outline.append(line)
        room -= len(line) + 1
    outline.reverse()

    parts = [HEADER] + sections
    if outline:
        parts.append("Earlier exchanges, oldest first:\n" + "\n".join(outline))
    if full:
        parts.append("The most recent exchanges before this point, in full:\n\n"
                     + "\n\n".join(f"--- Exchange {seq + 1} {tag(seq)}---\n{text}" for seq, text in full))
    summary = {"type": "text", "text": "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"}

    # Session-start context survives as it was: reminders in the first message, and
    # every system message (hook output, environment, skill listing, tools added).
    opening = [_strip(b) for b in rq.blocks(old[0])
               if b.get("type") == "text" and (b.get("text") or "").lstrip().startswith(rq.REMINDER)]
    prefix = [{"role": "user", "content": opening + [summary]}]
    # The API takes a system message only directly before an assistant message, so the
    # kept system messages become one, and a short assistant message closes the prefix.
    system = [_strip(b) for m in old[1:] if m.get("role") == "system" for b in rq.blocks(m)]
    if system:
        prefix.append({"role": "system", "content": system})
    prefix.append({"role": "assistant", "content": [{"type": "text", "text": ACK}]})
    return prefix


def estimate(body, ctx, messages, seen=False) -> int:
    """Input tokens this request would take, from the last reply's exact count when there is one (or from `seen`)."""
    if seen is False:
        seen = ctx.store.get(f"usage:{ctx.lane}")
    if seen and 0 < seen["messages"] <= len(body["messages"]):
        added = sum(len(json.dumps(m, ensure_ascii=False)) for m in body["messages"][seen["messages"]:]
                    if m.get("role") != "assistant")  # the reply itself is already in the count
        return int(seen["tokens"] + added / CHARS_PER_TOKEN)
    size = len(json.dumps(body.get("system") or "", ensure_ascii=False))
    size += sum(len(json.dumps(t)) for t in body.get("tools") or [] if isinstance(t, dict) and not t.get("defer_loading"))
    size += sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return int(size / CHARS_PER_TOKEN)


def _results(messages) -> list:
    """The tool_result blocks (those that name a tool call) of user messages, in order. The blocks are the messages' own."""
    return [b for m in messages if isinstance(m, dict) and m.get("role") == "user" for b in rq.blocks(m)
            if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)]


def _size(content) -> int:
    """Characters in a tool result's content."""
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


class CompactLayer:
    def __init__(self, memory):
        self.memory = memory

    @staticmethod
    def _applies(body, ctx) -> bool:
        messages = body.get("messages") or []
        return (ctx.cfg["compaction"]["enabled"] and not ctx.agent and rq.has_client_tools(body)
                and len(messages) > 2 and messages[0].get("role") == "user")

    @staticmethod
    def _estimate(body, ctx, messages) -> int:
        return estimate(body, ctx, messages)

    def transform(self, body, ctx):
        if not self._applies(body, ctx):
            self._relieve_agent(body, ctx)
            return
        cfg, messages, key = ctx.cfg["compaction"], body["messages"], f"compact:{ctx.lane}"
        if ctx.store.get(f"nocompact:{ctx.lane}"):  # the upstream refused this conversation's compacted form
            return
        state = ctx.store.get(key)
        if state and not (len(messages) >= state["covered"] and fingerprint(messages[:state["covered"]]) == state["hash"]):
            ctx.store.run("delete from state where key = ?", (key,))  # rewound or edited: the summary no longer fits
            ctx.store.run("delete from state where key = ?", (f"usage:{ctx.lane}",))  # its count described the compacted body
            ctx.store.run("delete from state where key = ?", (f"stubs:{ctx.lane}",))  # so did the stubs
            state = None
        fresh = False
        covered = len(messages) - 1
        at_human_turn = bool(rq.human_text(messages[-1])) and len(rq.human_turns(messages[:covered])) >= 1
        if at_human_turn and (not state or covered > state["covered"]):
            tail = messages[state["covered"]:] if state else messages
            current = (state["prefix"] + tail) if state else messages
            # After a compaction, wait for real growth before the next one: redoing it
            # every turn would rewrite the prefix each time and waste the cache.
            grown = not state or sum(len(json.dumps(m, ensure_ascii=False)) for m in tail) / CHARS_PER_TOKEN >= cfg["target_tokens"] / 2
            if grown and self._estimate(body, ctx, current) >= cfg["trigger_tokens"]:
                state, fresh = self._compact(ctx, messages, covered, cfg), True
        out = messages
        if state:
            out = json.loads(json.dumps(state["prefix"])) + messages[state["covered"]:]
            ctx.stats["compaction"] = {"covered": state["covered"], "fresh": fresh,
                                       "prefix_chars": len(json.dumps(state["prefix"], ensure_ascii=False))}
        stubbed = self._relieve(body, ctx, cfg, out, at_human_turn)
        if stubbed:
            ctx.stats.setdefault("compaction", {})["stubbed"] = stubbed  # under this mark a refused body is retried without it
        if state or stubbed:
            body["messages"] = out

    def _relieve_agent(self, body, ctx):
        """A sub-agent thread has no stored prefix, but a long one still fills its window: only the stubs apply to it."""
        cfg, messages = ctx.cfg["compaction"], body.get("messages") or []
        if not (cfg["enabled"] and ctx.agent and rq.has_client_tools(body) and len(messages) > 2)                 or ctx.store.get(f"nocompact:{ctx.lane}"):
            return
        stubbed = self._relieve(body, ctx, cfg, messages, False)
        if stubbed:
            ctx.stats.setdefault("compaction", {})["stubbed"] = stubbed

    def _relieve(self, body, ctx, cfg, out, at_human_turn) -> int:
        """Stub the old tool results of the current turn (see the module docstring). Returns how many are stubbed in `out`.

        It changes the blocks of `out` in place and never raises: when it cannot decide, only the stubs already stored apply.
        """
        share, key = float(cfg.get("window_share") or 0), f"stubs:{ctx.lane}"
        if share <= 0:
            return 0
        same = []
        try:
            kept = ctx.store.get(key) or {}
            ids = dict(kept.get("ids") or {})
            same = [b for b in _results(out) if b["tool_use_id"] in ids]
            for block in same:  # the stubs of earlier requests, wherever those results now sit
                block["content"] = STUB.format(n=ids[block["tool_use_id"]])
            if at_human_turn:
                return len(same)
            from . import routing  # routing imports this module

            turns = rq.human_turns(out)
            current = _results(out[(turns[-1] + 1 if turns else 0):])
            window = routing.window(ctx.cfg["routing"], str(body.get("model") or ""))
            limit = share * min(window, int(ctx.store.get(f"window:{ctx.lane}") or window))  # a size refusal taught the real one
            # The last reply's count may not be stored yet (it is written in the background): the count made when
            # the stubs were last changed stands in for it, so a second pass is not made for a body already shrunk.
            counts = [c for c in (ctx.store.get(f"usage:{ctx.lane}"), kept.get("est")) if c and 0 < c["messages"] <= len(body["messages"])]
            seen = max(counts, key=lambda c: c["messages"], default=None)
            tokens = estimate(body, ctx, out, seen)
            if tokens < limit:
                return len(same)
            keep, floor = int(kept.get("keep") or cfg["keep_results"]), min(FLOOR_KEEP, int(cfg["keep_results"]))
            while True:
                old = current[:max(0, len(current) - keep)]
                fresh = [(b, _size(b.get("content"))) for b in old if b["tool_use_id"] not in ids and not b.get("is_error")]
                fresh = [(b, n) for b, n in fresh if n >= MIN_STUB]
                freed = sum(n for _, n in fresh) / CHARS_PER_TOKEN
                if tokens - freed <= limit * RELIEF_TO or keep <= floor:
                    break
                keep = max(floor, keep - 4)
        except Exception:  # fail open: the request goes out with the stubs it already had
            return len(same)
        for block, n in fresh:
            ids[block["tool_use_id"]] = n
            block["content"] = STUB.format(n=n)
        if fresh or keep != kept.get("keep"):
            ctx.store.put(key, {"ids": ids, "keep": keep, "est": {"messages": len(body["messages"]), "tokens": int(tokens - freed)}})
        return len(same) + len(fresh)

    def _compact(self, ctx, messages, covered, cfg) -> dict:
        ids, told, statuses = {}, {}, {}
        if ctx.cfg["memory"]["enabled"]:  # make sure every covered exchange can be read back
            self.memory.index(ctx.lane, ctx.session, ctx.project, messages[:covered], finished=True)
            ids, told, statuses = self.memory.leaves(ctx.lane), self.memory.summaries(ctx.lane), self.memory.statuses(ctx.lane)
        prefix = build_prefix(messages, covered, ids, int(cfg["target_tokens"] * CHARS_PER_TOKEN), told, statuses)
        state = {"covered": covered, "hash": fingerprint(messages[:covered]), "prefix": prefix, "ts": time.time()}
        ctx.store.put(f"compact:{ctx.lane}", state)
        ctx.store.run("delete from state where key = ?", (f"usage:{ctx.lane}",))  # the old count no longer describes it
        ctx.store.run("delete from state where key = ?", (f"stubs:{ctx.lane}",))  # the prefix now stands for those results
        return state

    def observe(self, ctx, orig, reply):
        if ctx.agent or not rq.has_client_tools(orig):
            return
        usage = reply.get("usage") or {}
        ctx.store.put(f"usage:{ctx.lane}", {"tokens": sse.input_tokens(usage) + int(usage.get("output_tokens") or 0),
                                             "messages": len(orig["messages"])})
