"""Working rules, appended to the system prompt of every conversation thread.

The main thread and its forks get the model's full sheet; a sub-agent gets the short `agent.md` instead.

The text is chosen by the model the request will really be sent with, so this
layer runs after routing. It is read once per process and appended in the same
place every time, so a conversation's system prompt keeps the same bytes and
the prompt cache keeps hitting. No cache breakpoint is added, moved or removed.
"""
import functools
import os
from importlib import resources
from pathlib import Path

from . import config

FAMILIES = ("opus", "sonnet", "haiku", "fable")
NAMES = (*FAMILIES, "common", "skills", "agent", "lean")
LEVELS = {  # what the level adds to the lean rules; "full" is the rules as written
    "lite": "Level: lite. Build what was asked, and name the smaller option in one line so the person can choose it.",
    "ultra": "Level: ultra. Also question the request: before building, say which part the need does not justify.",
}
ALIASES = {"mythos": "fable"}  # the same model under another name
INDEX = 12  # playbooks listed
BUDGET_NOTE = ("# Context budget reached\n\nThis agent has used up its context budget. Stop now: finish only the step in progress, "
               "commit or save your work, then end with a hand-back that lists what is done and what is left, so the lead can "
               "continue in a fresh agent of the same type. Start no new work.")
COLD_NOTE = ("# Status note\n\nThe earlier conversation was compacted after a pause. Before you continue, write a brief handover "
             "status: what was being done, what is done, and what is next.")


def override_dir():
    """ALMANAC_CHEATSHEETS (set by `almanac eval` for one arm) wins over the user's folder under ALMANAC_HOME."""
    return Path(os.environ["ALMANAC_CHEATSHEETS"]) if os.environ.get("ALMANAC_CHEATSHEETS") else config.home() / "cheatsheets"


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


def skills(sub: bool = False, playbooks: bool = True) -> str:
    """The skills sheet without the lines that do not apply: a sub-agent does one job and hands nothing on, and with
    playbooks off there is no playbook to follow or record."""
    drop = (["- Hand a self-contained"] if sub else []) + ([] if playbooks else ["- When a note says an Almanac playbook"])
    head, *items = read("skills").split("\n- ")
    kept = [i for i in items if not any(("- " + i).startswith(d) for d in drop)]
    return "\n- ".join([head] + kept)


def text_for(model: str, sub: bool = False, playbooks: bool = True) -> str:
    """What is appended for this model, or "" for a model with no cheatsheet. `sub`: a sub-agent's short text.
    `playbooks`: false when playbooks are off, so the sheet does not point at them."""
    name = family(model)
    if not name:
        return ""
    if sub:  # one job, no handovers or merging: the short rules, and the skills unless the model is Haiku
        return read("agent") if name == "haiku" else "\n\n".join([read("agent"), skills(True, playbooks)])
    if name == "haiku":  # short on purpose: its own file is the whole text
        return read(name)
    parts = [read(name), skills(False, playbooks)] if name == "fable" else [read(name), read("common"), skills(False, playbooks)]
    return "\n\n".join(parts)


def lean_text(cfg) -> str:
    """Almanac's minimal-change rules, or "" when off. The same bytes on every request of a run: the packaged file (or
    the user's copy) is read once and the level and text come from settings, which are read once at start."""
    start = cfg["startup"]
    if not start.get("lean_rules"):
        return ""
    mine = (start.get("lean_rules_text") or "").strip()
    if mine:
        return mine
    return "\n\n".join(p for p in (read("lean"), LEVELS.get(start.get("lean_level") or "full", "")) if p)


