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

The summary is written by rule, with no model. What it leaves out stays
readable: each exchange's transcript is in the local memory, one tool call away.
Compaction happens only at a human turn. Inside a long tool loop Claude Code's
own compaction still applies.
"""
import hashlib
import json
import time

from . import request as rq
from . import sse
from .memory import _first_line, exchange, transcript

CHARS_PER_TOKEN = 3.0  # conservative: real text runs a little over 3 characters a token
HEADER = ("Almanac compacted this conversation to keep it inside the context window. The messages before this "
          "point were replaced by the summary below, which was written by rule and can miss detail. Each earlier "
          "exchange is stored on this machine: when a detail matters, read it with the almanac memory tools "
          "(load them with ToolSearch; memory_read with full=true returns an exchange's transcript by its number "
          "in brackets). Tell the user once, in one short line, that earlier messages were compacted.")


ACK = "Understood. I have the summary of the earlier conversation and will continue from it."


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


def build_prefix(messages, covered, ids, budget_chars) -> list:
    """The messages that stand in for `messages[:covered]`."""
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
    for seq, group in reversed(groups[:first_full]):
        record = exchange(group)
        line = f"- {tag(seq)}{record['title']} -> {_first_line(record['answer'], 200)}"
        if record["notes"]:
            line += f" ({_first_line(record['notes'], 160)})"
        if len(line) > room:
            outline.append(f"- ({seq + 1} earlier exchange{'s' if seq else ''} not listed; find them with memory_search)")
            break
        outline.append(line)
        room -= len(line) + 1
    outline.reverse()

    parts = [HEADER]
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


class CompactLayer:
    def __init__(self, memory):
        self.memory = memory

    @staticmethod
    def _applies(body, ctx) -> bool:
        messages = body.get("messages") or []
        return (ctx.cfg["compaction"]["enabled"] and not ctx.agent and bool(body.get("tools"))
                and len(messages) > 2 and messages[0].get("role") == "user")

    def _estimate(self, body, ctx, messages) -> int:
        """Input tokens this request would take, from the last reply's exact count when there is one."""
        seen = ctx.store.get(f"usage:{ctx.lane}")
        if seen and 0 < seen["messages"] <= len(body["messages"]):
            added = sum(len(json.dumps(m, ensure_ascii=False)) for m in body["messages"][seen["messages"]:]
                        if m.get("role") != "assistant")  # the reply itself is already in the count
            return int(seen["tokens"] + added / CHARS_PER_TOKEN)
        size = len(json.dumps(body.get("system") or "", ensure_ascii=False))
        size += sum(len(json.dumps(t)) for t in body.get("tools") or [] if isinstance(t, dict) and not t.get("defer_loading"))
        size += sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        return int(size / CHARS_PER_TOKEN)

    def transform(self, body, ctx):
        if not self._applies(body, ctx):
            return
        cfg, messages, key = ctx.cfg["compaction"], body["messages"], f"compact:{ctx.lane}"
        if ctx.store.get(f"nocompact:{ctx.lane}"):  # the upstream refused this conversation's compacted form
            return
        state = ctx.store.get(key)
        if state and not (len(messages) >= state["covered"] and fingerprint(messages[:state["covered"]]) == state["hash"]):
            ctx.store.run("delete from state where key = ?", (key,))  # rewound or edited: the summary no longer fits
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
        if not state:
            return
        body["messages"] = json.loads(json.dumps(state["prefix"])) + messages[state["covered"]:]
        ctx.stats["compaction"] = {"covered": state["covered"], "fresh": fresh,
                                   "prefix_chars": len(json.dumps(state["prefix"], ensure_ascii=False))}

    def _compact(self, ctx, messages, covered, cfg) -> dict:
        ids = {}
        if ctx.cfg["memory"]["enabled"]:  # make sure every covered exchange can be read back
            self.memory.index(ctx.lane, ctx.session, ctx.project, messages[:covered], finished=True)
            ids = self.memory.leaves(ctx.lane)
        prefix = build_prefix(messages, covered, ids, int(cfg["target_tokens"] * CHARS_PER_TOKEN))
        state = {"covered": covered, "hash": fingerprint(messages[:covered]), "prefix": prefix, "ts": time.time()}
        ctx.store.put(f"compact:{ctx.lane}", state)
        ctx.store.run("delete from state where key = ?", (f"usage:{ctx.lane}",))  # the old count no longer describes it
        return state

    def observe(self, ctx, orig, reply):
        if ctx.agent or not orig.get("tools"):
            return
        usage = reply.get("usage") or {}
        ctx.store.put(f"usage:{ctx.lane}", {"tokens": sse.input_tokens(usage) + int(usage.get("output_tokens") or 0),
                                             "messages": len(orig["messages"])})
