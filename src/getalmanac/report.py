"""The request log, and the report that says where a session's context goes."""
import json
import os
import re
import threading
import time
from pathlib import Path

from . import config

LOG_LIMIT = 20 * 1024 * 1024

# Text Claude Code injects at session start, by the line each part opens with.
_MARKS = [
    (r"^[\w:.-]+ hook (?:success|additional context)", "hook output"),
    (r"^# Environment$", "environment"),
    (r"^The following tools just became available", "tool announcements"),
    (r"^The following deferred tools are now available", "deferred tool names"),
    (r"^The following MCP servers (?:require|are still)", "MCP status"),
    (r"^Available agent types for the Agent tool:", "agent types"),
    (r"^# MCP Server Instructions$", "MCP instructions"),
    (r"^The following skills are available for use with the Skill tool:", "skill listing"),
    (r"^Today's date is", "date"),
    (r"^# Advisor Tool$", "advisor notes"),
    (r"^Almanac memory for this project", "almanac memory"),
]
_MARK = re.compile("|".join(f"(?P<m{i}>{p})" for i, (p, _) in enumerate(_MARKS)), re.M)


def _text_parts(text: str):
    """Split injected text into (label, chars) by the markers above."""
    if "Codebase and user instructions are shown below" in text[:300]:
        return [("instructions (CLAUDE.md, memory index)", len(text))]
    if "you can use the following context" in text[:300]:
        return [("user context", len(text))]
    hits = [(m.start(), _MARKS[int(m.lastgroup[1:])][1]) for m in _MARK.finditer(text)]
    if not hits:
        return [("conversation", len(text))]
    parts = [("session context (other)", hits[0][0])] if hits[0][0] else []
    for i, (pos, label) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        parts.append((label, end - pos))
    return parts


def sections(body) -> dict:
    """Characters of model input per part of a request, in the order sent."""
    out = {}

    def add(label, n):
        if n:
            out[label] = out.get(label, 0) + n

    system = body.get("system")
    add("system prompt", len(system) if isinstance(system, str) else sum(len(json.dumps(b)) for b in system or []))
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and not tool.get("defer_loading"):
            add(f"tool: {tool.get('name')}", len(json.dumps(tool)))
    for index, msg in enumerate(body.get("messages") or []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            add("conversation", len(content))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and index < 4:
                for label, n in _text_parts(block.get("text") or ""):
                    add(label, n)
            elif block.get("type") == "tool_addition":
                add("tools added in session", len(json.dumps(block)))
            else:
                add("conversation", len(json.dumps(block)))
    return out


class Log:
    """Append-only JSON lines. Sizes and counts only: never bodies, never credentials."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config.private(self.path.parent, 0o700)
        self._lock = threading.Lock()

    def write(self, record: dict):
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > LOG_LIMIT:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
                    handle.write(line)
            except OSError:
                pass  # a full disk must not stop a request

    def tail(self, count=2000) -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-count:]
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
        return records


def _tokens(usage) -> int:
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int((usage or {}).get(k) or 0) for k in keys)


def startup(records):
    """The newest record that holds a session-start breakdown, or None."""
    for record in reversed(records):
        if record.get("sections") and _tokens(record.get("usage")):
            return record
    return None


def summary(records, now=None) -> dict:
    """Totals for the last day: requests, sessions and estimated tokens kept out."""
    now = now or time.time()
    day = [r for r in records if r.get("ts", 0) > now - 86400 and r.get("kind") == "messages"]
    saved = 0
    for record in day:
        sent, tokens = record.get("bytes_out") or 0, _tokens(record.get("usage"))
        if sent and tokens and record.get("bytes_in", 0) > sent:
            saved += int((record["bytes_in"] - sent) * tokens / sent)
    held = [r["held"] for r in day if r.get("held")]
    return {"requests": len(day), "sessions": len({r.get("session") for r in day}), "saved_tokens": saved,
            "held": len(held), "held_s": sum(held)}


def routing(records) -> dict:
    """What model routing did in the log: requests per tier, moves made and declined, raises, tokens per model."""
    out = {"tiers": {}, "moves": {}, "raised": 0, "models": {}, "unrouted": 0}
    for r in records:
        if r.get("kind") != "messages":
            continue
        note = r.get("route") or r.get("route_kept")
        model = r.get("answered") or (note or {}).get("model") or r.get("model") or "unknown"
        tokens = _tokens(r.get("usage")) + int((r.get("usage") or {}).get("output_tokens") or 0)
        if tokens:
            out["models"][model] = out["models"].get(model, 0) + tokens
        if not note:
            out["unrouted"] += 1
            continue
        out["tiers"][note["tier"]] = out["tiers"].get(note["tier"], 0) + 1
        if note.get("first"):
            out["moves"][note["move"]] = out["moves"].get(note["move"], 0) + 1
            out["raised"] += bool(note.get("escalated"))
    return out


def render_routing(records) -> str:
    """Lines for `almanac report`, or "" when routing never ran."""
    r = routing(records)
    if not r["tiers"]:
        return ""
    lines = ["", "Model routing, from the request log:",
             "  requests   " + ", ".join(f"{t} {r['tiers'].get(t, 0)}" for t in ("small", "medium", "large"))
             + (f" ({r['unrouted']} not routed)" if r["unrouted"] else ""),
             "  moves      " + (", ".join(f"{k} {v}" for k, v in sorted(r["moves"].items())) or "none")
             + f"; raised after failures: {r['raised']}",
             "  tokens     " + ", ".join(f"{m} {n:,}" for m, n in sorted(r["models"].items(), key=lambda kv: -kv[1]))]
    return "\n".join(lines)


def render(records) -> str:
    record = startup(records)
    if not record:
        return "No session start recorded yet. Run `almanac` once, send a message, then run this again."
    now, was = record["sections"], record.get("sections_before") or record["sections"]
    total = _tokens(record["usage"])
    ratio = total / max(1, sum(now.values()))  # tokens per character, from the exact total
    before = int(sum(was.values()) * ratio)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("ts", 0)))
    lines = [f"Latest session start: {when}, {record.get('model') or 'unknown model'}", ""]
    lines.append(f"  {total:,} tokens sent" + (f" (about {before:,} without Almanac, {before - total:,} kept out)" if before > total else ""))
    lines += ["", f"  {'part':<44}{'tokens':>9}{'without':>10}"]
    labels = sorted(set(now) | set(was), key=lambda k: -max(now.get(k, 0), was.get(k, 0)))
    for label in labels[:30]:
        a, b = int(now.get(label, 0) * ratio), int(was.get(label, 0) * ratio)
        lines.append(f"  {label[:43]:<44}{a:>9,}{(f'{b:,}' if b != a else ''):>10}")
    if len(labels) > 30:
        rest = labels[30:]
        lines.append(f"  {'(' + str(len(rest)) + ' smaller parts)':<44}{int(sum(now.get(k, 0) for k in rest) * ratio):>9,}")
    totals = summary(records)
    lines += ["", f"Last 24 hours: {totals['requests']} request(s) in {totals['sessions']} session(s), "
              f"about {totals['saved_tokens']:,} input tokens kept out."]
    if totals["held"]:
        lines.append(f"{totals['held']} request(s) waited out a network outage, {totals['held_s']:,} seconds in all.")
    lines.append("Per-part figures are the exact total split by size, so treat them as estimates.")
    return "\n".join(lines) + render_routing(records)