@functools.lru_cache(maxsize=None)
def index() -> str:
    """The recorded playbooks that apply everywhere, one line each by name, or "" when there are none.

    A way of working cannot be matched from the words of a request ("yes, do it"), so Claude is told what is on
    hand. Read once per process and ordered by name only (use counts and projects change in the middle of a
    conversation), so a conversation's system prompt keeps its bytes for the run.
    """
    try:
        from getplaybook import store

        from . import _pbind  # noqa: F401

        books = sorted((pb for pb in store.listing() if not pb["project"] and pb["status"] == "approved"), key=lambda pb: pb["name"])[:INDEX]
        lines = [f"- {pb['name']}: {' '.join(pb['title'].split())[:80]}" + (" (script)" if store.runnable(pb) else "")
                 for pb in books]
    except Exception:  # a bad playbook folder must never block a request
        return ""
    if not lines:
        return ""
    return ("# Playbooks on hand\n\nWays of doing recurring work, recorded from earlier sessions. When the work in front of you "
            "is one of these, call `playbook_find` with its name for the steps and follow them, and run one marked (script) "
            "with `playbook_run` instead of doing its steps by hand.\n" + "\n".join(lines))


class CheatsheetLayer:
    def transform(self, body, ctx):
        self._sheet(body, ctx)
        try:
            self._notes(body, ctx)
        except Exception as exc:  # fail open: the request goes out without the notes
            ctx.stats["notes_error"] = type(exc).__name__

    @staticmethod
    def _notes(body, ctx):
        """Fixed notes as one last system block. A note is switched on once in the store and never off, so from the
        request that first shows it every later one carries the same bytes in the same place. No breakpoint is touched."""
        if ctx.kind != "messages" or not ctx.thread or getattr(ctx, "state", None) is None:
            return
        notes = []
        limit = int(ctx.cfg["compaction"].get("sub_budget_tokens") or 0)
        key = f"budgetnote:{ctx.lane}"
        if ctx.thread == "sub" and limit > 0 and ctx.cfg["compaction"]["enabled"]:
            from . import compact
            if ctx.state.get(key) or compact.estimate(body, ctx, body.get("messages") or []) >= limit:
                ctx.state.put(key, {"on": 1})
                notes.append(BUDGET_NOTE)
        if ctx.state.get(f"coldnote:{ctx.lane}"):
            notes.append(COLD_NOTE)
        system = body.get("system")
        if not notes or not system:
            return
        block = {"type": "text", "text": "\n\n".join(notes)}
        if isinstance(system, str):
            body["system"] = [{"type": "text", "text": system}, block]
        elif isinstance(system, list):
            system.append(block)

    def _sheet(self, body, ctx):
        if ctx.kind != "messages" or not isinstance(body.get("model"), str):
            return
        if not ctx.thread:
            return
        sub = ctx.thread == "sub"  # a fork is not "sub": it keeps its parent's text
        sheet = text_for(body["model"], sub, (ctx.cfg.get("playbooks") or {}).get("enabled", True)) if ctx.cfg["cheatsheet"]["mode"] == "on" else ""
        lean = lean_text(ctx.cfg)
        books = ctx.cfg.get("playbooks") or {}
        # the main thread and its forks only (a fork shares its parent's cached prefix); Haiku's sheet is short on purpose.
        # The list follows the playbook settings, not the cheatsheet's: it is there with the sheet off or no sheet for the model.
        main = ctx.thread in ("main", "fork")
        if main and family(body["model"]) != "haiku" and books.get("enabled") and books.get("inject") and index():
            sheet = sheet + "\n\n" + index() if sheet else index()
        if lean:  # every thread, whatever the model: the lean rules come first, the model's own sheet after them
            sheet = lean + "\n\n" + sheet if sheet else lean
        if sub and ctx.agent_type and getattr(ctx, "state", None) is not None and (ctx.cfg.get("lessons") or {}).get("enabled"):
            from . import lessons  # frozen at the thread's first request: a new lesson reaches only new threads
            learned = ctx.state.frozen(f"lessons:{ctx.lane}", lambda: lessons.text(ctx.state, ctx.agent_type[:40]))
            sheet = sheet + "\n\n" + learned if sheet and learned else sheet or learned
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
