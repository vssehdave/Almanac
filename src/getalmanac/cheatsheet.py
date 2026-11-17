"""Working rules, appended to the system prompt of every conversation thread.

The main thread and its forks get the model's full sheet; a sub-agent gets the short `agent.md` instead.

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
NAMES = (*FAMILIES, "common", "skills", "agent")
ALIASES = {"mythos": "fable"}  # the same model under another name
INDEX = 12  # playbooks listed


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


def text_for(model: str, sub: bool = False) -> str:
    """What is appended for this model, or "" for a model with no cheatsheet. `sub`: a sub-agent's short text."""
    name = family(model)
    if not name:
        return ""
    if sub:  # one job, no handovers or merging: the short rules, and the skills unless the model is Haiku
        return read("agent") if name == "haiku" else "\n\n".join([read("agent"), read("skills")])
    if name == "haiku":  # short on purpose: its own file is the whole text
        return read(name)
    parts = [read(name), read("skills")] if name == "fable" else [read(name), read("common"), read("skills")]
    return "\n\n".join(parts)


@functools.lru_cache(maxsize=None)
def index() -> str:
    """The recorded playbooks that apply everywhere, one line each by name, or "" when there are none.

    A way of working cannot be matched from the words of a request ("yes, do it"), so Claude is told what is on
    hand. Read once per process and ordered by name only (use counts and projects change in the middle of a
    conversation), so a conversation's system prompt keeps its bytes for the run.
    """
    try:
        from . import playbook

        books = sorted((pb for pb in playbook.listing() if not pb["project"]), key=lambda pb: pb["name"])[:INDEX]
        lines = [f"- {pb['name']}: {' '.join(pb['title'].split())[:80]}" + (" (script)" if playbook.runnable(pb) else "")
                 for pb in books]
    except Exception:  # a bad playbook folder must never block a request
        return ""
    if not lines:
        return ""
    return ("# Playbooks on hand\n\nWays of doing recurring work, recorded from earlier sessions. When the work in front of you "
            "is one of these, call `playbook_find` with its name for the steps and follow them, and run one marked (script) "
            "with `playbook_run` instead of doing its steps by hand.\n" + "\n".join(lines))


def _thread(ctx, body) -> bool:
    # a fork shares its parent's cached prefix, so it needs the same system prompt
    return bool(RoutingLayer._kind(ctx, body) or (body.get("tools") and ctx.agent_type.lower() == "fork"))


class CheatsheetLayer:
    def transform(self, body, ctx):
        if ctx.cfg["cheatsheet"]["mode"] != "on" or ctx.kind != "messages" or not isinstance(body.get("model"), str):
            return
        if not _thread(ctx, body):
            return
        sub = RoutingLayer._kind(ctx, body) == "sub"  # a fork is not "sub": it keeps its parent's text
        sheet = text_for(body["model"], sub)
        books = ctx.cfg.get("playbooks") or {}
        # the main thread and its forks only (a fork shares its parent's cached prefix); Haiku's sheet is short on purpose
        main = RoutingLayer._kind(ctx, body) == "main" or ctx.agent_type.lower() == "fork"
        if sheet and main and family(body["model"]) != "haiku" and books.get("enabled") and books.get("inject") and index():
            sheet += "\n\n" + index()
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
