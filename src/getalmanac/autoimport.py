"""Keep memory complete without a command: import new Claude Code transcripts in the background.

The proxy only sees conversations that go through it. While it runs, this thread imports the transcripts
Claude Code saved for the other sessions, a little after the proxy starts and then once an hour:

  1. new or grown session files under ~/.claude/projects, at most `memory.auto_import_days` old and not written to
     for an hour (a session still being written is left for the next round), for projects the store already
     holds conversations of (it never fills an empty store, and a forgotten project stays forgotten);
  2. the summary pass (summarize.refine, the personal login, within memory.summary_daily_calls) for those exchanges;
  3. a vector for every exchange and fact that lacks one, with the local model (no model call, no API tokens).

Only ~/.claude is read. Other config folders (a work account, a sandbox) are never imported, whatever
CLAUDE_CONFIG_DIR says. Everything here is idempotent: a file is read again only when its size or time changed.
"""
import os
import threading
import time
from pathlib import Path

from . import config, importer, summarize
from .memory import OPEN_SECONDS, Memory
from .store import Store

EVERY = 3600  # seconds between rounds
FIRST = 30  # seconds after the proxy starts: the first request must not compete with a big import
FILES = "autoimport:files"  # path -> [mtime, size] of what was imported
LAST = "autoimport:last"  # the report of the last round, for `almanac status`


def source() -> Path:
    return importer.transcript_dirs(None, "autoimport")[0] / "projects"


def _own_home() -> bool:
    """True when ALMANAC_HOME is unset or names the default ~/.almanac."""
    custom = os.environ.get("ALMANAC_HOME")
    return not custom or Path(custom).resolve() == (Path.home() / ".almanac").resolve()


def _known(memory) -> set:
    return {memory.projects.resolve(r["project"]) for r in memory.store.run("select distinct project from convo where project != ''")}


def run_once(memory, cfg, now=None) -> dict:
    """One round. Returns what it did: files, exchanges, summarized, failed, stopped, vectors (how many still wait,
    or None when no embedding model is installed)."""
    now = now or time.time()
    days, report = cfg["memory"]["auto_import_days"], {"ts": round(now), "files": 0, "exchanges": 0}
    seen = memory.store.get(FILES) or {}
    known, kept = _known(memory), {}
    for path in sorted(source().glob("*/*.jsonl")):
        try:
            stat = path.stat()
        except OSError:
            continue
        sig = [int(stat.st_mtime), stat.st_size]
        if now - stat.st_mtime > days * 86400:
            continue
        if seen.get(str(path)) in (sig, sig + [len(known)]):  # [.., n]: left alone while n projects were stored
            kept[str(path)] = sig
            continue
        if now - stat.st_mtime < OPEN_SECONDS:  # still being written: the next round
            continue
        try:
            done = importer.run(memory, [str(path)], days=days, only=known)
        except (OSError, ValueError):
            continue
        report["files"] += 1
        report["exchanges"] += done["exchanges"]
        # a run of a project not stored yet is looked at again once another project is stored
        kept[str(path)] = sig + [len(known)] if done["skipped"].get("project not stored yet") else sig
    memory.store.put(FILES, kept)
    report.update(summarized=0, failed=0, stopped="")
    if cfg["memory"]["summary_model"]:
        refined = summarize.refine(memory, cfg, say=lambda *_: None)
        report.update(summarized=refined["summarized"], failed=refined["failed"], stopped=refined["stopped"])
    report["vectors"] = memory.embed_all()
    memory.store.put(LAST, report)
    return report


def line(store) -> str:
    """One status line about the last round."""
    last = store.get(LAST)
    if not last:
        return "not run yet (it runs while `almanac serve` or `almanac` is up)"
    vectors = ("no embedding model installed (almanac semantic install)" if last["vectors"] is None
               else "every exchange and fact has a vector" if not last["vectors"] else f"{last['vectors']} still wait for a vector")
    return (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last['ts']))}: {last['files']} file(s), {last['exchanges']} exchange(s) "
            f"imported, {last['summarized']} summarized" + (f" (stopped: {last['stopped']})" if last["stopped"] else "") + "; " + vectors)


def start(proxy, first=FIRST, every=EVERY):
    """Start the background thread for a running proxy. Returns it, or None when memory.auto_import is off."""
    cfg = proxy.cfg
    if not (cfg["memory"]["enabled"] and cfg["memory"]["auto_import"]) or proxy.store is None:
        return None
    if not _own_home() and config._file().get("memory", {}).get("auto_import") is not True:
        return None  # a scratch or eval home (often a copy of the real store) must not import the real transcripts or spend the login's summary calls

    def loop():
        time.sleep(first)
        while True:
            store = None
            try:
                store = Store(proxy.store.path)  # a connection of its own: the proxy's requests never wait for an import
                run_once(Memory(store), cfg)
            except Exception as exc:  # background work never reaches the user
                proxy.log.write({"ts": time.time(), "kind": "error", "where": "autoimport", "error": f"{type(exc).__name__}: {exc}"[:300]})
            finally:
                if store:
                    store.close()
            time.sleep(every)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
