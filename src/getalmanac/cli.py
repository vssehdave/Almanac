"""The `almanac` command."""
import argparse
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

from . import __version__, accounts, autoimport, config, launcher, profile, report, summarize, upstream

USAGE = """almanac: a local proxy for Claude Code

  almanac [claude arguments]   run Claude Code through the proxy (the usual way)
  almanac claude [arguments]   the same, for arguments that clash with a command below
  almanac sandbox [--plugin PATH ...] [--list] [-- claude arguments]   plain Claude Code in ~/.claude-sandbox, no proxy and none of your plugins, loading only those plugins
  almanac serve [--port N]     run only the proxy, for clients you start yourself
  almanac report [--json]      where the latest session's context went, and what was kept out
  almanac memory search WORDS  search stored conversations (--project NAME, --all)
  almanac memory show ID       read one stored exchange or topic in full
  almanac memory recent        the latest conversations, by project
  almanac memory summarize     model summaries for imported exchanges (--project NAME, --limit N, --dry-run, --facts, --backfill)
  almanac memory import PATH... store past Claude Code transcripts (a ~/.claude/projects folder name, folder or .jsonl; --days N, --dry-run)
  almanac memory forget        delete stored conversations (--project NAME | --conversation ID | --all; --force for open ones)
  almanac cloud packet         print the hand-off packet for a Claude Code cloud session (--conversation ID)
  almanac cloud send           show that packet, ask, then run `claude --cloud` with it (--conversation ID, --yes)
  almanac cloud watchdog       the setup and the prompt for a scheduled cloud task that takes over a stalled one
  almanac cloud on|off         let this repository take part in the automatic hand-off, or stop it
  almanac cloud status         hand-off on or off, its last heartbeat, and any takeover branch on origin
  almanac cloud routine check  read cloud.routine and print its status, enabled flag and next run (--login FOLDER)
  almanac semantic [install|status|index|on|off]   search memory by meaning (optional model)
  almanac route [status]       what model routing is set to and what it decided lately
  almanac route pin TIER       keep this project on small, medium or large (`pin off` lifts it)
  almanac cheatsheet show [opus|sonnet|haiku|fable|agent]   the rules added for that model (agent: for a sub-agent), and their size
  almanac cheatsheet path      where your own copies go (opus.md, sonnet.md, haiku.md, fable.md, common.md, skills.md, agent.md, lean.md)
  almanac eval [--yes]         measure whether the cheatsheet changes behaviour: off, current and local variants on sample tasks (spends allowance; prints the plan first)
  almanac skill scan PATH|URL  scan a skill or plugin with SkillSpector (exit 0 SAFE, 1 CAUTION, 2 DO_NOT_INSTALL, 3 error)
  almanac skill add URL|PATH   fetch, scan, and install its skills only if SAFE (--skill NAME, --ref SHA)
  almanac skill add --bundle URL|PATH   the same, installed as a plugin in your Almanac bundle (the adopt skill continues)
  almanac skill import-bundle PATH      copy plugin folders from an old bundle into your Almanac bundle
  almanac skill integrate NAME check an adopted plugin for outside links, overlaps and layout (exit 1 while open)
  almanac skill update [NAME]  list adopted skills with a newer upstream commit and the command to re-adopt (exit 1 if any)
  almanac skill audit          scan installed skills and plugins not scanned yet (--again: all), then a table
  almanac skill accept ITEM    stop the launch warning for this item at its current contents (`unaccept` undoes it)
  almanac pack list            the pack: third-party skills and plugins, each pinned to a commit
  almanac pack install [NAME]  install the pack's skills through the scan; plugins and programs are only shown
  almanac playbook list|show|approve [--auto]|remove|path|run   recorded ways of doing repeated tasks, and their scripts
  almanac playbook mine        find tasks you repeat in past sessions, no model (--days 30, --min 3)
  almanac playbook compile     have one Claude Code session write the best repeat up as a playbook (--top N, --dry-run)
  almanac playbook dedupe      merge playbooks that do the same route into one (--include-approved to touch approved ones)
  almanac library list|show|forget   the prompt library: how past prompts were solved (list --project NAME; show ID; forget ID)
  almanac lessons [clear AGENT]   lessons per sub-agent type, from review verdicts; clear drops one type's
  almanac projects             projects seen; `projects merge FROM INTO`, `projects rename NAME NEW`
  almanac errors [--since 24h] [--limit N] [--json]   recent errors the proxy carried on past
  almanac status               settings, files and recent activity
  almanac doctor               check that everything needed is in place
  almanac config [set KEY VALUE | path]
  almanac version

Everything stays on this machine. Requests go to the same upstream Claude Code
already uses, with Claude Code's own login. Two things you turn on hand stored
text to anything else: `almanac cloud send` (Claude Code's cloud option) and
`cloud.auto` (a hand-off branch on your git remote).
"""


