"""The request log, and the report that says where a session's context goes."""
import json
import os
import re
import threading
import time
from pathlib import Path

from . import config
from .cheatsheet import family

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


def _spans(text: str):
    """Split injected text into (label, start, end) by the markers above."""
    if "Codebase and user instructions are shown below" in text[:300]:
        return [("instructions (CLAUDE.md, memory index)", 0, len(text))]
    if "you can use the following context" in text[:300]:
        return [("user context", 0, len(text))]
    hits = [(m.start(), _MARKS[int(m.lastgroup[1:])][1]) for m in _MARK.finditer(text)]
    if not hits:
        return [("conversation", 0, len(text))]
    spans = [("session context (other)", 0, hits[0][0])] if hits[0][0] else []
    for i, (pos, label) in enumerate(hits):
        spans.append((label, pos, hits[i + 1][0] if i + 1 < len(hits) else len(text)))
    return spans


def _text_parts(text: str):
    """The same cut as (label, chars)."""
    return [(label, end - start) for label, start, end in _spans(text)]


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


MAX_AGENTS = 4
AUDIT = re.compile(r"audit|plan|review", re.I)
BUILD = re.compile(r"build|implement|write the code", re.I)
TTL_SECONDS = {"5m": 300, "1h": 3600}
USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def is_cold(idle, ttls) -> bool:
    """True when a pause outlasted the shortest cache life the request asked for."""
    return bool(ttls) and isinstance(idle, (int, float)) and idle > min(TTL_SECONDS.get(t, 300) for t in ttls)


def cache_marks(body) -> dict:
    """The cache breakpoints in a request as the client sent it, and the ttl of each in order."""
    ttl = []

    def walk(blocks):
        for block in blocks if isinstance(blocks, list) else []:
            mark = block.get("cache_control") if isinstance(block, dict) else None
            if isinstance(mark, dict):
                ttl.append("1h" if mark.get("ttl") == "1h" else str(mark.get("ttl") or "5m"))

    walk(body.get("system"))
    walk(body.get("tools"))
    for msg in body.get("messages") or []:
        walk(msg.get("content") if isinstance(msg, dict) else None)
    return {"marks": len(ttl), "ttl": ttl}


def meter(records, now=None) -> dict:
    """Usage over 5 hours and 24 hours, and how often a pause outlasted the cache's life."""
    now = now or time.time()
    keys = tuple(USAGE)
    out = {"5h": None, "24h": None}
    seen = {}
    for name, span in (("5h", 5 * 3600), ("24h", 86400)):
        total = {"requests": 0, **dict.fromkeys(keys, 0), "cold_returns": 0, "cold_write_tokens": 0,
                 "cold_compacted": 0, "cold_saved_tokens": 0, "keepalive_pings": 0, "keepalive_read_tokens": 0}
        for r in records:
            if not isinstance(r, dict) or r.get("kind") not in ("messages", "keepalive") or not isinstance(r.get("ts"), (int, float)) or r["ts"] <= now - span:
                continue
            usage = r.get("usage")
            if not isinstance(usage, dict):
                continue
            for k in keys:  # a keepalive ping is real usage, so its tokens count here, but it is not a request
                n = usage.get(k)
                total[k] += n if isinstance(n, int) else 0
            if r["kind"] == "keepalive":
                total["keepalive_pings"] += 1
                n = usage.get("cache_read_input_tokens")
                total["keepalive_read_tokens"] += n if isinstance(n, int) else 0
                continue
            total["requests"] += 1
            c = r.get("cache")
            ttls = [t for t in c.get("ttl") or [] if isinstance(t, str)] if isinstance(c, dict) else []
            if name == "24h":
                for t in ttls:
                    seen[t] = seen.get(t, 0) + 1
            if is_cold(r.get("idle"), ttls):
                total["cold_returns"] += 1
                n = usage.get("cache_creation_input_tokens")
                total["cold_write_tokens"] += n if isinstance(n, int) else 0
            note = r.get("cold")
            if isinstance(note, dict) and isinstance(note.get("before"), int) and isinstance(note.get("after"), int):
                total["cold_compacted"] += 1
                total["cold_saved_tokens"] += max(0, note["before"] - note["after"])
        base = total["input_tokens"] + total["cache_creation_input_tokens"]
        total["cold_share"] = round(total["cold_write_tokens"] / base, 3) if base else None
        out[name] = total
    day = out["24h"]
    side = sum(day[k] for k in keys[:3])
    out["cache"] = {"read_share": round(day["cache_read_input_tokens"] / side, 3) if side else None,
                    "cold_returns": day["cold_returns"], "cold_write_tokens": day["cold_write_tokens"],
                    "cold_share": day["cold_share"], "cold_compacted": day["cold_compacted"],
                    "cold_saved_tokens": day["cold_saved_tokens"], "ttl_seen": seen}
    return out


