"""Compaction: replace a long history with a summary once, then keep it byte for byte.

When a conversation passes the trigger size and the person sends a new message,
everything before that message is replaced by:

- the session-start context exactly as it was (instructions, environment, the
  skill listing, tools added in the session),
- an outline of the earlier exchanges, written in generations, oldest first, and
- the most recent exchanges written out in full.

The replacement is stored. Every later request sends the same stored prefix
followed by the messages that came after it, so the prompt cache keeps hitting.
It is dropped if the covered messages change (a rewind or an edit).

A later compaction does not rebuild it. It keeps the session context and every
generation byte for byte, adds ONE generation for the exchanges since (the ones
that were verbatim included) and writes new recent exchanges after it, so the
cache still reads up to the last old generation and only the tail is written. A
cache breakpoint on the newest generation makes that prefix a cache entry. Each
generation has its own sections (see below) and, when the small model is on, a
short note joining its exchanges' decisions and open items (summarize.py, one
call per generation; without it the generation is the rule-written outline, and
a generation is never edited once sent). When the generations together outgrow
`compaction.outline_share` of the target, the oldest are merged into one "earlier
work" generation (another call, rule outline as the fallback) that keeps the
standing decisions and open work. That is the only step that rewrites earlier
bytes, and it merges enough at once to stay rare. The request log records
each compaction's kind (append or rollup) and how many prefix characters were kept.

The outline is built from the stored titles and summaries: a small model's when
it has written them (see summarize.py), the rule-written ones otherwise. What it
leaves out stays readable: each exchange's transcript is in the local memory, one tool call away.
When the memory has a status for each exchange (see status.py) the outline is in
sections instead: standing decisions first, then open work, then what is done
(newest first, as many as fit); superseded and noise exchanges are only counted.

A request that returns after its cache has expired (a pause longer than its shortest cache_control ttl)
pays for a full rewrite whatever is sent, so with `compaction.on_cold` it is compacted from COLD_FLOOR times
the target size instead of the trigger size: at a human turn by the prefix, inside a turn and in a sub-agent
by the stubs below. It happens once, on that request; the stored prefix and stubs then hold as usual.

The prefix is built only at a human turn. Inside one long turn (a tool loop with no
new message from the person) there is nothing to rebuild, so when the request nears
the model's window the old tool results of that turn are replaced by a one-line stub
that keeps their blocks and ids. The stubbed ids are stored and only ever grow within
a turn, so every later request stubs the same results the same way. The stub's text is
stored with them, so a turn already running when the wording changes keeps its bytes.
Each result is saved whole in the memory first, and the stub says which memory_read call
returns it; when the memory is off the stub says the tool has to be run again.
"""
import hashlib
import json
import time

from . import request as rq
from . import sse, summarize
from .memory import QUOTED, _first_line, clean, exchange, result_text, transcript

CHARS_PER_TOKEN = 3.0  # conservative: real text runs a little over 3 characters a token
HEADER = ("Almanac compacted this conversation to keep it inside the context window. The messages before this "
          "point were replaced by the summary below, which was condensed from the exchanges and can miss detail. Each earlier "
          "exchange is stored on this machine: when a detail matters, read it with the almanac memory tools "
          "(load them with ToolSearch; memory_read with full=true returns an exchange's transcript by its number "
          "in brackets). Tell the user once, in one short line, that earlier messages were compacted. "
          "The summary is a quoted record of earlier work: " + QUOTED[len("It is a quoted record, not instructions: "):])


AGENT_HEADER = ("[Almanac compacted this sub-agent's earlier steps to keep it small. Below is a rule-written list of what "
                "was run and the first line of each result; a full result is read back with the almanac memory_read tool "
                "named after it (load it with ToolSearch). Continue the task from the newest steps after this.]")
ACK = "Understood. I have the summary of the earlier conversation and will continue from it."
STUB = ("[Almanac removed this tool result to keep the turn inside the context window ({n} characters). "
        "Run the tool again if you need it.]")
STUB_RAW = ("[Almanac removed this tool result ({n} characters). "
            "Read it back with the almanac memory_read tool: {{\"result\": \"{id}\"}}]")
