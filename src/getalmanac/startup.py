"""The startup layer: shorter skill and agent listings and deferred tool schemas.

Both rewrites are decided once per conversation and then frozen, so every later
request in that conversation is rewritten to the same bytes and the prompt
cache keeps hitting. Nothing is removed for good: a collapsed skill or agent can
still be invoked by name, and a deferred tool is loaded by the client's own tool
search, which the API expands from the request's tool list.
"""
import re

from . import request as rq
from .match import Index

SKILL_MARK = "The following skills are available for use with the Skill tool:"
SKILL_NOTE = ("Descriptions ending in … are shortened to save context. Every skill listed can be invoked by its "
              "name, and its full description is shown when a message calls for it.")
AGENT_MARK = "Available agent types for the Agent tool:"
AGENT_NOTE = ("Descriptions ending in … are shortened to save context. Every agent listed can still be used by its "
              "name, and its full description is shown when a message calls for it.")
_ENTRY = re.compile(r"(?m)^- ([\w.\-/]+(?::[\w.\-/]+)*): ")
DEFER_MARK = "The following deferred tools are now available via ToolSearch."
DEFER_TEXT = (DEFER_MARK + " Their schemas are NOT loaded — calling them directly will fail with "
              'InputValidationError. Use ToolSearch with query "select:<name>[,<name>...]" to load tool schemas '
              "before calling them:")
EXPAND_LIMIT = 20  # most skills ever expanded after the first turn of one conversation


def _project(ctx) -> str:
    return getattr(ctx, "project", "") or ctx.cwd


def _frozen(ctx, name, compute):
    if ctx.store is None:
        return compute()
    return ctx.store.frozen(f"{name}:{ctx.lane}", compute)


def _untooled(description: str) -> str:
    """An agent line's description without its trailing `(Tools: ...)`."""
    at = description.rfind(" (Tools: ")
    return description[:at] if at >= 0 and description.endswith(")") else description


def parse_listing(text: str, mark: str = SKILL_MARK):
    """(start, end, [(name, description)]) of the listing that opens with `mark` in `text`, or None."""
    start = text.find(mark)
    if start < 0:
        return None
    first = start + len(mark)
    tail = re.search(r"\n\n(?!- )", text[first:])  # the listing ends at a blank line not followed by an entry
    end = first + tail.start() if tail else len(text)
    chunk = text[first:end]
    marks = list(_ENTRY.finditer(chunk))
    entries = []
    for i, mark in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(chunk)
        entries.append((mark.group(1), chunk[mark.end():stop].rstrip()))
    return (start, end, entries) if entries else None


def stub(description: str, chars: int) -> str:
    """The opening of a description, cut at a word, or all of it when it is already short."""
    flat = " ".join(description.split())
    if len(flat) <= chars + 24:
        return flat
    cut = flat[:chars].rsplit(" ", 1)[0].rstrip(",.;:—-")
    return cut + "…"


def _insert_before_last(msg, text):
    content = msg.get("content")
    block = {"type": "text", "text": text}
    if isinstance(content, str):
        msg["content"] = [block, {"type": "text", "text": content}]
    elif isinstance(content, list):
        content.insert(max(0, len(content) - 1), block)


class _ListingLayer:
    """Collapse a listing's descriptions to a stub; show the full text when a message matches."""

    MARK, NOTE, KEY, HEADER, GAP = "", "", "", "", "\n\n"  # GAP: what separates the note from the first entry

    def __init__(self):
        self._indexes = {}

    def settings(self, ctx):
        """(stub characters, most entries shown in full per message), or None when the layer is off."""
        raise NotImplementedError

    def pinned(self, ctx, catalog) -> set:
        """Names this layer always shows in full."""
        return set()

    def shorten(self, description: str, chars: int) -> str:
        return stub(description, chars)

    def searchable(self, description: str) -> str:
        return description

    def _index(self, entries) -> Index:
        key = hash(tuple(entries))
        if key not in self._indexes:
            if len(self._indexes) > 16:
                self._indexes.clear()
            self._indexes[key] = Index({name: self.searchable(desc) for name, desc in entries})
        return self._indexes[key]

    def transform(self, body, ctx):
        chars_max = self.settings(ctx)
        if not chars_max:
            return
        chars, max_full = chars_max
        messages = body.get("messages") or []
        found = None
        for msg in messages[:8]:
            for block in rq.blocks(msg) if isinstance(msg.get("content"), list) else []:
                if block.get("type") == "text" and self.MARK in (block.get("text") or ""):
                    found = (block, parse_listing(block["text"], self.MARK))
                    break
            if found:
                break
        if not found or not found[1]:
            return
        block, (start, end, entries) = found
        catalog = dict(entries)
        index = self._index(entries)
        turns = rq.human_turns(messages)
        first = rq.human_text(messages[turns[0]]) if turns else ""

        def decide():
            keep = self.pinned(ctx, catalog)
            keep.update(name for name, _ in index.search(first, limit=max_full))
            return sorted(keep & set(catalog))

        full = set(_frozen(ctx, self.KEY, decide))
        first_full = len(full)  # fixed by the first typed text; the count below also grows with later turns
        lines = [f"- {name}: {desc if name in full else self.shorten(desc, chars)}" for name, desc in entries]
        text = block["text"]
        listing = self.MARK + "\n" + self.NOTE + self.GAP + "\n".join(lines)
        if len(listing) >= end - start:
            return  # nothing to gain: leave the request alone
        block["text"] = text[:start] + listing + text[end:]
        saved = (end - start) - len(listing)

        # Later human turns: show the full description of collapsed entries they match.
        shown, budget = set(full), EXPAND_LIMIT
        for at in turns[1:]:
            hits = [n for n, _ in index.search(rq.human_text(messages[at]), limit=max_full) if n not in shown]
            hits = hits[:budget]
            if not hits:
                continue
            budget -= len(hits)
            shown.update(hits)
            note = ("<system-reminder>\n" + self.HEADER + "\n"
                    + "\n".join(f"- {n}: {catalog[n]}" for n in hits) + "\n</system-reminder>")
            _insert_before_last(messages[at], note)
            saved -= len(note)
        ctx.stats[self.KEY] = {"listed": len(entries), "full": len(shown), "first_full": first_full, "saved_chars": saved}