def _serve(rest):
    parser = argparse.ArgumentParser(prog="almanac serve")
    parser.add_argument("--port", type=int, help="default: the `port` setting (8787)")
    args = parser.parse_args(rest)
    from . import proxy

    cfg = config.load()
    try:
        server = proxy.build(cfg).serve(port=args.port or cfg["port"])
    except OSError as exc:
        print(f"almanac: cannot listen on port {args.port or cfg['port']}: {exc}", file=sys.stderr)
        return 1
    port, token = server.server_address[1], server.proxy.token
    url = f"http://127.0.0.1:{port}/{token}"
    try:
        launcher.publish(port, token, server.proxy.key)
    except OSError as exc:
        print(f"almanac: could not write {launcher.serve_file()}, so `almanac` will not share this proxy: {exc}", file=sys.stderr)
    print(f"almanac is listening on 127.0.0.1:{port}\n"
          "While it runs, `almanac` shares it. Other clients need this address, which carries a secret:\n"
          f"  ANTHROPIC_BASE_URL={url}\n  _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1\n"
          "Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.proxy.keepalive.stop_all()
        launcher.unpublish(token)
    return 0


def _log():
    return report.Log(config.home() / "logs" / "requests.jsonl")


def _report(rest):
    parser = argparse.ArgumentParser(prog="almanac report")
    parser.add_argument("--json", action="store_true", help="print the latest session-start record as JSON")
    args = parser.parse_args(rest)
    records = _log().tail()
    if args.json:
        print(json.dumps({"startup": report.startup(records), "summary": report.summary(records),
                          "meter": report.meter(records)}, indent=2))
    else:
        print(report.render(records))
        if line := report.error_line(_log()):
            print(f"\n{line}")
    return 0


def _errors(rest):
    parser = argparse.ArgumentParser(prog="almanac errors")
    parser.add_argument("--since", default="24h", help="how far back, such as 30m, 24h or 7d (default 24h)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(rest)
    try:
        found = report.recent_errors(_log(), args.since, args.limit)
    except ValueError as exc:
        print(f"almanac: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(found, indent=2) if args.json else report.render_errors(found))
    return 0


def _status(_rest):
    cfg, home = config.load(), config.home()
    records = _log().tail()
    print(f"almanac {__version__}")
    print(f"  files      {home}")
    print(f"  upstream   {cfg['upstream']}")
    for layer in ("skills", "tools", "memory", "compaction"):
        print(f"  {layer:<10} {'on' if cfg[layer]['enabled'] else 'off'}")
    print(f"  routing    {cfg['routing']['mode']}")
    if cfg["accounts"]["fallback"]:
        print(f"  accounts   {accounts.status_line(cfg, records)}")
    if line := report.error_line(_log()):
        print(f"  errors     {line}")
    if records:
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(records[-1].get("ts", 0)))
        totals = report.summary(records)
        print(f"  last request {last}; last 24 hours: {totals['requests']} requests, "
              f"about {totals['saved_tokens']:,} input tokens kept out")
        for text in report.render_meter(records):
            _print(text)
    else:
        print("  no requests recorded yet")
    memory = _memory_handle()
    for name, file in (("memory db", "almanac.db"), ("state db", "state.db")):  # after the handle: it creates both
        print(f"  {name:<10} {file}, {(home / file).stat().st_size / 1e6:.1f} MB")
    counts = memory.counts()
    print(f"  memory     {counts['convos']} conversation(s), {counts['exchanges']} exchange(s), {counts['topics']} topic(s)")
    print(f"  summaries  {summarize.line(memory, cfg)}")
    print(f"  auto import {autoimport.line(memory.state) if cfg['memory']['auto_import'] else 'off (memory.auto_import)'}")
    print(f"  statuses   {_status_counts(memory)}")
    seen = report.render_routing(records)
    if seen:
        _print(seen.lstrip("\n"))
    return 0


def _status_counts(memory) -> str:
    return ", ".join(f"{name} {n}" for name, n in memory.status_counts().items())


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
        for file in ("almanac.db", "state.db"):
            path = config.home() / file
            check(file, True, f"{path.stat().st_size / 1e6:.1f} MB" if path.exists() else "not created yet")
    except OSError as exc:
        check("files", False, f"{config.home()} is not writable: {exc}")
    try:
        sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)")
        check("SQLite full-text search", True, sqlite3.sqlite_version)
    except sqlite3.Error as exc:
        check("SQLite full-text search", False, f"FTS5 is missing: {exc}")
    up = urlsplit(cfg["upstream"])
    host, port = up.hostname or "", up.port or (443 if up.scheme == "https" else 80)
    ctx = ssl.create_default_context()
    loaded, failed = upstream.load_cas(ctx, cfg)
    check("extra CA file", not failed, f"could not load {', '.join(failed)}" if failed else
          ", ".join(loaded) if loaded else "none set (upstream_ca, SSL_CERT_FILE, NODE_EXTRA_CA_CERTS)")
    hop = upstream.find(cfg, host)
    if hop and not hop.supported:
        check("upstream proxy", False, f"the {hop.scheme}:// proxy from {hop.source} is not supported; "
              "only http:// proxies are. Connecting directly")
    elif hop:
        check("upstream proxy", True, f"{hop.label} (from {hop.source})")
    else:
        check("upstream proxy", True, "none; connecting directly")
    hop = hop if hop and hop.supported else None
    try:
        sock = upstream.open_socket(hop, host, port, 5)
        if up.scheme == "https":
            ctx.wrap_socket(sock, server_hostname=host)
        sock.close()
        check("upstream", True, cfg["upstream"] + (f" (CONNECT through {hop.label} works)" if hop else ""))
    except OSError as exc:
        check("upstream", False, f"{cfg['upstream']} is unreachable: {exc}")
    # not a failure: only skill scanning and adopting need it
    from . import skills
    if not skills.scanner():
        print(f"  note SkillSpector: not installed, so skills cannot be scanned or adopted. Install it with: {skills.INSTALL}")
    if todo := skills.pending():
        print(f"  note integration pending: {', '.join(todo)}. Run: almanac skill integrate NAME")
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
        print(f"{rest[1]} = {json.dumps(upstream.hide(value) if rest[1] == 'upstream_proxy' else value)}")
    elif not rest:
        cfg = config.load()
        cfg["upstream_proxy"] = upstream.hide(cfg["upstream_proxy"])  # its login is never printed
        print(json.dumps(cfg, indent=2))
    else:
        print("usage: almanac config [set KEY VALUE | path]", file=sys.stderr)
        return 2
    return 0


def _memory_handle():
    from .memory import Memory
    from .store import Store

    return Memory(Store(config.home() / "almanac.db"))


def _memory(rest):
    if rest[:1] == ["banner"]:  # before the parser and the store: it must stay quiet and fast
        return _banner(rest[1:])
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
    load = sub.add_parser("import")
    load.add_argument("paths", nargs="+", metavar="FOLDER_OR_FILE")
    load.add_argument("--days", type=int, help="only sessions with a message in the last N days")
    load.add_argument("--dry-run", action="store_true", help="store nothing; print what would be stored")
    refine = sub.add_parser("summarize")
    refine.add_argument("--project")
    refine.add_argument("--limit", type=int, help="most calls (default: what is left of memory.summary_daily_calls today)")
    refine.add_argument("--dry-run", action="store_true", help="make no calls; print how many exchanges would be summarized")
    refine.add_argument("--facts", action="store_true", help="instead, add searchable facts to exchanges that were summarized before facts existed")
    refine.add_argument("--backfill", action="store_true", help="instead, summarize conversations that have no model summary and close the open work later exchanges finished")
    forget = sub.add_parser("forget")
    group = forget.add_mutually_exclusive_group(required=True)
    group.add_argument("--project")
    group.add_argument("--conversation", type=int)
    group.add_argument("--all", action="store_true")
    forget.add_argument("--force", action="store_true", help="also delete conversations changed in the last hour")
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
    elif args.cmd == "import":
        from . import importer

        try:
            report = importer.run(memory, args.paths, days=args.days, dry_run=args.dry_run)
        except FileNotFoundError as error:
            print(f"almanac: {error}", file=sys.stderr)
            return 2
        _print(importer.render(report, args.dry_run))
        if not args.dry_run:
            left = memory.embed_all()
            print("vectors: no embedding model installed (almanac semantic install)" if left is None
                  else f"vectors: {left} exchange(s) and fact(s) still wait for one" if left else "vectors: every exchange and fact has one")
    elif args.cmd == "summarize":
        (summarize.backfill if args.backfill else summarize.refine)(
            memory, config.load(), project=project_key(args.project) if args.project else None, limit=args.limit,
            dry_run=args.dry_run, **({} if args.backfill else {"facts": args.facts}))
    elif args.cmd == "recent":
        _print(mcp.render_projects(memory))
        print(f"exchanges by status: {_status_counts(memory)}")
    else:
        count = memory.forget(project=project_key(args.project) if args.project else None, convo_id=args.conversation,
                              force=args.force)
        print(f"forgot {count} conversation(s)")
        if memory.kept:
            print(f"kept {memory.kept} open conversation(s) (changed in the last hour); close them and run forget again, or add --force")
    return 0


WATCHDOG_STEPS = """One-time setup, so a cloud task can take over when this machine goes quiet:

1. Connect GitHub: install the Claude GitHub App at claude.ai/code, or run /web-setup in Claude Code.
   On a Team plan an Owner must first enable the GitHub connector.
2. almanac config set cloud.auto on      (then restart almanac)
3. almanac cloud on                      (in this repository; no other repository takes part)
   This repository is {here}.
4. In Claude Code, in this repository, run /schedule and create an hourly task with the prompt below
   (or create it at claude.ai/code/routines).
5. Put the same secret in that task's environment, so it can open the snapshot (the branch holds one file,
   encrypted with it):
   ALMANAC_HANDOFF_KEY={key}
   (keep it private: anyone with it can read the snapshot and write one the task will act on)

The prompt for the scheduled task:

{prompt}
"""


def _cloud(rest):
    import os

    from . import cloud

    parser = argparse.ArgumentParser(prog="almanac cloud")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("packet", "send"):
        sub.add_parser(name).add_argument("--conversation", type=int, help="default: the latest one of this folder's project")
    sub.choices["send"].add_argument("--yes", action="store_true", help="send without asking")
    for name in ("status", "watchdog", "on", "off"):
        sub.add_parser(name)
    routine = sub.add_parser("routine")
    routine.add_argument("action", choices=["check"])
    routine.add_argument("--login", help="Claude config folder whose login is used (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    args = parser.parse_args(rest)
    memory, cwd, cfg = _memory_handle(), os.getcwd(), config.load()
    if args.cmd == "routine":
        from . import accounts
        tok = accounts.token(args.login or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
        if not cfg["cloud"]["routine"] or not tok:
            print("almanac: set cloud.routine to the routine's id, and use a login that is valid (--login FOLDER).", file=sys.stderr)
            return 2
        try:
            _print(cloud.routine_check(cfg["cloud"]["routine"], tok))
        except Exception as exc:  # the message holds a status or a network error, never the token
            print(f"almanac: could not read the routine: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd in ("on", "off"):
        text = cloud.turn(memory, cwd, args.cmd == "on")
        _print(text)
        return 2 if "cannot" in text else 0
    key = cloud.key_of(memory, cwd)
    listed = bool(key) and key in cfg["cloud"]["repos"]
    if args.cmd == "watchdog":
        here = "on" if listed else "off"
        _print(WATCHDOG_STEPS.format(prompt=cloud.watchdog(cfg["cloud"]["heartbeat_minutes"]), here=here, key=cloud.handoff_key()))
        return 0
    if args.cmd == "status":
        _print(cloud.status(cwd, cfg, memory.state, listed))
        return 0
    if args.conversation is None:
        found = memory.recent(project=memory.projects.see(cwd, touch=False), limit=1)
    else:
        found = memory.store.run("select id, title from convo where id = ?", (args.conversation,))
    text = cloud.fitted(memory, found[0]["id"], cwd) if found else ""
    if not text:
        print("almanac: no stored conversation to hand off" + (f" with id {args.conversation}" if args.conversation is not None
              else " for this folder's project") + "; see `almanac memory recent`", file=sys.stderr)
        return 2
    if args.cmd == "packet":
        _print(text)
        return 0
    exe = launcher.find_claude()
    if not exe:
        print("almanac: Claude Code was not found on PATH. Install it or set ALMANAC_CLAUDE to its path.", file=sys.stderr)
        return 2
    _print(f"almanac: handing conversation {found[0]['id']} ({found[0]['title']}) to a Claude Code cloud session")
    return cloud.send(exe, text, cwd, yes=args.yes)


def _semantic(rest):
    from . import semantic

    cmd = rest[0] if len(rest) == 1 else ""
    if cmd in ("on", "off"):
        config.set_value("memory.semantic", "true" if cmd == "on" else "false")
        print(f"memory.semantic = {'true' if cmd == 'on' else 'false'}")
    elif cmd == "install":
        if semantic.missing():
            print(f"almanac: missing packages: {', '.join(semantic.missing())}\n"
                  'Install them with: pip install "getalmanac[semantic] @ git+https://github.com/vssehdave/Almanac"', file=sys.stderr)
            return 1
        if semantic.present():
            print("The model is already downloaded.")
        else:
            print(f"Downloading {semantic.MODEL} (about 90 MB) from huggingface.co, once. This is the only time Almanac "
                  "contacts a host other than the upstream. No conversation text is sent.", flush=True)
            try:
                print(f"Downloaded {semantic.download() / 1e6:.0f} MB.")
            except Exception as exc:  # no network, a proxy, a full disk
                print(f"almanac: the download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
        if not semantic.load():
            print("almanac: the model is there but would not load; search stays keyword-based.", file=sys.stderr)
            return 1
        print("Ready. New exchanges get a vector as they are stored; `almanac semantic index` does the stored ones now.")
    elif cmd == "status":
        cfg, memory = config.load(), _memory_handle()
        found, model = semantic.missing(), semantic.present()
        done, waiting = memory.dense_counts(semantic.NAME)
        print(f"  packages   {'all installed' if not found else 'missing: ' + ', '.join(found)}")
        print(f"  model      {'downloaded' if model else 'not downloaded (almanac semantic install)'}")
        print(f"  setting    memory.semantic is {'on' if cfg['memory']['semantic'] else 'off'}")
        print(f"  searching  {'by meaning (' + semantic.NAME + ') and words' if semantic.active() else 'by words only (hashed vectors)'}")
        print(f"  vectors    {done} exchange(s) have one, {waiting} waiting")
    elif cmd == "index":
        if not semantic.active():
            print("almanac: no model is active. See `almanac semantic status`.", file=sys.stderr)
            return 1
        memory = _memory_handle()
        done, waiting = memory.dense_counts(semantic.NAME)
        made = 0
        while made < waiting:
            step = memory.embed_pending(32)
            if not step:
                break
            made += step
            print(f"\rembedded {made} of {waiting}", end="", flush=True)
        print(f"\rembedded {made} of {waiting} exchange(s)")
        return 0 if made >= waiting else 1
    else:
        print("usage: almanac semantic [install | status | index | on | off]", file=sys.stderr)
        return 2
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


def _route(rest):
    from . import routing

    cfg, memory = config.load(), _memory_handle()
    project = memory.projects.see(os.getcwd(), touch=False)
    store = memory.state
    print(f"  project    {project or 'unknown'}  (a pin applies only if the proxy works out this same project)")
    if rest[:1] == ["pin"] and len(rest) == 2 and rest[1] in (*routing.TIERS, "off"):
        if not project:
            print("almanac: this folder is not a known project yet; run Claude Code here once first", file=sys.stderr)
            return 2
        if rest[1] == "off":
            store.run("delete from state where key = ?", (routing.pin_key(project),))
            print("pin lifted")
        else:
            store.put(routing.pin_key(project), rest[1])
            print(f"this project is pinned to {rest[1]} ({cfg['routing']['models'][rest[1]]}) until `almanac route pin off`")
        return 0
    if rest not in ([], ["status"]):
        print("usage: almanac route [status | pin small|medium|large|off]", file=sys.stderr)
        return 2
    route = cfg["routing"]
    pin = store.get(routing.pin_key(project)) if project else None
    print(f"  mode       {route['mode']}  (classifier: {route['classifier']}, ceiling: {route['ceiling']})")
    print("  models     " + ", ".join(f"{t} {route['models'][t]}" for t in routing.TIERS))
    print(f"  pin        {pin or 'none'} (this project)")
    outside = store.get("route:outside")
    if outside and outside not in route["models"].values():
        print(f"  not routed {outside} is not in routing.models, so its threads keep it")
    for tier, why in routing.bad_tiers(store).items():
        print(f"  unavailable {tier}: {why}")
    log = store.get("route:log", [])[-10:]
    print("  last decisions:" if log else "  no decisions yet")
    for d in log:
        outcome = f"NOT applied ({d['skip']})" if d.get("skip") else ("kept" if d.get("tier") == d.get("from", d.get("tier")) else "applied")
        print(f"    {time.strftime('%H:%M:%S', time.localtime(d['ts']))} {d['kind']:<4} {d['tier']:<6} {d['move']:<8} {outcome}: "
              f"{d['why']}")
    return 0


def _cheatsheet(rest):
    from . import cheatsheet
    from .compact import CHARS_PER_TOKEN

    if rest[:1] == ["path"] and len(rest) == 1:
        print(cheatsheet.override_dir())
    elif rest[:1] == ["show"] and len(rest) <= 2 and all(name in (*cheatsheet.FAMILIES, "agent") for name in rest[1:]):
        for name in rest[1:] or cheatsheet.FAMILIES:
            sub = name == "agent"  # a sub-agent's text, shown for Opus (with the skills) and Haiku (without)
            for label, model in (("agent, Opus", "opus"), ("agent, Haiku", "haiku")) if sub else ((name, name),):
                text = cheatsheet.text_for(model, sub)
                _print(f"--- {label}: about {round(len(text) / CHARS_PER_TOKEN)} tokens ---\n{text}\n")
    else:
        print("usage: almanac cheatsheet [show [opus|sonnet|haiku|fable|agent] | path]", file=sys.stderr)
        return 2
    return 0
def _skill(rest):
    from . import skills

    parser = argparse.ArgumentParser(prog="almanac skill")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").add_argument("target", help="a local folder or SKILL.md, or an https:// git URL")
    add = sub.add_parser("add")
    add.add_argument("source", help="an https:// git URL or a local folder")
    add.add_argument("--skill", action="append", help="install only this skill folder (repeatable)")
    add.add_argument("--ref", help="a full commit SHA to fetch instead of the default branch")
    add.add_argument("--accept-caution", action="store_true", help="install even when the verdict is CAUTION")
    add.add_argument("--accept-danger", action="store_true",
                     help="owner override: after the findings, offer to install a DO_NOT_INSTALL item (type yes); implies --accept-caution. A failed scan is still refused. Exit code stays 2 after a successful override")
    add.add_argument("--replace", action="store_true", help="overwrite a skill with the same name")
    add.add_argument("--bundle", action="store_true", help="install as a plugin in your Almanac bundle (folder, manifest, SOURCES.md row)")
    add.add_argument("--hooks", action="store_true", help="with --bundle: keep the plugin's hooks (they run code in every session)")
    add.add_argument("--name", help="with --bundle: the folder and plugin name (default: the source's name)")
    sub.add_parser("import-bundle").add_argument("path", help="a folder of plugin folders, such as an old copy of the bundle")
    sub.add_parser("accept").add_argument("item", help="an installed skill or plugin, by name or path")
    sub.add_parser("unaccept").add_argument("item")
    sub.add_parser("integrate").add_argument("name", help="a plugin folder in your bundle")
    sub.add_parser("update").add_argument("name", nargs="?", help="check only this adopted skill (its folder name in SOURCES.md)")
    audit = sub.add_parser("audit")
    audit.add_argument("--again", action="store_true", help="scan everything, even unchanged items")
    audit.add_argument("--pending", type=int, help=argparse.SUPPRESS)  # the background scan started at launch
    args = parser.parse_args(rest)
    if args.cmd in ("accept", "unaccept"):
        return getattr(skills, args.cmd)(args.item, out=_print)
    if args.cmd == "import-bundle":
        return skills.import_bundle(args.path, out=_print)
    if args.cmd == "integrate":
        return skills.integrate(args.name, out=_print)
    if args.cmd == "update":
        return skills.update(args.name, out=_print)
    if args.cmd == "audit":
        return skills.audit(args.pending, args.again, out=_print)
    if args.cmd == "add":
        ask = _yes if sys.stdin.isatty() else None
        skills.offer_scanner(ask, out=_print)
        if args.accept_danger and not ask:
            _print("almanac: --accept-danger needs a terminal to type yes in; a DO_NOT_INSTALL item will be refused.")
        return skills.add(args.source, args.skill, args.ref, args.accept_caution or args.accept_danger, args.replace,
                          out=_print, bundle=args.bundle, name=args.name, ask=ask, hooks=args.hooks,
                          confirm=_typed_yes if args.accept_danger and ask else None)
    if os.path.exists(args.target):
        res = skills.scan(args.target)
    else:
        box = skills.quarantine()
        try:
            res = skills.scan(skills.fetch(args.target, None, box)[0])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"almanac: could not fetch {args.target}: {exc}", file=sys.stderr)
            return skills.ERROR
        finally:
            skills.remove(box)
    _print(skills.render(res, args.target))
    return skills.code(res)


def _pack(rest):
    from . import skills

    parser = argparse.ArgumentParser(prog="almanac pack")
    parser.add_argument("--file", help="a pack.json other than the one in the Almanac folder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    install = sub.add_parser("install")
    install.add_argument("name", nargs="?", help="one entry; default: all of them")
    install.add_argument("--accept-caution", action="store_true")
    install.add_argument("--replace", action="store_true")
    args = parser.parse_args(rest)
    try:
        entries = skills.load_pack(args.file)
    except ValueError as exc:
        print(f"almanac: {exc}", file=sys.stderr)
        return 2
    if not entries:
        print(f"almanac: no entries in {skills.pack_file(args.file)}; pass --file PATH", file=sys.stderr)
        return 2
    if args.cmd == "list":
        for e in entries:
            _print(f"{e['name']:<20} {e['kind']:<8} {e.get('source', '')} @ {str(e.get('ref', ''))[:12]}\n    {e.get('note', '')}")
        return 0
    if args.name:
        entries = [e for e in entries if e["name"] == args.name]
        if not entries:
            print(f"almanac: the pack has no entry called {args.name!r}; see `almanac pack list`", file=sys.stderr)
            return 2
    return skills.pack_install(entries, args.accept_caution, args.replace, out=_print)


def _playbook(rest):
    """`almanac playbook ...` is the getplaybook command, on Almanac's playbooks (a library of repeated prompts feeds its miner)."""
    from getplaybook import cli as playbook_cli

    from . import _pbind  # noqa: F401

    return playbook_cli.main(rest)


def _library(rest):
    from . import library

    parser = argparse.ArgumentParser(prog="almanac library")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--project", help="a project name or key; default: all projects")
    for name in ("show", "forget"):
        sub.add_parser(name).add_argument("id", type=int)
    args = parser.parse_args(rest)
    memory = _memory_handle()
    if args.cmd == "list":
        key = None
        if args.project:
            key = memory.projects.find(args.project)
            if not key:
                print(f"almanac: no project is called {args.project!r}; see `almanac projects`", file=sys.stderr)
                return 2
        entries = library.listing(memory.store, key, library.min_tools())
        for e in entries:
            _print(f"{e['id']:>5}  {time.strftime('%Y-%m-%d', time.localtime(e['created']))}  {e['uses']:>4}  {e['prompt'][:70]}")
        if not entries:
            print(f"No entry with at least {library.min_tools()} tool calls is stored for that "
                  "(library.min_tools; `almanac library show ID` still opens a smaller one).")
        return 0
    if args.cmd == "show":
        text = library.show(memory, args.id)
    else:
        gone = library.forget(memory.store, args.id)
        text = f"forgot {len(gone)} entr{'y' if len(gone) == 1 else 'ies'}: {', '.join(map(str, gone))}" if gone else None
    if text is None:
        print(f"almanac: no entry {args.id}; see `almanac library list`", file=sys.stderr)
        return 2
    _print(text)
    return 0


def _lessons(rest):
    from . import lessons

    parser = argparse.ArgumentParser(prog="almanac lessons")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("clear").add_argument("agent", help="the agent type whose lessons are removed")
    args = parser.parse_args(rest)
    state = _memory_handle().state
    if args.cmd == "clear":
        if not lessons.load(state, args.agent):
            print(f"almanac: no lessons for {args.agent!r}; see `almanac lessons`", file=sys.stderr)
            return 2
        state.run("delete from state where key = ?", ("lesson:" + args.agent,))
        print(f"cleared the lessons for {args.agent} (threads already running keep theirs)")
        return 0
    kept = state.scan("lesson:")
    for key in sorted(kept):
        if kept[key]:
            _print(lessons.text(state, key.partition(":")[2]) + "\n")
    if not any(kept.values()):
        print("No lessons yet: they come from memory_review verdicts that report a problem.")
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


def _sandbox(rest):
    from . import sandbox

    return sandbox.run(rest)


def _yes(question) -> bool:
    """A yes typed at the terminal. No answer is a no: on Windows an empty input such as NUL still says it is a terminal."""
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _typed_yes(question) -> bool:
    """The strict form for DO_NOT_INSTALL: exactly "yes", in lower case, and nothing else."""
    try:
        return input(question).strip() == "yes"
    except EOFError:
        return False


def _review_gate(_rest):
    """Claude Code's Stop hook: hold the turn open while an Opus main thread has unreviewed sub-agent work. Never fails."""
    try:
        cfg = config.load()
        if not profile.review_gate_on(cfg):
            return 0
        session = str((json.loads(sys.stdin.read() or "{}") or {}).get("session_id") or "")
        reason = _memory_handle().gate(session) if session else ""
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
    except Exception as exc:  # a broken gate must never stop Claude Code from finishing
        report.log_error("review-gate", exc)
    return 0


def _banner(_rest):
    """Claude Code's SessionStart hook: show the person a memory summary. Never fails, prints nothing when there is none."""
    try:
        text = _memory_handle().banner(os.getcwd()) if profile.banner_on(config.load()) else ""
        if text:
            print(json.dumps({"systemMessage": text}))
    except Exception as exc:  # a broken banner must never block a session start
        report.log_error("banner", exc)
    return 0


def _eval(rest):
    from . import evaluate

    return evaluate.main(rest)


def _version(_rest):
    print(f"almanac {__version__}")
    return 0


COMMANDS = {"sandbox": _sandbox, "serve": _serve, "report": _report, "errors": _errors, "status": _status, "doctor": _doctor, "config": _config,
            "version": _version, "memory": _memory, "projects": _projects, "route": _route, "cheatsheet": _cheatsheet, "eval": _eval, "mcp": _mcp,
            "semantic": _semantic, "cloud": _cloud, "skill": _skill, "pack": _pack,
            "playbook": _playbook, "library": _library, "lessons": _lessons, "review-gate": _review_gate}


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
