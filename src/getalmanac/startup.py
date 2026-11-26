"""The startup layer: shorter skill and agent listings and deferred tool schemas.

Both rewrites are decided once per conversation and then frozen, so every later
request in that conversation is rewritten to the same bytes and the prompt
cache keeps hitting. Nothing is removed for good: a collapsed skill or agent can
still be invoked by name, and a deferred tool is loaded by the client's own tool
search, which the API expands from the request's tool list.
"""
import re
import time

from . import request as rq
from .match import Index

SKILL_MARK = "The following skills are available for use with the Skill tool:"
SKILL_NOTE = ("Descriptions ending in … are shortened to save context. Every skill listed can be invoked by its "
              "name, and its full description is shown when a message calls for it.")
SKILL_NOTE_NAMES = ("Only skill names are listed to save context. Every skill listed can be invoked by its name, "
                    "and its full description is shown when a message calls for it.")
AGENT_MARK = "Available agent types for the Agent tool:"
AGENT_NOTE = ("Descriptions ending in … are shortened to save context; the full description is shown when a message "
              "calls for it. When a listed specialist agent fits the task, use it (set subagent_type to its name) "
              "instead of general-purpose; use general-purpose only when none fits.")
_ENTRY = re.compile(r"(?m)^- ([\w.\-/]+(?::[\w.\-/]+)*)(?:: |[ ]*$)")  # `- name: text`, or a bare `- name`
DEFER_MARK = "The following deferred tools are now available via ToolSearch."
DEFER_TEXT = (DEFER_MARK + " Their schemas are NOT loaded — calling them directly will fail with "
              'InputValidationError. Use ToolSearch with query "select:<name>[,<name>...]" to load tool schemas '
              "before calling them:")
EXPAND_LIMIT = 20  # most skills ever expanded after the first turn of one conversation


def _project(ctx) -> str:
    return getattr(ctx, "project", "") or ctx.cwd


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _sub_type(ctx):
    """The agent type whose own usage a sub-agent's listings pin; None for the main thread and a fork, which pin the main thread's."""
    return ctx.agent_type if ctx.thread == "sub" else None


def _daily(ctx, name, compute):
    """A per-project, per-day snapshot of what drifts with usage, so new conversations of one project agree.
    A sub-agent's own type has its own snapshot."""
    if ctx.state is None:
        return compute()
    sub = _sub_type(ctx)
    return ctx.state.frozen(f"{name}{f':{sub}' if sub is not None else ''}-day:{_project(ctx)}:{_today()}", compute)


