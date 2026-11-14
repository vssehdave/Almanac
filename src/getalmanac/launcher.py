"""Start the proxy, then run Claude Code pointed at it."""
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import proxy as proxy_mod


def find_claude():
    """The Claude Code executable, or None."""
    override = os.environ.get("ALMANAC_CLAUDE")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for cand in (home / ".local/bin/claude", home / ".local/bin/claude.exe", home / ".claude/local/claude"):
        if cand.exists():
            return str(cand)
    return None


def child_env(url: str, parent=None) -> dict:
    """Claude Code's environment. Its login is left alone: no key or token is set here."""
    env = dict(os.environ if parent is None else parent)
    env["ANTHROPIC_BASE_URL"] = url
    # A custom base URL makes Claude Code hide model features it cannot verify through
    # a gateway. This relay changes none of them, so say it is the first-party API.
    env["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"] = "1"
    env["ALMANAC_PROXY"] = url
    return env


def start(cfg):
    """Start a proxy on a free loopback port. Returns (proxy, server, url)."""
    proxy = proxy_mod.build(cfg)
    server = proxy.serve()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return proxy, server, f"http://127.0.0.1:{server.server_address[1]}"


def run(args, cfg) -> int:
    exe = find_claude()
    if not exe:
        print("almanac: Claude Code was not found. Install it (https://code.claude.com/docs/en/setup) "
              "or set ALMANAC_CLAUDE to its path.", file=sys.stderr)
        return 127
    # A base URL the user already set (a company gateway) becomes the upstream.
    outer = os.environ.get("ANTHROPIC_BASE_URL")
    if outer and not os.environ.get("ALMANAC_UPSTREAM"):
        cfg["upstream"] = outer
    proxy, server, url = start(cfg)
    # Ctrl+C belongs to Claude Code. A handler (not SIG_IGN) so the child does not inherit it.
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        return subprocess.call([exe, *args], env=child_env(url))
    finally:
        signal.signal(signal.SIGINT, previous)
        proxy.drain()
        server.shutdown()
