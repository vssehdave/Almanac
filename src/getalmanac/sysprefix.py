"""Move the stable startup sections to the end of the system prompt.

Claude Code puts them in a session message that follows the typed text, so a new
conversation never finds them in cache. As the last system block they sit before
the typed text and are read from cache.

Determinism: the result is a pure function of the request's own bytes and the
skill and agent listings' first_full counts (fixed by the first typed text). No
state is kept, so every request of a conversation, a resume and a fresh layer
cut the same way."""
import copy
import re

from .report import _spans
from .routing import RoutingLayer

ORDER = ["MCP instructions", "advisor notes", "deferred tool names", "agent types", "skill listing"]
_UNKNOWN = re.compile(r"\n\n(?=# \S)")  # a top-level heading inside a span: a section no marker names, so it stays
LOOK = 4  # messages searched, as the report does


def _cuts(text, listings):
    """[(label, start, end)] of the movable sections in one text block."""
    out = []
    for label, start, end in _spans(text):
        if label not in ORDER or (listings.get(label) or 0) > 0:
            continue
        hit = None if label == "MCP instructions" else _UNKNOWN.search(text, start + 1, end)  # server text has its own # headings
        out.append((label, start, hit.end() if hit else end))
    return out


class SysPrefixLayer:
    def transform(self, body, ctx):
        if ctx.cfg["startup"].get("system_prefix") is not True or ctx.kind != "messages":
            return
        if RoutingLayer._kind(ctx, body) != "main":
            return
        system, messages = body.get("system"), body.get("messages")
        if not isinstance(system, list) or not system or not isinstance(system[-1], dict) or not isinstance(messages, list):
            return
        listings = {"skill listing": (ctx.stats.get("skills") or {}).get("first_full"),
                    "agent types": (ctx.stats.get("agents") or {}).get("first_full")}
        for msg in messages[:LOOK]:
            content = msg.get("content") if isinstance(msg, dict) and msg.get("role") == "system" else None  # a user block may quote typed text
            if not isinstance(content, list):
                continue
            for at, block in enumerate(content):
                text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
                if not isinstance(text, str):
                    continue
                cuts = _cuts(text, listings)
                if cuts:
                    self._move(body, ctx, content, at, text, cuts)
                    return

    @staticmethod
    def _move(body, ctx, content, at, text, cuts):
        kept, last = [], 0
        for _, start, end in cuts:
            kept.append(text[last:start])
            last = end
        rest = "".join(kept) + text[last:]
        if not rest.strip() and len(content) == 1:
            return  # the message would be left with nothing in it
        ordered = sorted(cuts, key=lambda c: (ORDER.index(c[0]), c[1]))
        moved = "\n\n".join(text[s:e].rstrip("\n") for _, s, e in ordered)
        new = {"type": "text", "text": moved}
        old = body["system"][-1]
        if "cache_control" in old:
            new["cache_control"] = copy.deepcopy(old["cache_control"])
            body["system"][-1] = {k: v for k, v in old.items() if k != "cache_control"}
        body["system"].append(new)
        if rest.strip():
            content[at] = dict(content[at], text=rest)
        else:
            del content[at]
        ctx.stats["sysprefix"] = {"moved": [c[0] for c in ordered], "chars": len(moved)}