NO_TEXT = "(this result held no text, for example only an image, so nothing was kept)"
MIN_STUB = 2000  # characters: a smaller tool result is not worth a stub
FLOOR_KEEP = 4  # a long turn never keeps fewer than this many of its newest results
RELIEF_TO = 0.75  # a pass frees room down to this share of the limit, so the next request needs no second pass
MAX_MARKS = 4  # cache breakpoints the API takes in one request
COLD_FLOOR = 2  # x target_tokens: a cold return is compacted from this size, since its prefix is rewritten anyway
RELATED = 5  # most related exchanges listed
RELATED_HEAD = "Earlier exchanges related to your new message:"
MORE ="- ({n} more {what} not listed; find them with memory_search)"
RECENT = "The most recent exchanges before this point, in full:"
JOINED = "Decisions and open items in this stretch, joined across its exchanges (a later item can replace an earlier one):"
GEN_SHARE = 0.5  # most of the outline budget one generation may take
ROLLUP_TO = 0.5  # a roll-up leaves the generations at about this share of the outline budget, so it does not repeat soon
ROLLUP_SHARE = 0.25  # most of the outline budget the merged "earlier work" generation may take
SYNTH_MIN = 4  # a stretch of fewer exchanges is not worth a model call
SYNTH_RESERVE = 1000  # characters a model's joined notes may add to a generation, counted before deciding on a roll-up


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


def _sections(groups, lo, hi, tag, told, statuses, room, skip_done=False) -> list:
    """The outline of exchanges lo..hi-1 by status: decisions, open work, done, and a count of what is left out on purpose."""
    lines, hidden = {"decision": [], "open": [], "done": []}, 0
    for seq in range(lo, hi):
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
    shown["done"], room, left["done"] = ([], room, len(lines["done"])) if skip_done else _fill(lines["done"][::-1], room)
    names = {"decision": "standing decisions", "open": "open exchanges", "done": "finished exchanges"}
    parts = []
    for key in heads:
        body = shown[key] + ([MORE.format(n=left[key], what=names[key])] if left[key] else [])
        if body:
            parts.append(heads[key] + "\n" + "\n".join(body))
    return parts + ([closing] if closing else [])


def _gen(groups, lo, hi, tag, told, statuses, room, title, first, joined=(), skip_done=False) -> str:
    """One generation of the outline: exchanges lo..hi-1, under a title, with the model's joined notes when it wrote some."""
    parts = ([HEADER] if first else []) + [title]
    room -= len(title)
    if joined:
        parts.append(JOINED + "\n" + "\n".join("- " + line for line in joined))
        room -= len(parts[-1])
    if statuses:
        return "\n\n".join(parts + _sections(groups, lo, hi, tag, told, statuses, room, skip_done))
    flat = []
    for seq in range(hi - 1, lo - 1, -1):  # without statuses: one list, newest first until the room is out
        record = exchange(groups[seq][1])
        name, said = told.get(seq) or (record["title"], _first_line(record["answer"], 200))
        line = f"- {tag(seq)}{name} -> {said}"
        if record["notes"]:
            line += f" ({_first_line(record['notes'], 160)})"
        if len(line) > room:
            flat.append(f"- ({seq + 1 - lo} earlier exchange{'s' if seq > lo else ''} not listed; find them with memory_search)")
            break
        flat.append(line)
        room -= len(line) + 1
    return "\n\n".join(parts + (["Earlier exchanges, oldest first:\n" + "\n".join(flat[::-1])] if flat else []))


def _digest(groups, lo, hi, tag, told, statuses, only=None) -> list:
    """One line per exchange lo..hi-1 for the model that joins them: id, status, title, summary. Noise is left out."""
    out = []
    for seq in range(lo, hi):
        status = statuses.get(seq, "done")
        if status == "noise" or (only and status not in only):
            continue
        record = exchange(groups[seq][1])
        title, said = told.get(seq) or (record["title"], record["answer"])
        out.append(f"{tag(seq) or f'[{seq + 1}] '}({status}) {title}: {_first_line(said, 240)}")
    return out


