"""Rebuild the assistant message and its usage from a Messages API response."""
import json


def parse(raw: bytes):
    """Return {"content": [...], "usage": {...}, "stop_reason": ..., "model": ...} or None.

    Accepts a server-sent event stream or a plain JSON message.
    """
    head = raw.lstrip()[:1]
    if head == b"{":
        try:
            msg = json.loads(raw)
        except ValueError:
            return None
        if msg.get("type") != "message":
            return None
        return {"content": msg.get("content") or [], "usage": msg.get("usage") or {}, "stop_reason": msg.get("stop_reason"),
                "model": str(msg.get("model") or "")}
    usage, stop, parts, seen, model = {}, None, {}, False, ""
    for line in raw.split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "message_start":
            seen = True
            usage.update((event.get("message") or {}).get("usage") or {})
            model = str((event.get("message") or {}).get("model") or "")
        elif kind == "content_block_start":
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["_json"] = ""
            parts[event.get("index")] = block
        elif kind == "content_block_delta":
            block, delta = parts.get(event.get("index")), event.get("delta") or {}
            if block is None:
                continue
            if delta.get("type") == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                block["_json"] = block.get("_json", "") + (delta.get("partial_json") or "")
        elif kind == "message_delta":
            usage.update(event.get("usage") or {})
            stop = (event.get("delta") or {}).get("stop_reason") or stop
    if not seen:
        return None
    content = []
    for index in sorted(parts):
        block = parts[index]
        if "_json" in block:
            try:
                block["input"] = json.loads(block.pop("_json") or "{}")
            except ValueError:
                block["input"] = {}
        content.append(block)
    return {"content": content, "usage": usage, "stop_reason": stop, "model": model}


def input_tokens(usage) -> int:
    """Every token the model read for this request, cached or not."""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)
