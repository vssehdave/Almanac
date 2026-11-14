"""The `almanac` command."""
import argparse
import json
import socket
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from . import __version__, config, launcher, report

USAGE = """almanac: a local proxy for Claude Code

  almanac [claude arguments]   run Claude Code through the proxy (the usual way)
  almanac claude [arguments]   the same, for arguments that clash with a command below
  almanac serve [--port N]     run only the proxy, for clients you start yourself
  almanac report [--json]      where the latest session's context went, and what was kept out
  almanac memory search WORDS  search stored conversations (--project NAME, --all)
  almanac memory show ID       read one stored exchange or topic in full
  almanac memory recent        the latest conversations, by project
  almanac memory forget        delete stored conversations (--project NAME | --conversation ID | --all)
  almanac projects             projects seen; `projects merge FROM INTO`, `projects rename NAME NEW`
  almanac status               settings, files and recent activity
  almanac doctor               check that everything needed is in place
  almanac config [set KEY VALUE | path]
  almanac version

Everything stays on this machine. Requests go to the same upstream Claude Code
already uses, with Claude Code's own login.
"""


def _serve(rest):
    parser = argparse.ArgumentParser(prog="almanac serve")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(rest)
    from . import proxy

    server = proxy.build(config.load()).serve(port=args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"almanac is listening on {url}\n"
          f"Start a client with:\n  ANTHROPIC_BASE_URL={url}\n  _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1\n"
          "Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _log():
    return report.Log(config.home() / "logs" / "requests.jsonl")


def _report(rest):
    parser = argparse.ArgumentParser(prog="almanac report")
    parser.add_argument("--json", action="store_true", help="print the latest session-start record as JSON")
    args = parser.parse_args(rest)
    records = _log().tail()
    if args.json:
        print(json.dumps({"startup": report.startup(records), "summary": report.summary(records)}, indent=2))
    else:
        print(report.render(records))
    return 0


def _status(_rest):
    cfg, home = config.load(), config.home()
    records = _log().tail()
    print(f"almanac {__version__}")
    print(f"  files      {home}")
    print(f"  upstream   {cfg['upstream']}")
    for layer in ("skills", "tools", "memory", "compaction"):
        print(f"  {layer:<10} {'on' if cfg[layer]['enabled'] else 'off'}")
    if records:
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(records[-1].get("ts", 0)))
        totals = report.summary(records)
        print(f"  last request {last}; last 24 hours: {totals['requests']} requests, "
              f"about {totals['saved_tokens']:,} input tokens kept out")
    else:
        print("  no requests recorded yet")
    counts = _memory_handle().counts()
    print(f"  memory     {counts['convos']} conversation(s), {counts['exchanges']} exchange(s), {counts['topics']} topic(s)")
    return 0


def _doctor(_rest):
    cfg, ok = config.load(), True

    def check(name, good, detail):
        nonlocal ok
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {name}: {detail}")

    exe = launcher.find_claude()
    check("Claude Code", bool(exe), exe or "not found; install it or set ALMANAC_CLAUDE")
    check("Python", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        probe = config.home() / ".write-test"
        probe.write_text("x")
        probe.unlink()
        check("files", True, str(config.home()))
    except OSError as exc:
        check("files", False, f"{config.home()} is not writable: {exc}")
    try:
        sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)")
        check("SQLite full-text search", True, sqlite3.sqlite_version)
    except sqlite3.Error as exc:
        check("SQLite full-text search", False, f"FTS5 is missing: {exc}")
    up = urlsplit(cfg["upstream"])
    try:
        socket.create_connection((up.hostname, up.port or (443 if up.scheme == "https" else 80)), timeout=5).close()
        check("upstream", True, cfg["upstream"])
    except OSError as exc:
        check("upstream", False, f"{cfg['upstream']} is unreachable: {exc}")
    return 0 if ok else 1


