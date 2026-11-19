"""Second line of the clean profile: whatever of an off plugin or skill still reaches a request is cut out.

The launcher keeps off plugins and hooks from loading at all (profile.py). This layer covers sessions it did not
start (`almanac serve` with a plain `claude`), and anything a project's own settings switched back on. It only
removes, and decides from the request alone, so a conversation is rewritten to the same bytes every time.
"""
import re

from . import request as rq
from .profile import off_names
from .startup import AGENT_MARK, SKILL_MARK, parse_listing

_HOOK = re.compile(r"(?:\w+:)?\w+ hook (?:additional context|success): ")
# shortcut: hook text has no closing mark, so it is cut at the next thing Claude Code writes after it. If a
# future version adds a new section type, that section is cut with the hook text until one of these ends it.
_END = re.compile(r"\n\n(?=# Environment|The following (?:skills|tools|deferred|MCP)|You are powered|<system-reminder>)"
                  r"|\n(?=\w+(?::\w+)? hook (?:success|additional context))|</system-reminder>")


def _off(name: str, names) -> bool:
    return any(name == n or name.startswith(n + ":") for n in names)


def _cut_hooks(text: str, names, markers=()) -> str:
    """The text without the hook output that mentions an off plugin (its first 400 characters decide) or opens with a marker (its first line)."""
    out, at = [], 0
    for m in _HOOK.finditer(text):
        if m.start() < at:
            continue
        end = _END.search(text, m.end())
        stop = end.start() if end else len(text)
        head = text[m.end():min(m.end() + 400, stop)].lower()
        first = next((line for line in head.splitlines() if line.strip()), "")
        if any(n in head for n in names) or any(k.lower() in first for k in markers) or "<logbook-notes>" in head:
            if "<logbook-notes>" in head and "</logbook-notes>" in text[m.end():]:
                stop = text.index("</logbook-notes>", m.end()) + len("</logbook-notes>")
            out.append(text[at:m.start()])
            at = stop
    out.append(text[at:])
    return "".join(out)


def _cut_listing(text: str, mark: str, names) -> str:
    found = parse_listing(text, mark)
    if not found:
        return text
    start, end, entries = found
    kept = [(n, d) for n, d in entries if not _off(n, names)]
    if len(kept) == len(entries):
        return text
    body = text[start:end]
    first = body.find("\n- ")
    head = body[:first + 1] if first >= 0 else body
    return text[:start] + head + "\n".join(f"- {n}: {d}" for n, d in kept) + text[end:]


def _cut_lines(text: str, prefixes) -> str:
    return "".join(line for line in text.splitlines(True) if not line.startswith(prefixes))


def _cut_instructions(text: str, names) -> str:
    """MCP server instructions: the `## plugin:<name>:...` sections of off plugins."""
    for n in names:
        text = re.sub(rf"(?ms)^## plugin:{re.escape(n)}:.*?(?=^## |\Z)", "", text)
    return text


class StripLayer:
    def transform(self, body, ctx):
        if not ctx.cfg["profile"]["enabled"]:
            return
        names = off_names(ctx.cfg)
        if not names:
            return
        tool_prefix = tuple(f"mcp__plugin_{n}_" for n in names)
        markers = tuple(ctx.cfg["profile"].get("off_hook_text") or ())
        if isinstance(body.get("tools"), list):
            body["tools"] = [t for t in body["tools"]
                             if not (isinstance(t, dict) and str(t.get("name", "")).startswith(tool_prefix))]
        system = body.get("system")
        for block in system if isinstance(system, list) else []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block["text"] = _cut_instructions(block["text"], names)
        if isinstance(system, str):
            body["system"] = _cut_instructions(system, names)
        for msg in body.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            if isinstance(msg.get("content"), str):
                msg["content"] = self._text(msg["content"], names, tool_prefix, markers)
                continue
            kept = []
            for block in rq.blocks(msg):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    block["text"] = self._text(block["text"], names, tool_prefix, markers)
                elif block.get("type") == "tool_addition":
                    tool = block.get("tool") or {}
                    name = (tool.get("definition") or {}).get("name") or tool.get("name") or ""
                    if str(name).startswith(tool_prefix):
                        continue
                kept.append(block)
            if isinstance(msg.get("content"), list):
                msg["content"] = kept

    @staticmethod
    def _text(text, names, tool_prefix, markers):
        text = _cut_hooks(text, names, markers)
        text = _cut_listing(text, SKILL_MARK, names)
        text = _cut_listing(text, AGENT_MARK, names)
        return _cut_lines(text, tool_prefix) if "mcp__plugin_" in text else text
