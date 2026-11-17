"""Put the blocks that are the same in every session of a project first in a conversation's first message.

Claude Code puts text that changes from session to session (hook output, CLAUDE.md,
the typed message) in front of text that does not (MCP instructions, agent types, the
skill listing, deferred tool names, advisor notes). The prompt cache matches a prefix,
so everything after the first changed byte is written again. This layer moves the
stable blocks, byte for byte, to the front of the first user message and puts a cache
breakpoint after them, so a new conversation can read them from the cache.

The order is decided once per conversation and frozen. A later request whose first
message no longer holds exactly the same blocks (compaction, a rewind) is left alone.
"""
import hashlib

from . import request as rq
from .report import _MARK, _MARKS
from .routing import RoutingLayer

# Fixed order, most stable first. A listing whose text was chosen from the typed message
# (descriptions shown in full) differs between conversations, so it goes after the breakpoint.
ORDER = ["MCP instructions", "advisor notes", "deferred tool names", "agent types", "skill listing"]
LISTING_KEY = {"agent types": "agents", "skill listing": "skills"}
MAX_MARKS = 4


def _label(block):
    """The stable label of a block that holds exactly one marked section and nothing else, or None."""
    text = block.get("text") if block.get("type") == "text" else None
    if not isinstance(text, str) or "cache_control" in block:
        return None
    hits = list(_MARK.finditer(text))
    if len(hits) != 1 or text[:hits[0].start()].strip() not in ("", "<system-reminder>"):
        return None
    label = _MARKS[int(hits[0].lastgroup[1:])][1]
    return label if label in ORDER else None


def _sha(block) -> str:
    return hashlib.sha256(block["text"].encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _marks(body, upto=None):
    """cache_control dicts in prompt order (tools, system, messages), those of messages before `upto` only when given."""
    msgs = body.get("messages") or []
    groups = [body.get("tools"), body.get("system")] + [m.get("content") for m in (msgs if upto is None else msgs[:upto]) if isinstance(m, dict)]
    return [b["cache_control"] for g in groups for b in (g if isinstance(g, list) else [])
            if isinstance(b, dict) and isinstance(b.get("cache_control"), dict)]


def _ttl(body, at, stable):
    """The cache_control for our breakpoint: the nearest earlier one's (a 1h mark may not follow a 5m one), else the first later one's."""
    if not stable:
        return None
    before, every = _marks(body, at), _marks(body)
    peer = before[-1] if before else (every[0] if every else None)
    return {k: v for k, v in peer.items() if k in ("type", "ttl")} if peer else None


class PrefixLayer:
    def transform(self, body, ctx):
        cfg = ctx.cfg["startup"]
        if not cfg.get("stable_first") or ctx.kind != "messages" or ctx.store is None:
            return
        if RoutingLayer._kind(ctx, body) != "main":
            return
        messages = body.get("messages")
        at = next((i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
        if at is None or not isinstance(messages[at].get("content"), list):
            return
        content = messages[at]["content"]
        found = {}  # label -> index in content
        for i, block in enumerate(content):
            label = _label(block) if isinstance(block, dict) else None
            if label:
                if label in found:
                    return  # the same section twice: not the shape this layer knows
                found[label] = i
        if not found:
            return
        now = sorted((label, _sha(content[i])) for label, i in found.items())

        def decide():
            volatile = {label for label, key in LISTING_KEY.items() if (ctx.stats.get(key) or {}).get("full")}
            order = [label for label in ORDER if label in found and label not in volatile]
            stable = len(order)
            order += [label for label in ORDER if label in found and label in volatile]
            return {"blocks": now, "order": order, "stable": stable, "ttl": _ttl(body, at, stable)}

        plan = ctx.store.frozen(f"prefix:{ctx.lane}", decide)
        if [tuple(x) for x in plan["blocks"]] != now:  # compaction or a rewind changed the blocks: leave it
            return
        head = [content[found[label]] for label in plan["order"]]
        rest = [b for i, b in enumerate(content) if i not in found.values()]
        moved = sum(1 for k, block in enumerate(head) if content[k] is not block)
        mark = bool(plan["ttl"]) and len(_marks(body)) < MAX_MARKS  # never more than 4 breakpoints in all
        if mark:
            k = plan["stable"] - 1
            head[k] = dict(head[k], cache_control=plan["ttl"])
        if not moved and not mark:
            return
        messages[at]["content"] = head + rest
        ctx.stats["prefix"] = {"moved": moved, "chars": sum(len(b["text"]) for b in head), "mark": mark}