def _rolled(sizes, budget) -> int:
    """How many of the oldest generations to merge (0: none). Enough at once that the total lands well under the budget."""
    if len(sizes) < 2 or sum(sizes) <= budget:
        return 0
    return next((k for k in range(2, len(sizes)) if sum(sizes[k:]) + budget * ROLLUP_SHARE + len(HEADER) <= budget * ROLLUP_TO), len(sizes))


def _wrap(text) -> dict:
    return {"type": "text", "text": "<system-reminder>\n" + clean(text) + "\n</system-reminder>"}  # model text and stored titles too


def build_prefix(messages, covered, ids, budget_chars, told=None, statuses=None, related=None,
                 outline_chars=None, prev=None, synth=None, info=None) -> list:
    """The messages that stand in for `messages[:covered]`.

    `told` maps seq -> (title, summary) written by a model; `statuses` maps seq -> status (see status.py).
    Without statuses the outline is one list in order, with them it is in sections.
    `related` is the search hits (id, title, snippet) for the message that triggers the compaction.

    The user message holds the session-start reminders, then one block per generation of the outline (oldest
    first), then the recent exchanges in full. With `outline_chars` (the room for all generations together) and the
    `prev` state of an earlier compaction, the earlier generations are kept byte for byte and one is added for the
    exchanges since; only when they outgrow `outline_chars` are the oldest merged into one. Without `outline_chars`
    there is one generation, which may take what the recent exchanges leave over.
    `synth(kind, text)` asks a small model for ([lines], why) joining a generation's exchanges ("append") or the
    oldest generations ("rollup"); with no lines the generation is the rule-written outline alone.
    `info` is filled with what was done, for the caller to store and log.
    """
    told, info = told or {}, {} if info is None else info
    old = messages[:covered]
    groups = list(_groups(old, rq.human_turns(old)))
    legacy = outline_chars is None
    synth = None if legacy or not ids else synth

    def tag(seq):
        return f"[{ids[seq]}] " if seq in ids else ""

    if prev:
        content, nopen = prev["prefix"][0]["content"], prev["nopen"]
        opening, blocks = content[:nopen], [dict(b) for b in content[nopen:nopen + len(prev["gens"])]]
        gens, lo = [dict(g) for g in prev["gens"]], min(prev["recent_from"], len(groups))
    else:  # session-start context survives as it was: the reminders of the first message
        opening = [_strip(b) for b in rq.blocks(old[0])
                   if b.get("type") == "text" and (b.get("text") or "").lstrip().startswith(rq.REMINDER)]
        blocks, gens, lo = [], [], 0

    # Newest first: write whole exchanges until the recent share of the budget is used.
    recent_budget = budget_chars * 2 // 3 if legacy else budget_chars - outline_chars
    full, used = [], 0
    for seq in range(len(groups) - 1, lo - 1, -1):
        text = transcript(groups[seq][1], limit=max(4000, budget_chars // 3))
        if full and used + len(text) > recent_budget:
            break
        full.append((seq, text))
        used += len(text)
    full.reverse()
    first_full = full[0][0] if full else len(groups)
    seqs = {node: seq for seq, node in ids.items()}
    near = [f"- [{h['id']}] {h['title']} -> {_first_line(h['snippet'], 200)}" for h in (related or [])
            if seqs.get(h["id"], first_full) < first_full][:RELATED]  # those in full are already there
    note = (RELATED_HEAD + "\n" + "\n".join(near)) if near else ""
    recent = [p for p in (note, (RECENT + "\n\n" + "\n\n".join(
        f"--- Exchange {seq + 1} {tag(seq)}---\n{text}" for seq, text in full)) if full else "") if p]

    def build(glo, ghi, room, title, first, kind, skip_done=False, known=()):
        """(text, meta) of the generation over glo..ghi-1, with a model's joined notes when there is something to join."""
        found, why = [], "off"
        if synth:
            digest = list(known) + _digest(groups, glo, ghi, tag, told, statuses or {}, ("decision", "open") if kind == "rollup" else None)
            found, why = [], "short"
            if len(digest) >= (1 if kind == "rollup" else SYNTH_MIN):
                try:
                    found, why = synth(kind, "\n".join(digest))
                except Exception:  # fail open: the rule-written outline stands
                    found, why = [], "error"
        if synth:
            info["rollup_synth" if kind == "rollup" else "synth"] = "ok" if found else (why or "empty")
        return (_gen(groups, glo, ghi, tag, told, statuses, room, title, first, found, skip_done),
                {"lo": glo, "hi": ghi, "kind": kind, "lines": list(found)})

    cap = int(outline_chars * GEN_SHARE) if not legacy else budget_chars - used - len(HEADER) - 400 - len(note)
    new, title = first_full > lo, f"Exchanges {lo + 1} to {first_full}:"
    sizes = [len(b["text"]) for b in blocks]
    if new and not legacy:
        sizes.append(len(_gen(groups, lo, first_full, tag, told, statuses, cap, title, not gens)) + (SYNTH_RESERVE if synth else 0))
    k = 0 if legacy else _rolled(sizes, outline_chars)
    info.update(kind="rollup" if k else "append", rolled=min(k, len(gens)), gen_chars=0,
                kept_chars=len(json.dumps(opening + (blocks if not k else []))) if prev else 0)
    if k:  # the one change to earlier bytes: the oldest generations merge, and every later byte moves with them
        n, absorb = min(k, len(gens)), new and k > len(gens)
        end = first_full if absorb else gens[n - 1]["hi"]
        text, meta = build(0, end, int(outline_chars * ROLLUP_SHARE), f"Earlier work, rolled up (exchanges 1 to {end}):", True,
                           "rollup", skip_done=True, known=[line for g in gens[:n] for line in g["lines"]])
        gens, blocks, new = [meta] + gens[n:], [_wrap(text)] + blocks[n:], new and not absorb
        info["gen_chars"] = len(text)
    if new:
        text, meta = build(lo, first_full, cap, title, not gens, "append")
        gens, blocks = gens + [meta], blocks + [_wrap(text)]
        info["gen_chars"] = len(text)
    recent = recent if blocks else [HEADER] + recent  # with no earlier generation the recent block carries the explanation
    prefix = [{"role": "user", "content": opening + blocks + ([_wrap("\n\n".join(recent))] if recent else [])}]
    info.update(gens=gens, recent_from=first_full, nopen=len(opening))
    # Every system message of the covered range (hook output, environment, skill listing, tools added) survives.
    # The API takes a system message only directly before an assistant message, so the kept system messages
    # become one, and a short assistant message closes the prefix. A later generation adds to the end of it.
    before = prev["prefix"][1]["content"] if prev and prev["prefix"][1].get("role") == "system" else []
    system = list(before) + [_strip(b) for m in old[prev["covered"] if prev else 1:] if m.get("role") == "system" for b in rq.blocks(m)]
    if system:
        prefix.append({"role": "system", "content": system})
    prefix.append({"role": "assistant", "content": [{"type": "text", "text": ACK}]})
    return prefix


def estimate(body, ctx, messages, seen=False) -> int:
    """Input tokens this request would take, from the last reply's exact count when there is one (or from `seen`)."""
    if seen is False:
        seen = ctx.state.get(f"usage:{ctx.lane}")
    if seen and 0 < seen["messages"] <= len(body["messages"]):
        start = seen["messages"]
        added = sum(len(json.dumps(m, ensure_ascii=False)) for i, m in enumerate(body["messages"][start:], start)
                    if not (i == start and m.get("role") == "assistant"))  # only that request's own reply is in the count; a late count leaves later replies out
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


RETRY_AFTER = 40  # messages: a refused compaction is tried again after this much growth, since Claude Code's own is off


def blocked(ctx, messages) -> bool:
    """True while a refusal of this conversation's compacted form still holds. It lapses after RETRY_AFTER more messages,
    so a long conversation is never left with no compaction at all (an old mark without a count lapses at once)."""
    mark = ctx.state.get(f"nocompact:{ctx.lane}")
    if not mark:
        return False
    at = mark.get("at") if isinstance(mark, dict) else None
    return isinstance(at, int) and len(messages) < at + RETRY_AFTER

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
        if blocked(ctx, messages):  # the upstream refused this conversation's compacted form not long ago
            return
        state = ctx.state.get(key)
        if state and not (len(messages) >= state["covered"] and fingerprint(messages[:state["covered"]]) == state["hash"]):
            ctx.state.run("delete from state where key = ?", (key,))  # rewound or edited: the summary no longer fits
            ctx.state.run("delete from state where key = ?", (f"usage:{ctx.lane}",))  # its count described the compacted body
            ctx.state.run("delete from state where key = ?", (f"stubs:{ctx.lane}",))  # so did the stubs
            state = None
        fresh = False
        size = self._size_if_cold(body, ctx, (state["prefix"] + messages[state["covered"]:]) if state else messages)  # what would go out without the cold rule
        covered = len(messages) - 1
        at_human_turn = bool(rq.human_text(messages[-1])) and len(rq.human_turns(messages[:covered])) >= 1
        if at_human_turn and (not state or covered > state["covered"]):
            tail = messages[state.get("at", state["covered"]):] if state else messages  # what came after the last outline
            current = (state["prefix"] + tail) if state else messages
            # After a compaction, wait for real growth before the next one: redoing it
            # every turn would rewrite the prefix each time and waste the cache. A cold return rewrites it anyway.
            grown = not state or self._cold(ctx, cfg) or sum(len(json.dumps(m, ensure_ascii=False)) for m in tail) / CHARS_PER_TOKEN >= cfg["target_tokens"] / 2
            if grown and self._estimate(body, ctx, current) >= self._trigger(ctx, cfg):
                state, fresh = self._compact(ctx, body, messages, covered, cfg, state), True
        out = messages
        if state:
            out = json.loads(json.dumps(state["prefix"])) + messages[state["covered"]:]
            self._mark(body, out, state)
            ctx.stats["compaction"] = {"covered": state["covered"], "fresh": fresh,
                                       "prefix_chars": len(json.dumps(state["prefix"], ensure_ascii=False))}
            if fresh:
                ctx.stats["compaction"].update(state["log"])
        had = self._stubs(ctx)
        stubbed = self._relieve(body, ctx, cfg, out, at_human_turn)
        if stubbed:
            ctx.stats.setdefault("compaction", {})["stubbed"] = stubbed  # under this mark a refused body is retried without it
        if state or stubbed:
            body["messages"] = out
        self._note_cold(body, ctx, size, out, fresh or self._stubs(ctx) > had)

    @staticmethod
    def _cold(ctx, cfg) -> bool:
        return bool(ctx.cold and cfg.get("on_cold", True))

    @classmethod
    def _trigger(cls, ctx, cfg) -> int:
        return min(cfg["trigger_tokens"], COLD_FLOOR * cfg["target_tokens"]) if cls._cold(ctx, cfg) else cfg["trigger_tokens"]

    @staticmethod
    def _stubs(ctx) -> int:
        return len((ctx.state.get(f"stubs:{ctx.lane}") or {}).get("ids") or {})

    @classmethod
    def _size_if_cold(cls, body, ctx, messages) -> int:
        """The request's estimated tokens before any change, kept only to log a cold return's saving (the same count for both sides)."""
        return estimate(body, ctx, messages, None) if cls._cold(ctx, ctx.cfg["compaction"]) else 0

    @classmethod
    def _note_cold(cls, body, ctx, before, after, acted):
        if acted and cls._cold(ctx, ctx.cfg["compaction"]):
            ctx.stats["cold"] = {"before": before, "after": estimate(body, ctx, after, None)}

    def _relieve_agent(self, body, ctx):
        """A sub-agent thread has no stored prefix, but a long one still fills its window: only the stubs apply to it."""
        cfg, messages = ctx.cfg["compaction"], body.get("messages") or []
        if not (cfg["enabled"] and ctx.agent and rq.has_client_tools(body) and len(messages) > 2)                 or blocked(ctx, messages):
            return
        had, size = self._stubs(ctx), self._size_if_cold(body, ctx, messages)
        try:
            out = self._outline_agent(body, ctx, cfg)
        except Exception:  # fail open: the request goes out with its stubs only
            out = messages
        stubbed = self._relieve(body, ctx, cfg, out, False)
        if stubbed:
            ctx.stats.setdefault("compaction", {})["stubbed"] = stubbed
        body["messages"] = out
        self._note_cold(body, ctx, size, out, self._stubs(ctx) > had)

    def _outline_agent(self, body, ctx, cfg):
        """A sub-agent's older steps become a rule-written outline once it passes `stub_tokens` (see AGENT_HEADER).

        The outline is stored with the steps it covers and sent byte for byte on every later request; when the agent
        grows past the limit again, the new steps are added as one more block after the old ones, which stay as they were."""
        messages, key = body["messages"], f"agentcompact:{ctx.lane}"
        limit = self._agent_limit(body, ctx, cfg)
        if not (limit and cfg.get("agent_outline", True)):
            return messages
        state = ctx.state.get(key)
        if state and not (len(messages) > state["covered"] and fingerprint(messages[:state["covered"]]) == state["hash"]):
            ctx.state.run("delete from state where key = ?", (key,))  # rewound or edited
            state = None
        current = (state["prefix"] + messages[state["covered"]:]) if state else messages
        tail = messages[state.get("at", state["covered"]):] if state else messages  # what came after the last outline
        grown = not state or sum(len(json.dumps(m, ensure_ascii=False)) for m in tail) / CHARS_PER_TOKEN >= limit / 2  # not every request
        if grown and estimate(body, ctx, current, None) >= limit:
            state = self._agent_prefix(ctx, messages, state, cfg, limit) or state
        if not state:
            return messages
        ctx.stats.setdefault("compaction", {})["agent_outline"] = state["covered"]
        return json.loads(json.dumps(state["prefix"])) + messages[state["covered"]:]

    @staticmethod
    def _agent_limit(body, ctx, cfg) -> int:
        """Tokens past which a sub-agent is outlined: stub_tokens, or the window share when that is smaller; 0 is off."""
        from . import routing  # routing imports this module
        if not cfg.get("stub_tokens"):
            return 0
        window = routing.window(ctx.cfg["routing"], str(body.get("model") or ""))
        share = float(cfg.get("window_share") or 0) * min(window, int(ctx.state.get(f"window:{ctx.lane}") or window))
        return int(min(cfg["stub_tokens"], share) if share > 0 else cfg["stub_tokens"])

    def _agent_prefix(self, ctx, messages, state, cfg, limit):
        cut = len(messages) - 2 * int(cfg["keep_results"])  # the newest steps stay whole
        while cut > 1 and messages[cut].get("role") != "assistant":
            cut -= 1
        start = state["covered"] if state else 1
        if cut <= start or messages[0].get("role") != "user":
            return None
        memory = ctx.cfg["memory"]["enabled"] and ctx.cfg["memory"].get("tools", True)
        lines = []
        for msg in messages[start:cut]:
            for b in rq.blocks(msg):
                kind = b.get("type")
                if kind == "text" and msg.get("role") == "assistant" and (b.get("text") or "").strip():
                    lines.append("- said: " + _first_line(b["text"])[:200])
                elif kind == "tool_use":
                    args = b.get("input") if isinstance(b.get("input"), dict) else {}
                    what = next((str(args[k]) for k in ("file_path", "command", "pattern", "path", "url", "description", "query") if args.get(k)), "")
                    lines.append(f"- ran {b.get('name')}: {' '.join(what.split())[:160]}")
                elif kind == "tool_result" and isinstance(b.get("tool_use_id"), str):
                    text = result_text(b) or NO_TEXT
                    where = ""
                    if memory:
                        try:
                            if not self.memory.has_raw(ctx.lane, b["tool_use_id"]):
                                self.memory.save_raw(ctx.lane, b["tool_use_id"], text, cleaned=True)
                            where = f' (memory_read {{"result": "{b["tool_use_id"]}"}})'
                        except Exception:
                            where = ""
                    flag = "error" if b.get("is_error") else "result"
                    lines.append(f"  {flag}{where}: {' '.join(_first_line(text).split())[:160]}")
        room = int(limit * CHARS_PER_TOKEN * 0.15)  # one block's share: the newest lines win
        while lines and sum(len(x) + 1 for x in lines) > room:
            lines = lines[len(lines) // 4 + 1:]
            if not lines or not lines[0].startswith("(earlier"):
                lines.insert(0, "(earlier steps left out; their results are still in memory)")
        block = {"type": "text", "text": ("" if state else AGENT_HEADER + "\n\n") + "\n".join(lines)}
        if state:
            prefix = json.loads(json.dumps(state["prefix"]))
        else:
            task = messages[0].get("content")
            prefix = [{"role": "user", "content": [{"type": "text", "text": task}] if isinstance(task, str) else json.loads(json.dumps(task))}]
        prefix[0]["content"].append(block)
        state = {"covered": cut, "at": len(messages), "hash": fingerprint(messages[:cut]), "prefix": prefix}
        ctx.state.put(f"agentcompact:{ctx.lane}", state)
        return state

    def _relieve(self, body, ctx, cfg, out, at_human_turn) -> int:
        """Stub the old tool results of the current turn (see the module docstring). Returns how many are stubbed in `out`.

        It changes the blocks of `out` in place and never raises: when it cannot decide, only the stubs already stored apply.
        """
        share, key = float(cfg.get("window_share") or 0), f"stubs:{ctx.lane}"
        if share <= 0:
            return 0
        same = []
        try:
            kept = ctx.state.get(key) or {}
            ids = dict(kept.get("ids") or {})
            # The wording is chosen when a set is first written and stored with it: a set written by an earlier
            # version has none and keeps the old wording, so a turn in progress sends the same bytes.
            tpl = kept.get("tpl") or (STUB if ids else STUB_RAW if ctx.cfg["memory"]["enabled"] and ctx.cfg["memory"].get("tools", True) else STUB)  # memory_read only when its server is loaded
            same = [b for b in _results(out) if b["tool_use_id"] in ids]
            for block in same:  # the stubs of earlier requests, wherever those results now sit
                block["content"] = tpl.format(n=ids[block["tool_use_id"]], id=block["tool_use_id"])
            if at_human_turn:
                return len(same)
            from . import routing  # routing imports this module

            turns = rq.human_turns(out)
            current = _results(out[(turns[-1] + 1 if turns else 0):])
            window = routing.window(ctx.cfg["routing"], str(body.get("model") or ""))
            limit = share * min(window, int(ctx.state.get(f"window:{ctx.lane}") or window))  # a size refusal taught the real one
            if self._cold(ctx, cfg):
                limit = min(limit, COLD_FLOOR * cfg["target_tokens"])
            if cfg.get("stub_tokens"):  # a 1M window makes 85% far too late: every request re-reads the whole history
                limit = min(limit, cfg["stub_tokens"])
            # The last reply's count may not be stored yet (it is written in the background): the count made when
            # the stubs were last changed stands in for it, so a second pass is not made for a body already shrunk.
            counts = [c for c in (ctx.state.get(f"usage:{ctx.lane}"), kept.get("est")) if c and 0 < c["messages"] <= len(body["messages"])]
            seen = max(counts, key=lambda c: c["messages"], default=None)
            tokens = estimate(body, ctx, out, seen)
            if tokens < limit:
                return len(same)
            keep, floor = int(kept.get("keep") or cfg["keep_results"]), min(FLOOR_KEEP, int(cfg["keep_results"]))
            while True:
                old = current[:max(0, len(current) - keep)]
                fresh = [(b, _size(b.get("content"))) for b in old if b["tool_use_id"] not in ids and not b.get("is_error")]
                fresh = [(b, n) for b, n in fresh if n >= MIN_STUB]
                freed = sum(n - len(tpl.format(n=n, id=b["tool_use_id"])) for b, n in fresh) / CHARS_PER_TOKEN  # a stub is not free
                if tokens - freed <= limit * RELIEF_TO or keep <= floor:
                    break
                keep = max(floor, keep - 4)
        except Exception:  # fail open: the request goes out with the stubs it already had
            return len(same)
        for block, n in fresh:
            if "{id}" in tpl and ctx.cfg["memory"]["enabled"]:  # save it whole before it is replaced; a result that cannot be saved stays as it is
                try:
                    if not self.memory.has_raw(ctx.lane, block["tool_use_id"]):  # a long turn's results are often saved already
                        self.memory.save_raw(ctx.lane, block["tool_use_id"], result_text(block) or NO_TEXT, cleaned=True)
                except Exception:
                    continue
            ids[block["tool_use_id"]] = n
            block["content"] = tpl.format(n=n, id=block["tool_use_id"])
        if fresh or keep != kept.get("keep"):
            ctx.state.put(key, {"ids": ids, "keep": keep, "tpl": tpl,
                                "est": {"messages": len(body["messages"]), "tokens": int(tokens - freed)}})
        return len(same) + len(fresh)

    @staticmethod
    def _mark(body, out, state):
        """A cache breakpoint on the newest generation, so that a later compaction still reads the prefix up to it.

        Only where the request has room (the API takes four) and with the life of the client's own breakpoints
        after it, since a longer one may not follow a shorter one. The stored prefix never holds it.
        """
        if not state.get("gens"):
            return
        later = [b["cache_control"] for m in out[len(state["prefix"]):] for b in rq.blocks(m) if isinstance(b.get("cache_control"), dict)]
        if not later or json.dumps([body.get("tools"), body.get("system"), out]).count('"cache_control"') >= MAX_MARKS:
            return
        out[0]["content"][state["nopen"] + len(state["gens"]) - 1]["cache_control"] = (
            {"type": "ephemeral", "ttl": "1h"} if any(c.get("ttl") == "1h" for c in later) else {"type": "ephemeral"})

    def _compact(self, ctx, body, messages, covered, cfg, state) -> dict:
        """Build the prefix: a new generation added to the stored ones, or the oldest merged (see build_prefix)."""
        ids, told, statuses = {}, {}, {}
        if ctx.cfg["memory"]["enabled"]:  # make sure every covered exchange can be read back
            self.memory.index(ctx.lane, ctx.session, ctx.project, messages[:covered], finished=True)
            ids, told, statuses = self.memory.leaves(ctx.lane), self.memory.summaries(ctx.lane), self.memory.statuses(ctx.lane)
        near = self.memory.related(ctx.lane, rq.human_text(messages[covered])) if ids else []
        target, info = int(cfg["target_tokens"] * CHARS_PER_TOKEN), {}
        prefix = build_prefix(messages, covered, ids, target, told, statuses, near, outline_chars=int(target * cfg["outline_share"]),
                              prev=state if state and state.get("gens") else None, info=info,
                              synth=lambda kind, text: summarize.outline_lines(ctx, getattr(ctx, "orig", None) or body, kind, text))
        log = {k: info[k] for k in ("kind", "rolled", "gen_chars", "kept_chars", "synth", "rollup_synth") if k in info}
        log["gens"] = len(info["gens"])
        state = {"covered": covered, "hash": fingerprint(messages[:covered]), "prefix": prefix, "ts": time.time(),
                 "gens": info["gens"], "recent_from": info["recent_from"], "nopen": info["nopen"], "log": log}
        ctx.state.put(f"compact:{ctx.lane}", state)
        ctx.state.run("delete from state where key = ?", (f"usage:{ctx.lane}",))  # the old count no longer describes it
        ctx.state.run("delete from state where key = ?", (f"stubs:{ctx.lane}",))  # the prefix now stands for those results
        return state

    def observe(self, ctx, orig, reply):
        if ctx.agent or not rq.has_client_tools(orig):
            return
        usage = reply.get("usage") or {}
        ctx.state.put(f"usage:{ctx.lane}", {"tokens": sse.input_tokens(usage) + int(usage.get("output_tokens") or 0),
                                             "messages": len(orig["messages"])})
