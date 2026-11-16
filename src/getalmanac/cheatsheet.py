"""Per-model working rules, appended to the system prompt of every conversation thread.

The text is chosen by the model the request will really be sent with, so this
layer runs after routing. It is read once per process and appended in the same
place every time, so a conversation's system prompt keeps the same bytes and
the prompt cache keeps hitting. No cache breakpoint is added, moved or removed.
"""
import functools
from importlib import resources

from . import config
from .routing import RoutingLayer

FAMILIES = ("opus", "sonnet", "haiku", "fable")
NAMES = (*FAMILIES, "common", "skills")
ALIASES = {"mythos": "fable"}  # the same model under another name


def override_dir():
    return config.home() / "cheatsheets"


@functools.lru_cache(maxsize=None)
def read(name: str) -> str:
    """One cheatsheet file: the user's copy under ALMANAC_HOME if there is one, else the packaged one."""
    mine = override_dir() / f"{name}.md"
    if mine.is_file():
        return mine.read_text(encoding="utf-8").rstrip()
    return resources.files("getalmanac").joinpath("cheatsheets", f"{name}.md").read_text(encoding="utf-8").rstrip()


def family(model: str) -> str:
    low = model.lower()
    return next((f for f in FAMILIES if f in low), "") or next((v for k, v in ALIASES.items() if k in low), "")


def text_for(model: str) -> str:
    """What is appended for this model, or "" for a model with no cheatsheet."""
    name = family(model)
    if not name:
        return ""
    if name == "haiku":  # short on purpose: its own file is the whole text
        return read(name)
    parts = [read(name), read("skills")] if name == "fable" else [read(name), read("common"), read("skills")]
    return "\n\n".join(parts)


def _thread(ctx, body) -> bool:
    # a fork shares its parent's cached prefix, so it needs the same system prompt
    return bool(RoutingLayer._kind(ctx, body) or (body.get("tools") and ctx.agent_type.lower() == "fork"))


class CheatsheetLayer:
    def transform(self, body, ctx):
        if ctx.cfg["cheatsheet"]["mode"] != "on" or ctx.kind != "messages" or not isinstance(body.get("model"), str):
            return
        if not _thread(ctx, body):
            return
        sheet = text_for(body["model"])
        system = body.get("system")
        if not sheet or not system:
            return
        if isinstance(system, str):  # the first block is Claude Code's identity: never edit it, add a block after
            body["system"] = [{"type": "text", "text": system}, {"type": "text", "text": sheet}]
            return
        if not isinstance(system, list):
            return
        texts = [b for b in system if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if any(b["text"].endswith(sheet) for b in texts):
            return
        if len(texts) >= 2:  # the last block keeps its cache_control where it is
            texts[-1]["text"] += "\n\n" + sheet
        else:
            system.append({"type": "text", "text": sheet})