def render_meter(records, now=None) -> list:
    """The `almanac status` lines: raw token counts per period, no price weighting (a plan counts usage its own way)."""
    m = meter(records, now)

    def lines(label, t):
        share = "no input yet" if t["cold_share"] is None else f"{t['cold_share']:.1%} of cache writes + input"
        out = [f"  {label:<10} {t['requests']} requests, input {t['input_tokens']:,}, cache write "
                f"{t['cache_creation_input_tokens']:,}, cache read {t['cache_read_input_tokens']:,}, output {t['output_tokens']:,}",
                f"  {'cold':<10} {t['cold_returns']} returns wrote {t['cold_write_tokens']:,} cache tokens ({share}); "
                f"{t['cold_compacted']} compacted, {t['cold_saved_tokens']:,} tokens saved"]
        if t["keepalive_pings"]:  # counted in the totals above; shown apart so its cost and effect are visible
            out.append(f"  {'keepalive':<10} {t['keepalive_pings']} pings, cache read {t['keepalive_read_tokens']:,} (included above)")
        return out

    c = m["cache"]
    share = "no usage yet" if c["read_share"] is None else f"{c['read_share']:.0%} of input read from cache"
    seen = ", ".join(f"{k} {v}" for k, v in sorted(c["ttl_seen"].items())) or "none seen"
    return [*lines("usage 5h", m["5h"]), *lines("usage 24h", m["24h"]), f"  {'cache':<10} {share}; ttl seen: {seen}"]


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


def agents(records) -> dict:
    """The sub-agent threads of the latest main conversation, one entry per lane, in the order they started."""
    msgs = [r for r in records if r.get("kind") == "messages" and r.get("session")]
    main = [r for r in msgs if not r.get("agent")]
    if not main:
        return {"threads": [], "peak": 0, "building": []}
    session = main[-1]["session"]
    lanes = {}
    for r in msgs:
        if r["session"] == session and r.get("agent") and r.get("lane"):
            lanes.setdefault(r["lane"], []).append(r)
    threads = []
    for rs in lanes.values():
        rs.sort(key=lambda r: r.get("ts") or 0)
        usage = [r.get("usage") or {} for r in rs]
        threads.append({"agent": next((r["agent_type"] for r in rs if r.get("agent_type")), "") or "unknown",
                        "id": rs[0]["agent"], "model": rs[0].get("answered") or rs[0].get("model") or "unknown", "requests": len(rs),
                        "input": sum(_tokens(u) for u in usage), "output": sum(int(u.get("output_tokens") or 0) for u in usage),
                        "task": next((r["task"] for r in rs if r.get("task")), ""), "start": rs[0].get("ts") or 0, "end": rs[-1].get("ts") or 0})
    threads.sort(key=lambda t: t["start"])
    now = peak = 0
    for _, step in sorted([(t["start"], 1) for t in threads] + [(t["end"], -1) for t in threads], key=lambda e: (e[0], -e[1])):
        now += step
        peak = max(peak, now)
    # shortcut: a build job is spotted by words in the first task text; a task worded differently is missed
    building = [t["agent"] for t in threads if family(t["model"]) == "opus" and not AUDIT.search(t["agent"]) and BUILD.search(t["task"])]
    return {"threads": threads, "peak": peak, "building": building}


def render_agents(records) -> str:
    """Lines for `almanac report`, or "" when the latest conversation ran no sub-agent."""
    a = agents(records)
    if not a["threads"]:
        return ""
    lines = ["", "Agents in the latest session:"]
    for t in a["threads"]:
        lines.append(f"  {t['agent'][:24]:<24} {t['model']:<24} {t['requests']:>3} req, in {t['input']:>9,}, out {t['output']:>7,}"
                     + (f"  {t['task']}" if t["task"] else ""))
    if a["peak"] > MAX_AGENTS:
        lines.append(f"Warning: {a['peak']} sub-agent threads were active at the same time (limit {MAX_AGENTS}).")
    if a["building"]:
        lines.append("Warning: Opus used for building: " + ", ".join(dict.fromkeys(a["building"])))
    return "\n".join(lines)


COLD_SECONDS = 3600  # a new conversation this long after the project's last one has lost its cache


def prefix_line(records, now=None) -> str:
    """One line on where new conversations' cache prefixes broke from the project's previous one, last 24 hours."""
    now = now or time.time()
    seen = [v for r in records if r.get("ts", 0) > now - 86400
            for v in [r.get("diverge") or r.get("prefix")] if isinstance(v, dict) and "diverge_at" in v]
    if not seen:
        return ""
    cold = [v for v in seen if (v.get("since_project") or 0) > COLD_SECONDS or not v.get("same_account")]
    warm = [v for v in seen if v not in cold]
    places = {}
    for v in warm:
        if v["diverge_at"] != "none":
            places[v["diverge_at"]] = places.get(v["diverge_at"], 0) + 1
    top = ", ".join(f"{k} x{n}" for k, n in sorted(places.items(), key=lambda kv: -kv[1])[:3])
    return (f"Last 24 hours: {len(seen)} new conversation(s) with an earlier one in the project: "
            f"{sum(places.values())} diverged" + (f" ({top})" if top else "") +
            f", {len(cold)} cold (over an hour apart or another account), {len(warm) - sum(places.values())} identical.")


def library_line(records, now=None) -> str:
    """One line on the prompt library, last 24 hours: entries the writer wrote and notes shown to the model."""
    now = now or time.time()
    day = [r for r in records if isinstance(r.get("ts"), (int, float)) and r["ts"] > now - 86400]
    written = sum(r["written"] for r in day if r.get("kind") == "library" and isinstance(r.get("written"), int))
    shown = sum(1 for r in day if isinstance(r.get("library"), dict))
    return (f"Prompt library, last 24 hours: {written} entr{'y' if written == 1 else 'ies'} written, "
            f"{shown} note{'' if shown == 1 else 's'} shown.")


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
    if prefix_line(records):
        lines.append(prefix_line(records))
    lines.append(library_line(records))
    lines.append("Per-part figures are the exact total split by size, so treat them as estimates.")
    return "\n".join(lines) + render_routing(records) + render_agents(records)