def _frozen(ctx, name, compute):
    if ctx.state is None:
        return compute()
    return ctx.state.frozen(f"{name}:{ctx.frozen_lane or ctx.lane}", compute)  # a fork shares its parent's cached prefix


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
    """The opening of a description, cut at a word, or all of it when it is already short. 0 chars keeps no description."""
    if chars <= 0:
        return ""
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

    NOTE_NAMES = ""  # the note used when no description is kept at all
    KIND = ""  # the usage `pinned` reads (tool, skill or agent); none pins nothing
    MARK, NOTE, KEY, HEADER, GAP = "", "", "", "", "\n\n"  # GAP: what separates the note from the first entry

    ALWAYS_NOTE = False  # true: the note goes in even when nothing was shortened

    def __init__(self):
        self._indexes = {}

    def settings(self, ctx):
        """(stub characters, most entries shown in full per message), or None when the layer is off."""
        raise NotImplementedError

    def always_full(self, ctx) -> set:
        """Names the settings always show in full."""
        return set()

    def pinned(self, ctx) -> list:
        """Names the project keeps using (two of its last thirty conversations of this kind: main, or one sub-agent type): shown in full, snapshotted once a day."""
        if not self.KIND or ctx.state is None:
            return []
        from . import library  # library imports the layer that imports this module
        used = library.recently_used(ctx.memory_store, self.KIND, _project(ctx), agent_type=_sub_type(ctx))
        return sorted(name for name, count in used.items() if count >= 2)

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
        late = _frozen(ctx, self.KEY + "-late", lambda: not (found and found[1]))
        if late or not found or not found[1]:  # a listing the thread's first request lacked (a sub-agent's) stays as sent: editing it would rewrite the first message
            return
        block, (start, end, entries) = found
        catalog = dict(entries)
        index = self._index(entries)
        turns = rq.human_turns(messages)
        first = rq.human_text(messages[turns[0]]) if turns else ""

        def decide():
            keep = self.always_full(ctx) | set(_daily(ctx, self.KEY, lambda: self.pinned(ctx)))
            return sorted(keep & set(catalog))

        full = set(_frozen(ctx, self.KEY, decide))
        first_full = len(full)  # shown in full inside the listing; the count below also grows with later turns
        # What the first typed text matches goes next to it, so the listing is the same in every conversation.
        first_hits = _frozen(ctx, self.KEY + "-first",
                             lambda: [n for n, _ in index.search(first, limit=max_full) if n not in full])
        shown_text = [(name, desc if name in full else self.shorten(desc, chars)) for name, desc in entries]
        lines = [f"- {name}: {desc}" if desc else f"- {name}" for name, desc in shown_text]  # no description, no separator
        text = block["text"]
        note = self.NOTE_NAMES if chars <= 0 and self.NOTE_NAMES else self.NOTE
        listing = self.MARK + "\n" + note + self.GAP + "\n".join(lines)
        if len(listing) >= end - start and not self.ALWAYS_NOTE:
            return  # nothing to gain: leave the request alone
        block["text"] = text[:start] + listing + text[end:]
        saved = (end - start) - len(listing)

        # Every human turn: show the full description of collapsed entries it matches.
        shown, budget = set(full), EXPAND_LIMIT
        for at in turns:
            if at == turns[0]:
                hits = [n for n in first_hits if n in catalog and n not in shown]
            else:
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
    MARK, NOTE, KEY, KIND = SKILL_MARK, SKILL_NOTE, "skills", "skill"
    HEADER = "Full descriptions of skills that may be relevant to this message:"
    NOTE_NAMES = SKILL_NOTE_NAMES

    def settings(self, ctx):
        cfg = ctx.cfg["skills"]
        chars = ctx.cfg["startup"]["sub_skill_chars"] if ctx.thread == "sub" else cfg["stub_chars"]  # a sub-agent seldom needs a skill: names only, the text loads on a match
        return (chars, cfg["max_full"]) if cfg["enabled"] else None

    def always_full(self, ctx) -> set:
        return set(ctx.cfg["skills"]["always_full"])


class AgentsLayer(_ListingLayer):
    """The same for the agent listing, whose lines end in `(Tools: ...)`; a stubbed line drops that suffix."""

    MARK, NOTE, KEY, KIND, GAP = AGENT_MARK, AGENT_NOTE, "agents", "agent", "\n"
    HEADER = "Full descriptions of agent types that may be relevant to this message:"
    ALWAYS_NOTE = True  # the note also tells Claude to prefer a fitting specialist, so it must not depend on anything being cut

    def settings(self, ctx):
        chars = ctx.cfg["startup"]["agent_chars"]
        return (chars, ctx.cfg["skills"]["max_full"]) if chars > 0 else None

    def shorten(self, description: str, chars: int) -> str:
        cut = stub(_untooled(description), chars)
        return cut if cut.endswith("…") else description

    def searchable(self, description: str) -> str:
        return _untooled(description)


MCP_RUN = 8  # a server with at least this many deferred names is listed as one line


def _collapse_names(messages):
    """In a sub-agent's deferred-name list, one line per MCP server that has many names. ToolSearch still finds them by keyword.
    The same list gives the same text: it is a pure function of it."""
    for msg in messages[:8]:
        for block in rq.blocks(msg) if isinstance(msg.get("content"), list) else []:
            text = block.get("text") if block.get("type") == "text" else None
            if not text or DEFER_MARK not in text:
                continue
            at = text.find(DEFER_MARK)
            end = text.find("\n\n", at)
            end = len(text) if end < 0 else end
            head, _, body = text[at:end].partition(":\n")
            lines = body.split("\n") if body else []
            servers = {}
            for line in lines:
                m = re.match(r"mcp__([^_].*?)__", line)
                if m:
                    servers.setdefault(m.group(1), []).append(line)
            big = {k for k, v in servers.items() if len(v) >= MCP_RUN}
            if not big:
                return
            out, done = [], set()
            for line in lines:
                m = re.match(r"mcp__([^_].*?)__", line)
                if not m or m.group(1) not in big:
                    out.append(line)
                elif m.group(1) not in done:
                    done.add(m.group(1))
                    out.append(f"mcp__{m.group(1)}__* ({len(servers[m.group(1)])} tools; search ToolSearch by keyword to load)")
            block["text"] = text[:at] + head + ":\n" + "\n".join(out) + text[end:]
            return


