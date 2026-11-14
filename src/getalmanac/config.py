"""Where Almanac keeps its files, and its settings."""
import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "upstream": "https://api.anthropic.com",
    "skills": {
        "enabled": True,
        "stub_chars": 90,  # characters of description kept for a collapsed skill
        "max_full": 6,  # most skills shown in full per message on a match
        "always_full": [],  # skill names never collapsed
    },
    "tools": {
        "enabled": True,
        # deferred unless the conversation or the project's history calls for them
        "cold": ["Artifact", "ListAgents", "PowerShell", "ReportFindings", "ScheduleWakeup", "SendFeedback"],
        "hot": [],  # tool names never deferred
        "promote_after": 2,  # conversations in the last 10 that used a cold tool before it stays loaded
    },
    "memory": {
        "enabled": True,
        "recall": True,
        "recall_chars": 2400,  # most characters of memory added to a new conversation
        "tools": True,  # give Claude Code the memory_search / memory_read tools
    },
    "compaction": {
        "enabled": True,
        "trigger_tokens": 150000,  # a conversation this large is compacted at its next message
        "target_tokens": 40000,  # most tokens the summary that replaces it may take
    },
}


def home() -> Path:
    path = Path(os.environ.get("ALMANAC_HOME") or Path.home() / ".almanac")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home() / "config.json"


def _merge(base, over):
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _file() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    cfg = _merge(copy.deepcopy(DEFAULTS), _file())
    if os.environ.get("ALMANAC_UPSTREAM"):
        cfg["upstream"] = os.environ["ALMANAC_UPSTREAM"]
    # ALMANAC_DISABLE=skills,tools,memory,compaction turns layers off for one run
    for name in filter(None, (os.environ.get("ALMANAC_DISABLE") or "").replace(" ", "").split(",")):
        if name == "all":
            for layer in ("skills", "tools", "memory", "compaction"):
                cfg[layer]["enabled"] = False
        elif isinstance(cfg.get(name), dict):
            cfg[name]["enabled"] = False
    return cfg


def set_value(dotted: str, raw: str) -> object:
    """Set `a.b` in config.json. The value is JSON when it parses, else a string."""
    node = DEFAULTS
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    if type(value) is not type(node) and not (isinstance(node, (int, float)) and isinstance(value, (int, float))):
        raise TypeError(f"{dotted} expects {type(node).__name__}")
    data = _file()
    target = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    config_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return value
