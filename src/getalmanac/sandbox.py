"""`almanac sandbox`: a plain Claude Code in its own config folder, for trying plugins you are building.

No proxy, none of your plugins, hooks or skills: the folder starts empty and only the plugin folders named with
`--plugin` are loaded. Your login is separate too: run /login once inside it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from . import launcher

USAGE = """almanac sandbox [--plugin PATH ...] [--list] [-- claude arguments]

  --plugin PATH   load the plugin in that folder (repeat for more); it is loaded for this run only
  --list          show what would be loaded, and start nothing
  --              everything after this goes to Claude Code as it is

The sandbox keeps its own settings, login, history and plugins in ~/.claude-sandbox
(ALMANAC_SANDBOX sets another folder). Run /login in it once."""

CLEARED = ("ANTHROPIC_BASE_URL", "ALMANAC_PROXY", "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL", "ALMANAC_HOME",
           "PLAYBOOK_CONFIG")


def folder() -> Path:
    return Path(os.environ.get("ALMANAC_SANDBOX") or Path.home() / ".claude-sandbox")


def source_root(plugin: str):
    """The nearest `src` folder above a plugin folder (where its package code lives), or None."""
    return next((str(p) for p in Path(plugin).parents if p.name == "src"), None)


def protected() -> list:
    """Config folders the sandbox must never point Claude Code at: the real ones."""
    home = Path.home()
    named = [home / ".claude", home / ".claude-work"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        named.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    return [p.resolve() for p in named]


def parse(rest):
    """(plugin folders, list only, claude arguments), or raises ValueError."""
    plugins, listing, args, i = [], False, [], 0
    while i < len(rest):
        item = rest[i]
        if item == "--":
            args = rest[i + 1:]
            break
        if item == "--list":
            listing = True
        elif item == "--plugin" and i + 1 < len(rest):
            plugins.append(str(Path(rest[i + 1]).expanduser().resolve()))
            i += 1
        else:
            raise ValueError(f"unknown argument {item!r}")
        i += 1
    return plugins, listing, args


def plugin_name(path: str):
    """The plugin's name from its manifest, or None when the folder is not a plugin."""
    try:
        return json.loads((Path(path) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("name") or Path(path).name
    except (OSError, ValueError, AttributeError):
        return None


def command(exe, plugins, args):
    return [exe, *[x for p in plugins for x in ("--plugin-dir", p)], *args]


def environment(parent=None, plugins=()) -> dict:
    env = dict(os.environ if parent is None else parent)
    for name in CLEARED:
        env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = str(folder())
    env["PLAYBOOK_HOME"] = str(folder() / "playbook")
    roots = [r for r in map(source_root, plugins) if r]
    if roots:  # first on sys.path, so `playbook` run from PATH imports the plugin's own code
        env["PYTHONPATH"] = os.pathsep.join([*roots, *filter(None, [env.get("PYTHONPATH")])])
    return env


def needs_window(args) -> bool:
    return not (sys.stdin.isatty() and sys.stdout.isatty()) and not {"-p", "--print"} & set(args)


def run(rest) -> int:
    if rest[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    try:
        plugins, listing, args = parse(rest)
    except ValueError as exc:
        print(f"almanac sandbox: {exc}\n\n{USAGE}", file=sys.stderr)
        return 2
    where = folder()
    if where.resolve() in protected():
        print(f"almanac sandbox: {where} is a real Claude Code folder; the sandbox needs its own.", file=sys.stderr)
        return 2
    bad = [p for p in plugins if plugin_name(p) is None]
    if bad:
        print("almanac sandbox: not a plugin folder (no .claude-plugin/plugin.json): " + ", ".join(bad), file=sys.stderr)
        return 2
    exe = launcher.find_claude()
    login = (where / ".credentials.json").exists()
    if listing:
        print(f"config folder  {where}" + ("" if where.exists() else "  (made on first use)"))
        print(f"login          {'found' if login else 'none yet: run /login inside the sandbox once'}")
        print(f"claude         {exe or 'not found'}")
        print("plugins        " + ("; ".join(f"{plugin_name(p)} ({p})" for p in plugins) if plugins else "none (only Claude Code's own)"))
        print(f"playbook       {where / 'playbook'}")
        print("plugin code    " + ("; ".join(f"{plugin_name(p)} from {source_root(p) or 'installed packages'}" for p in plugins) if plugins else "none"))
        print("proxy          none (Almanac is not in the path)")
        print("command        " + " ".join(command(exe or "claude", plugins, args)))
        return 0
    if not exe:
        print("almanac sandbox: Claude Code was not found. Install it or set ALMANAC_CLAUDE.", file=sys.stderr)
        return 127
    where.mkdir(parents=True, exist_ok=True)
    if not login:
        print(f"almanac sandbox: first run in {where}: type /login once.", file=sys.stderr)
    env = environment(plugins=plugins)
    if needs_window(args):
        if sys.platform != "win32":
            print("almanac sandbox: no terminal here; run it in a real terminal.", file=sys.stderr)
            return 2
        subprocess.Popen(command(exe, plugins, args), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        print("almanac sandbox: no terminal here, opened the sandbox in a new window.")
        return 0
    return subprocess.call(command(exe, plugins, args), env=env)