class SubTtlLayer:
    """A sub-agent's stable prefix (tools and system prompt) keeps its cache for an hour, so a pause (waiting on other
    agents, a usage limit) does not make the next request write it again. Marks in the messages keep the client's ttl: a
    longer ttl must come before a shorter one, and these come first."""

    def transform(self, body, ctx):
        if ctx.thread != "sub" or ctx.cfg["startup"]["sub_ttl"] != "1h":
            return
        marks = [b["cache_control"] for part in ("tools", "system") if isinstance(body.get(part), list)
                 for b in body[part] if isinstance(b, dict) and isinstance(b.get("cache_control"), dict)]
        todo = [m for m in marks if m.get("ttl") != "1h"]
        for mark in todo:
            mark["ttl"] = "1h"
        if todo:
            ctx.stats["sub_ttl"] = len(todo)


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
        if ctx.thread == "sub":
            _collapse_names(body.get("messages") or [])
        cold_names = set(cfg["cold"]) | (set(ctx.cfg["startup"]["sub_cold"]) if ctx.thread == "sub" else set())
        cold = [t for t in tools if isinstance(t, dict) and t.get("name") in cold_names
                and t.get("name") not in cfg["hot"] and not t.get("defer_loading") and "type" not in t
                and not (t.get("name") == "PowerShell" and "Bash" not in names)]
        if not cold:
            return
        messages = body.get("messages") or []
        turns = rq.human_turns(messages)

        def promoted():
            from . import library  # library imports the layer that imports this module
            used = library.recently_used(ctx.memory_store, "tool", _project(ctx), agent_type=_sub_type(ctx)) if ctx.state is not None else {}
            return sorted(name for name, count in used.items() if count >= cfg["promote_after"])

        def decide():
            keep = set(_daily(ctx, "tools", promoted))  # a tool the message names is loaded by ToolSearch, not kept here
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


FORCE_NOTE = ("\n\nAlmanac: subagent_type is required and must be one of the listed specialist agents; "
              "pick the one whose job fits the task.")


class ForceAgentsLayer:
    """Make the sub-agent tool take only the listed specialist agent types, so the model cannot fall back to a generic one.

    The choice set comes from the agent listing Claude Code sends, minus `startup.force_agents_skip`. When nothing is left
    (no specialists installed), the tool is left alone, so sub-agents still work."""

    def transform(self, body, ctx):
        cfg = ctx.cfg["startup"]
        if not cfg.get("force_agents") or ctx.kind != "messages":
            return
        tools = [t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") in ("Agent", "Task")]
        props = tools and ((tools[0].get("input_schema") or {}).get("properties") or {})
        if not props or "subagent_type" not in props:
            return
        allowed = _frozen(ctx, "force_agents", lambda: self._allowed(body, cfg, tools[0]))  # the first request of a thread decides, so its tools never change
        if not allowed:
            return
        schema = tools[0]["input_schema"]
        schema["properties"]["subagent_type"] = {**props["subagent_type"], "enum": allowed}
        schema["required"] = list(dict.fromkeys([*(schema.get("required") or []), "subagent_type"]))
        if isinstance(tools[0].get("description"), str) and FORCE_NOTE not in tools[0]["description"]:
            tools[0]["description"] += FORCE_NOTE
        ctx.stats["force_agents"] = allowed

    @staticmethod
    def _allowed(body, cfg, tool) -> list:
        names = None
        for msg in (body.get("messages") or [])[:8]:
            for block in rq.blocks(msg) if isinstance(msg.get("content"), list) else []:
                if block.get("type") == "text" and AGENT_MARK in (block.get("text") or ""):
                    names = [n for n, _ in parse_listing(block["text"], AGENT_MARK)[2]]
                    break
            if names is not None:
                break
        skip = set(cfg.get("force_agents_skip") or [])
        allowed = [n for n in names or [] if n not in skip]
        if allowed and '"fork"' in (tool.get("description") or ""):  # forking yourself is not a generic fallback
            allowed.append("fork")
        return allowed
