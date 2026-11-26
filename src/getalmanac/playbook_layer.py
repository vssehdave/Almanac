"""The proxy side of playbooks: offers placed at the end of the newest message, frozen per lane.

The playbooks themselves (the folder format, matching, route offers, running, mining) are the `getplaybook` package; this layer
only decides where an offer goes in a request and keeps it byte-identical on every later request of the conversation.
"""
from pathlib import Path

from getplaybook import offer as po
from getplaybook import run as pr
from getplaybook import store as ps

from . import _pbind  # noqa: F401  (getplaybook uses Almanac's home and settings)
from . import library, report
from . import request as rq
from .memory import MemoryLayer
from .routing import CONTINUE, _digest, handwritten, person_text, person_turns
from .startup import _insert_before_last


def _memory_tools(cfg) -> bool:
    """The memory tools are offered only when both settings are on, the same test as mcp.client_config."""
    memory = cfg.get("memory") or {}
    return bool(memory.get("enabled", True) and memory.get("tools", True))


class PlaybookLayer:
    """Tell Claude which playbook applies to the newest typed request of the main conversation thread.
    When none does, a prompt-library entry for a task like it is offered instead (never both).

    The note is frozen per (lane, turn, digest of the typed text), an empty one included, so every
    later request of the conversation carries the same bytes whatever happens to the playbook.
    """

    @staticmethod
    def _key(ctx, turn, text) -> str:
        return f"playbook:{ctx.lane}:{turn}:{_digest(text)}"

    def transform(self, body, ctx):
        cfg, lib = ctx.cfg.get("playbooks") or {}, ctx.cfg.get("library") or {}
        books = bool(cfg.get("enabled") and cfg.get("inject"))
        entries = lib.get("enabled", True) and lib.get("inject", True)
        if not (books or entries) or ctx.state is None or ctx.kind != "messages":
            return
        main, sub = MemoryLayer._main_thread(body, ctx), MemoryLayer._sub_thread(body, ctx)
        if not (main or sub):
            return
        try:
            if main:
                self._apply(body, ctx, books, entries)
            if books and cfg.get("route", True):
                self._route(body, ctx, main)
        except Exception as exc:  # fail open: a bad playbook folder must never block a turn
            report.log_error("playbook", exc, ctx)
            return

    def _apply(self, body, ctx, books=True, entries=False):
        messages, orig = body["messages"], (ctx.orig or body)["messages"]
        seen, mine = person_turns(orig), person_turns(messages)
        if not seen:
            return
        last = len(seen) - 1
        newest = person_text(orig[seen[last]])
        key = self._key(ctx, last, newest)
        akey = f"{key}:m{seen[last]}"  # an automatic run is kept per message position too, so a rewind and retype runs it again
        text = ctx.state.get(akey)
        if text is None:
            text = ctx.state.get(key)
        if text is None:
            low = handwritten(newest)
            if len(low) < po.MIN_REQUEST or CONTINUE.match(low):
                text = ""  # decided by the words alone, so nothing is stored
            else:
                text = self._auto(ctx, low, akey) if books else None
                if text is None:
                    text = ctx.state.frozen(key, lambda: self._first(ctx, low, key, books, entries))
        nudge = ctx.state.frozen("playbooknudge" + key[len("playbook"):], lambda: self._nudge(ctx, orig, seen, last)) if books else ""
        # Earlier turns got their note when they were new: put it back, byte for byte, so the cached prefix holds.
        # When compaction changed the history the turns no longer line up, and there is nothing to put back.
        lane = ctx.lane
        kept = ctx.state.scan(f"playbook:{lane}:", f"playbooknudge:{lane}:", f"playbookskip:{lane}:") if last else {}  # one query for every earlier turn
        if len(mine) == len(seen):
            for turn in range(last):
                earlier = self._key(ctx, turn, person_text(orig[seen[turn]]))
                old = kept.get(f"{earlier}:m{seen[turn]}")
                if old is None:
                    old = kept.get(earlier)
                old = "\n".join(x for x in (old, kept.get("playbooknudge" + earlier[len("playbook"):])) if x)
                if old:
                    _insert_before_last(messages[mine[turn]], old)
        for turn in range(last):  # a reminder that went into an earlier turn's tool loop stays where it was put
            earlier = self._key(ctx, turn, person_text(orig[seen[turn]]))
            self._place(messages, orig, kept.get("playbookskip" + earlier[len("playbook"):]))
        text = "\n".join(x for x in (text, nudge) if x)
        if text:
            _insert_before_last(messages[mine[-1]], text)
            ctx.stats.setdefault("playbook", {})["chars"] = len(text)
            if nudge:
                ctx.stats["playbook"]["nudge"] = True
            self._place(messages, orig, self._skipped(ctx, orig, seen[last], key))

    # ---- route: offers made from what the thread is doing, not from the typed words ----

    @staticmethod
    def _carrier(msg) -> bool:
        """A message an offer can be appended to: a user message of tool results."""
        return isinstance(msg, dict) and isinstance(msg.get("content"), list) and rq.is_tool_result(msg)

    @staticmethod
    def _called(messages) -> set:
        """The names of the playbooks the thread has called with playbook_run."""
        return {str(b["input"].get("name")) for m in messages if isinstance(m, dict) and m.get("role") == "assistant" for b in rq.blocks(m)
                if b.get("type") == "tool_use" and str(b.get("name")).endswith("playbook_run") and isinstance(b.get("input"), dict)}

    def _route(self, body, ctx, main):
        """Offer a playbook when two of three signals say it covers the rest of the work: the route so far (the phases of the
        tool calls) is the start of its route, the assistant's own narration matches its triggers, and the repo has something
        to ship (the last only for a route that ships). A main thread is judged from its newest typed turn on, a sub-agent from
        its whole thread. The decision is stored per lane, turn and message, and put back byte for byte on every later request.
        An offer is one short note at the end of the newest message, never in the cached prefix."""
        messages, orig = body["messages"], (ctx.orig or body)["messages"]
        seen = person_turns(orig)
        if len(messages) != len(orig) or not seen:
            return
        turn, start = (len(seen) - 1, seen[-1]) if main else (0, seen[0])
        last, offered = len(orig) - 1, {}
        for t, first in (list(enumerate(seen)) if main else [(0, seen[0])]):  # earlier turns too: their bytes must not change
            end = seen[t + 1] if main and t + 1 < len(seen) else len(orig)
            key = f"playbookroute:{ctx.lane}:{t}:{_digest(person_text(orig[first]))}"
            for i in range(first + 1, end):
                kept = ctx.state.get(f"{key}:{i}") if self._carrier(orig[i]) else None
                if kept:
                    messages[i]["content"].append({"type": "text", "text": kept["text"]})
                    if t == turn:
                        offered[kept["name"]] = kept
        base = f"playbookroute:{ctx.lane}:{turn}:{_digest(person_text(orig[start]))}"
        called = self._called(orig[start:])
        asked = ctx.state.get("playbookoffer" + self._key(ctx, turn, person_text(orig[start]))[len("playbook"):]) if main else None
        for name in sorted((set(offered) | {asked}) & called):  # an offer that was taken: say so once
            mark = f"playbooktaken:{ctx.lane}:{turn}:{name}"
            if ctx.state.get(mark) is None:
                ctx.state.put(mark, 1)
                ctx.stats["playbook_taken"] = name
                break
        if self._carrier(orig[last]) and ctx.state.get(f"{base}:{last}") is None:
            kept = self._decide(ctx, orig, start, turn, main, set(offered) | {asked}, called)
            ctx.state.put(f"{base}:{last}", kept or "")
            if kept:
                messages[last]["content"].append({"type": "text", "text": kept["text"]})
                ctx.stats["playbook_offer"], ctx.stats["signals"] = kept["name"], kept["signals"]

    def _decide(self, ctx, orig, start, turn, main, offered, called):
        """The offer to make now as {name, text, signals}, or None."""
        kind = "" if main else ctx.agent_type
        if not main and not kind:
            return None
        asked = [m for m in orig[start + 1:] if isinstance(m, dict) and m.get("role") == "assistant"]

        def ahead():  # decided at most once per turn, the first time a route that ships needs it
            key = f"playbookfacts:{ctx.lane}:{turn}"
            known = ctx.state.get(key)
            if known is None:
                known = int(po._ahead(ctx.cwd))
                ctx.state.put(key, known)
            return bool(known)

        return po.decide(asked, ctx.project or "", kind, offered, called, ctx.cwd, ahead)

    def _nudge(self, ctx, orig, seen, last) -> str:
        """A reminder, frozen with the turn that starts now, to record the previous turn as a playbook: it took
        `playbooks.nudge_after` tool calls or more, saved none and matched none. "" when there is nothing to say."""
        limit = (ctx.cfg.get("playbooks") or {}).get("nudge_after", 0)
        if last < 1 or type(limit) is not int or limit <= 0:
            return ""
        prev = seen[last - 1]
        calls = [b.get("name") or "" for m in orig[prev + 1:seen[last]] if isinstance(m, dict) and m.get("role") == "assistant"
                 for b in rq.blocks(m) if b.get("type") == "tool_use"]
        earlier = self._key(ctx, last - 1, person_text(orig[prev]))
        matched = ctx.state.get(f"{earlier}:m{prev}") or ctx.state.get(earlier)
        if len(calls) < limit or matched or any(c.endswith("playbook_save") for c in calls):
            return ""
        if self._covered(ctx, orig[prev:seen[last]], person_text(orig[prev])):
            return ""
        return (f"<system-reminder>\nThe previous task took {len(calls)} tool calls and has no playbook. If it will come up again, "
                "record it with the `playbook` skill once this request is answered.\n</system-reminder>")

    @staticmethod
    def _covered(ctx, group, text) -> bool:
        """True when a playbook or draft (any status) matches the request, or the miner or the library's repeated routes
        already have it as a candidate to compile: the model and the compiler would draft the same playbook twice."""
        from getplaybook import mine
        if any(po.score(pb, text) for pb in ps.listing()):
            return True
        route = tuple(library._collapse([s for c in library._calls(group) for s in c["steps"]])[:library.MAX_ROUTE])
        found = mine._load(mine.candidates_path(), {}).get("candidates") or []
        return library._found(route, text, found + library.candidates(ctx.memory_store, found, library.min_tools(ctx.cfg)))

    @staticmethod
    def _place(messages, orig, kept):
        """Put a stored reminder back after the tool results of the message it first went into, or nowhere
        when the history no longer lines up (compacted or rewound)."""
        if not kept or len(messages) != len(orig) or not 0 <= kept["at"] < len(messages):
            return
        msg = messages[kept["at"]]
        if isinstance(msg, dict) and rq.is_tool_result(msg) and isinstance(msg.get("content"), list):
            msg["content"].append({"type": "text", "text": kept["text"]})

    @staticmethod
    def _skipped(ctx, orig, start, key):
        """The reminder for this turn: decided once, when the model has made 2 tool calls without taking an offered script."""
        suffix = key[len("playbook"):]
        kept = ctx.state.get("playbookskip" + suffix)
        name = ctx.state.get("playbookoffer" + suffix)
        if kept or not name:
            return kept
        calls = [b.get("name") or "" for m in orig[start + 1:] if isinstance(m, dict) and m.get("role") == "assistant"
                 for b in rq.blocks(m) if b.get("type") == "tool_use"]
        calls = [c for c in calls if c != "ToolSearch"]
        if len(calls) < 2 or any(c.endswith("playbook_run") for c in calls) or not rq.is_tool_result(orig[-1]):
            return None
        pb = ps.load(name)
        if pb is None or not ps.runnable(pb):
            return None
        args = ", ".join(f"<{a['name']}>" for a in pb["args"])
        text = (f'<system-reminder>\nAlmanac playbook "{name}" has an approved script that does this job. Call the tool '
                f'`playbook_run` with name "{name}" and args [{args}] now instead of continuing by hand, '
                "unless this request really differs from the playbook.\n</system-reminder>")
        return ctx.state.frozen("playbookskip" + suffix, lambda: {"at": len(orig) - 1, "text": text})

    @staticmethod
    def _auto(ctx, low, key):
        """The note for an automatic playbook, after running its script outside the store's lock; None when this request is not one."""
        if not (ctx.cfg.get("playbooks") or {}).get("auto_run") or not ctx.cwd or not Path(ctx.cwd).is_dir():
            return None
        best = po.find(low, ctx.project, limit=1, agent_type="")
        pb = best[0] if best else None
        if pb is None or not ps.automatic(pb) or not po.strong(pb, low):
            return None
        with pr.AUTO_LOCK:  # a second request of the same turn waits here, then finds the stored note
            kept = ctx.state.get(key)
            if kept is not None:
                return kept
            try:
                head, out, err, code = pr._execute_auto(pb["name"], ctx.cwd, pr.AUTO_TIMEOUT)
                text = po.ran_note(ps.bump(pb["name"]) or pb, head, out, err)
            except Exception as exc:  # a script that cannot run leaves the ordinary note
                report.log_error("playbook:script", exc, ctx)
                return None
            ctx.stats["playbook"] = {"ran": pb["name"], **({"exit": code} if code is not None else {"timeout": True})}
            ctx.state.put(key, text)
            return text

    @staticmethod
    def _entry(ctx, low) -> str:
        """The note for the prompt-library entry that best matches the typed text, or "" (decided once per turn, so counted once)."""
        cfg = ctx.cfg.get("library") or {}
        hit = library.find(ctx.memory_store, low, ctx.project or "", cfg.get("threshold", library.THRESHOLD), library.min_tools(ctx.cfg))
        if not hit or f"[{hit[0]['node']}]" in (ctx.state.get(f"recall:{ctx.lane}") or ""):  # the recall note already cites it
            return ""
        entry, score = hit
        entry["uses"] = library.bump(ctx.memory_store, entry["id"])
        ctx.stats["library"] = {"id": entry["id"], "score": score}
        return library.note(entry, cfg.get("note_chars", library.NOTE_CHARS), _memory_tools(ctx.cfg))

    @classmethod
    def _first(cls, ctx, low, key, books=True, entries=False) -> str:
        best = po.find(low, ctx.project, limit=1, agent_type="") if books else []
        if not best:
            return cls._entry(ctx, low) if entries else ""
        done = ps.bump(best[0]["name"]) or best[0]
        if ps.runnable(done):  # offered, not run: remember it, so a skipped offer can be called back
            ctx.state.put("playbookoffer" + key[len("playbook"):], done["name"])
        return po.note(done)
