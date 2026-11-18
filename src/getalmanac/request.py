"""Reading a Claude Code request body: turns, lanes and the working directory."""
import hashlib
import json
import re

REMINDER = "<system-reminder>"
SKILL_BODY = "Base directory for this skill:"  # Claude Code inserts a skill's text after a typed slash command
COMMAND = ("<command-message>", "<command-name>")
_CWD = re.compile(r"^\s*-?\s*Primary working directory: (.+)$", re.M)


def blocks(msg) -> list:
    """A message's content as a list of blocks (a bare string becomes one text block)."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def has_client_tools(body) -> bool:
    """True when the request carries a tool the client runs. Claude Code's own turns do; a tool's inner model call (web search) does not."""
    return any(isinstance(t, dict) and "input_schema" in t for t in body.get("tools") or [])


def is_tool_result(msg) -> bool:
    return msg.get("role") == "user" and any(b.get("type") == "tool_result" for b in blocks(msg))


def typed_parts(msg) -> list:
    """The text blocks of a user message a person could have typed: not reminders, not a skill's body, and nothing
    after a slash command's own blocks (that is skill text Claude Code inserted: third-party words)."""
    if msg.get("role") != "user" or is_tool_result(msg):
        return []
    out, after_command = [], False
    for b in blocks(msg):
        if b.get("type") != "text":
            continue
        text = b.get("text") or ""
        head = text.lstrip()
        if head.startswith(COMMAND):
            out.append(text)
            after_command = True
        elif text.strip() and not after_command and not head.startswith((REMINDER, SKILL_BODY)):
            out.append(text)
    return out


def human_text(msg) -> str:
    """What the person typed in a user message: its text minus injected reminders and skill text."""
    return "\n".join(p for p in typed_parts(msg) if p.strip()).strip()


def human_turns(messages) -> list:
    """Indexes of the messages a person typed."""
    return [i for i, m in enumerate(messages) if isinstance(m, dict) and human_text(m)]


def message_text(msg) -> str:
    """Plain text of any message, for indexing: text, tool calls and tool results."""
    out = []
    for b in blocks(msg):
        kind = b.get("type")
        if kind == "text":
            text = b.get("text") or ""
            if not text.lstrip().startswith(REMINDER):
                out.append(text)
        elif kind == "tool_use":
            out.append(f"[tool {b.get('name')}] {json.dumps(b.get('input'), ensure_ascii=False)[:600]}")
        elif kind == "tool_result":
            inner = b.get("content")
            if isinstance(inner, list):
                inner = "\n".join(x.get("text") or "" for x in inner if isinstance(x, dict) and x.get("type") == "text")
            out.append(f"[result] {str(inner or '')[:1200]}")
    return "\n".join(o for o in out if o.strip())


def lane_key(session: str, agent: str, messages) -> str:
    """One conversation thread: the session, the subagent if any, and its first human text.

    Decisions frozen for a conversation are keyed by this, so the main thread and
    each subagent keep their own, and `/clear` starts a new one.
    """
    turns = human_turns(messages)
    first = human_text(messages[turns[0]]) if turns else ""
    return hashlib.sha256(f"{session}\0{agent}\0{first}".encode("utf-8", "surrogatepass")).hexdigest()[:20]


def working_dir(body) -> str:
    """The session's working directory as Claude Code states it, or ""."""
    texts = []
    system = body.get("system")
    if isinstance(system, str):
        texts.append(system)
    for b in system if isinstance(system, list) else []:
        if isinstance(b, dict):
            texts.append(b.get("text") or "")
    for msg in (body.get("messages") or [])[:4]:
        for b in blocks(msg):
            if b.get("type") == "text":
                texts.append(b.get("text") or "")
    for text in texts:
        found = _CWD.search(text)
        if found:
            return found.group(1).strip()
    return ""


def dumps(body) -> bytes:
    """Serialise a rewritten body. Deterministic: the same body gives the same bytes."""
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate: escape everything instead
        return json.dumps(body, separators=(",", ":")).encode("ascii")
