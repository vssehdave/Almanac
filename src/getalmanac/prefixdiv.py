"""Observation only: where does a new conversation's prompt start to differ from the last one's?

The cache matches a request from its first byte: tools, then system blocks, then messages. On the
first request of each new main-thread conversation this lists those blocks as hashes (never text),
keeps the latest list per project, and logs the first block that differs from the previous one's.
The body is only read. Authorization is reduced to a short hash and never stored whole.
"""
import hashlib
import json
import time

from .compact import CHARS_PER_TOKEN
from .report import _spans


def _entry(label, value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return label, hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(), int(len(text) / CHARS_PER_TOKEN)


def _plain(block):
    return {k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block


BILLING = "x-anthropic-billing-header:"


def blocks(body) -> list:
    """(label, sha256, tokens) per block in prompt order: tools, system, first two messages."""
    out = [_entry(f"tool: {t.get('name')}", _plain(t)) for t in body.get("tools") or []
           if isinstance(t, dict) and not t.get("defer_loading")]
    system = body.get("system")
    for n, block in enumerate([{"type": "text", "text": system}] if isinstance(system, str) else system or []):
        if isinstance(block, dict) and str(block.get("text", "")).startswith(BILLING):
            continue  # changes per conversation, but the API does not count it for the prompt cache (measured 2026-11-17)
        out.append(_entry(f"system {n}", _plain(block)))
    for m, msg in enumerate((body.get("messages") or [])[:2]):
        content = msg.get("content") if isinstance(msg, dict) else None
        for n, block in enumerate([{"type": "text", "text": content}] if isinstance(content, str) else content or []):
            text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
            spans = _spans(text) if isinstance(text, str) else []
            if len(spans) > 1:  # one known section per entry, so the label names the section that moved
                out += [_entry(f"m{m}.{n} {label}", text[a:b]) for label, a, b in spans]
            else:
                out.append(_entry(f"m{m}.{n}" + (f" {spans[0][0]}" if spans else ""), _plain(block)))
    return out


def compare(old, new):
    """(label, index, tokens shared, tokens after) of the first block that differs, or ('none', ...)."""
    i = 0
    while i < min(len(old), len(new)) and old[i][1] == new[i][1]:
        i += 1
    label = "none" if i == len(old) == len(new) else (new[i][0] if i < len(new) else "end")
    return label, i, sum(b[2] for b in new[:i]), sum(b[2] for b in new[i:])


def account(headers) -> str:
    sent = {k.lower(): v for k, v in (headers or {}).items()}
    value = sent.get("authorization") or sent.get("x-api-key") or ""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12] if value else ""


class PrefixLayer:
    """Never changes the body. Placed after every rewriter so it sees the bytes that go upstream."""

    def transform(self, body, ctx):
        try:
            self._look(body, ctx)
        except Exception:  # observation must never cost a request its rewrite
            pass

    @staticmethod
    def _look(body, ctx):
        messages = body.get("messages")
        if ctx.kind != "messages" or ctx.store is None or not isinstance(messages, list):
            return
        if ctx.thread != "main" or any(isinstance(m, dict) and m.get("role") == "assistant" for m in messages):
            return
        project = ctx.project or ctx.cwd
        if not project:
            return
        now, key = time.time(), f"firstreq:{project}"
        now_blocks = blocks(body)
        with ctx.store.lock:
            prev = ctx.store.get(key)
            if prev and prev.get("lane") == ctx.lane:
                return  # the same conversation's first request sent again
            acct = account(ctx.headers)
            ctx.store.put(key, {"ts": now, "acct": acct, "lane": ctx.lane, "blocks": now_blocks})
        if not prev:
            return
        at, index, before, after = compare([tuple(b) for b in prev["blocks"]], now_blocks)
        ctx.stats["diverge" if "prefix" in ctx.stats else "prefix"] = {
            "diverge_at": at, "index": index, "tokens_before": before, "tokens_after": after,
            "since_project": round(now - prev["ts"]), "same_account": prev["acct"] == acct}