def _config(rest):
    if rest[:1] == ["path"]:
        print(config.config_path())
    elif rest[:1] == ["set"] and len(rest) == 3:
        try:
            value = config.set_value(rest[1], rest[2])
        except KeyError:
            print(f"almanac: no setting named {rest[1]}", file=sys.stderr)
            return 2
        except TypeError as exc:
            print(f"almanac: {exc}", file=sys.stderr)
            return 2
        print(f"{rest[1]} = {json.dumps(value)}")
    elif not rest:
        print(json.dumps(config.load(), indent=2))
    else:
        print("usage: almanac config [set KEY VALUE | path]", file=sys.stderr)
        return 2
    return 0


def _memory_handle():
    from .memory import Memory
    from .store import Store

    return Memory(Store(config.home() / "almanac.db"))


def _memory(rest):
    import os

    from . import mcp

    parser = argparse.ArgumentParser(prog="almanac memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    search = sub.add_parser("search")
    search.add_argument("words", nargs="+")
    search.add_argument("--project")
    search.add_argument("--all", action="store_true")
    search.add_argument("--limit", type=int, default=8)
    sub.add_parser("show").add_argument("id", type=int)
    sub.add_parser("recent")
    forget = sub.add_parser("forget")
    group = forget.add_mutually_exclusive_group(required=True)
    group.add_argument("--project")
    group.add_argument("--conversation", type=int)
    group.add_argument("--all", action="store_true")
    args = parser.parse_args(rest)
    memory = _memory_handle()

    def project_key(word):
        key = memory.projects.find(word)
        if not key:
            print(f"almanac: no project is called {word!r}; see `almanac projects`", file=sys.stderr)
            raise SystemExit(2)
        return key

    if args.cmd == "search":
        project = None if args.all else (project_key(args.project) if args.project else memory.projects.see(os.getcwd(), touch=False))
        _print(mcp.render_hits(memory.search(" ".join(args.words), project=project, limit=args.limit)))
    elif args.cmd == "show":
        _print(mcp.render_node(memory, memory.read(args.id)))
    elif args.cmd == "recent":
        _print(mcp.render_projects(memory))
    else:
        count = memory.forget(project=project_key(args.project) if args.project else None, convo_id=args.conversation)
        print(f"forgot {count} conversation(s)")
    return 0


def _projects(rest):
    memory = _memory_handle()
    if rest[:1] == ["merge"] and len(rest) == 3:
        source, target = (memory.projects.find(word) for word in rest[1:])
        if not source or not target or source == target:
            print("almanac: name two different known projects; see `almanac projects`", file=sys.stderr)
            return 2
        memory.merge_projects(source, target)
        print(f"merged {source} into {target}")
    elif rest[:1] == ["rename"] and len(rest) == 3:
        key = memory.projects.find(rest[1])
        if not key:
            print(f"almanac: no project is called {rest[1]!r}", file=sys.stderr)
            return 2
        memory.projects.rename(key, rest[2])
        print(f"{key} is now called {rest[2]}")
    elif not rest:
        counts = {r["project"]: r["n"] for r in memory.store.run("select project, count(*) n from convo group by project")}
        rows = memory.projects.listing()
        if not rows:
            print("No projects seen yet.")
        for row in rows:
            _print(f"{row['name']:<24} {counts.get(row['key'], 0):>4} conversation(s)   {row['key']}   {row['root']}")
    else:
        print("usage: almanac projects [merge FROM INTO | rename NAME NEW]", file=sys.stderr)
        return 2
    return 0


def _mcp(_rest):
    from . import mcp

    return mcp.serve()


def _print(text):
    """Print text that may hold characters the console cannot show."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "ascii", "replace").decode(sys.stdout.encoding or "ascii"))


def _version(_rest):
    print(f"almanac {__version__}")
    return 0


COMMANDS = {"serve": _serve, "report": _report, "status": _status, "doctor": _doctor, "config": _config,
            "version": _version, "memory": _memory, "projects": _projects, "mcp": _mcp}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] in (["-h"], ["--help"], ["help"]):
        print(USAGE)
        return 0
    if argv[:1] == ["claude"]:
        return launcher.run(argv[1:], config.load())
    if argv and argv[0] in COMMANDS:
        if argv[0] != "mcp" and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # stored text is not always ASCII
        return COMMANDS[argv[0]](argv[1:])
    return launcher.run(argv, config.load())