class SkillsLayer(_ListingLayer):
    MARK, NOTE, KEY = SKILL_MARK, SKILL_NOTE, "skills"
    HEADER = "Full descriptions of skills that may be relevant to this message:"

    def settings(self, ctx):
        cfg = ctx.cfg["skills"]
        return (cfg["stub_chars"], cfg["max_full"]) if cfg["enabled"] else None

    def pinned(self, ctx, catalog) -> set:
        keep = set(ctx.cfg["skills"]["always_full"])
        if ctx.store is not None:
            used = ctx.store.recently_used("skill", _project(ctx))
            keep.update(name for name, count in used.items() if count >= 2)
        return keep


class AgentsLayer(_ListingLayer):
    """The same for the agent listing, whose lines end in `(Tools: ...)`; a stubbed line drops that suffix."""

    MARK, NOTE, KEY, GAP = AGENT_MARK, AGENT_NOTE, "agents", "\n"
    HEADER = "Full descriptions of agent types that may be relevant to this message:"

    def settings(self, ctx):
        chars = ctx.cfg["startup"]["agent_chars"]
        return (chars, ctx.cfg["skills"]["max_full"]) if chars > 0 else None

    def shorten(self, description: str, chars: int) -> str:
        cut = stub(_untooled(description), chars)
        return cut if cut.endswith("…") else description

    def searchable(self, description: str) -> str:
        return _untooled(description)


class ToolsLayer:
    """Defer the schemas of rarely used tools until the model asks for them."""

    def transform(self, body, ctx):
        cfg = ctx.cfg["tools"]
        tools = body.get("tools")
        if not cfg["enabled"] or not isinstance(tools, list):
            return
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        if "ToolSearch" not in names:  # without the search tool nothing could load them again
            return
        cold = [t for t in tools if isinstance(t, dict) and t.get("name") in cfg["cold"]
                and t.get("name") not in cfg["hot"] and not t.get("defer_loading") and "type" not in t
                and not (t.get("name") == "PowerShell" and "Bash" not in names)]
        if not cold:
            return
        messages = body.get("messages") or []
        turns = rq.human_turns(messages)
        first = rq.human_text(messages[turns[0]]) if turns else ""

        def decide():
            keep = set(Index({t["name"]: "" for t in cold}).named(first))  # the message names the tool
            if ctx.store is not None:
                used = ctx.store.recently_used("tool", _project(ctx))
                keep.update(name for name, count in used.items() if count >= cfg["promote_after"])
            for msg in messages:  # a tool this conversation already called must stay loaded
                for b in rq.blocks(msg):
                    if b.get("type") == "tool_use":
                        keep.add(b.get("name"))
            return sorted(t["name"] for t in cold if t["name"] not in keep)

        deferred = set(_frozen(ctx, "tools", decide))
        chosen = [t for t in cold if t["name"] in deferred]
        if not chosen:
            return
        saved = 0
        for tool in chosen:
            before = len(rq.dumps(tool))
            tool["defer_loading"] = True
            saved += before
        self._announce(messages, turns, sorted(t["name"] for t in chosen))
        ctx.stats["tools"] = {"deferred": sorted(t["name"] for t in chosen), "saved_chars": saved}

    @staticmethod
    def _announce(messages, turns, names):
        """Tell the model the deferred tools exist, in the list the client already keeps."""
        for msg in messages[:8]:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
                if text and DEFER_MARK in text:
                    at = text.find("\n\n", text.find(DEFER_MARK))
                    at = len(text) if at < 0 else at
                    block["text"] = text[:at] + "\n" + "\n".join(names) + text[at:]
                    return
        if turns:
            note = {"type": "text", "text": "<system-reminder>\n" + DEFER_TEXT + "\n" + "\n".join(names) + "\n</system-reminder>"}
            msg = messages[turns[0]]
            if isinstance(msg.get("content"), str):
                msg["content"] = [note, {"type": "text", "text": msg["content"]}]
            else:
                msg["content"].insert(0, note)

    def observe(self, ctx, orig, reply):
        """Remember which tools and skills this conversation used, to keep them loaded next time."""
        if ctx.store is None:
            return
        for block in reply["content"]:
            if block.get("type") != "tool_use":
                continue
            ctx.store.mark_used("tool", str(block.get("name")), _project(ctx), ctx.lane)
            if block.get("name") == "Skill":
                skill = (block.get("input") or {}).get("skill")
                if skill:
                    ctx.store.mark_used("skill", str(skill), _project(ctx), ctx.lane)
