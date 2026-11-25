"""Start the proxy, then run Claude Code pointed at it."""
import hmac
import http.client
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import config
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


def own_context(env: dict, cfg) -> dict:
    """Switch off Claude Code's own auto-compaction, when Almanac keeps the context (compaction on, `own_the_context`)."""
    if cfg and cfg["compaction"]["enabled"] and cfg["compaction"]["own_the_context"]:
        env["DISABLE_AUTO_COMPACT"] = "1"
    return env


def child_env(url: str, parent=None, cfg=None) -> dict:
    """Claude Code's environment. Its login is left alone: no key or token is set here."""
    env = own_context(dict(os.environ if parent is None else parent), cfg)
    env["ANTHROPIC_BASE_URL"] = url
    # A custom base URL makes Claude Code hide model features it cannot verify through
    # a gateway. This relay changes none of them, so say it is the first-party API.
    env["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"] = "1"
    env["ALMANAC_PYTHON"] = Path(sys.executable).as_posix()  # the guard plugin's hooks run with this interpreter
    env["ALMANAC_PROXY"] = url  # also tells the Playbook plugin's hooks to stay silent: the proxy places the offers
    env["PLAYBOOK_HOME"], env["PLAYBOOK_CONFIG"] = str(config.home()), str(config.config_path())  # its server shares the playbooks
    # Behind a corporate proxy Claude Code would send its requests for this loopback address to that proxy.
    keep = (env.pop("no_proxy", "") + "," + env.get("NO_PROXY", "")).replace(" ", "").split(",")
    env["NO_PROXY"] = ",".join(dict.fromkeys([h for h in keep if h] + ["localhost", "127.0.0.1", "::1"]))
    return env


def with_memory_tools(args, cfg):
    """Add the memory tools' MCP server to Claude Code's arguments, unless turned off."""
    memory = cfg["memory"]["enabled"] and cfg["memory"]["tools"]
    if not (memory or cfg.get("playbooks", {}).get("enabled")) or "--strict-mcp-config" in args:
        return args
    from . import mcp

    path = config.home() / "mcp.json"
    try:
        path.write_text(json.dumps(mcp.client_config(cfg), indent=2), encoding="utf-8")
    except OSError:
        return args
    return ["--mcp-config", str(path), *args]


def with_bundle(args, cfg):
    """Add Almanac's own plugin and every plugin in the user's bundle (`<almanac home>/bundle/`) to Claude Code's arguments."""
    if not cfg["bundle"]["enabled"]:
        return args
    here = Path(__file__).parent / "bundle"
    own = [here / "almanac"] + ([here / "guard"] if cfg.get("guard", {}).get("enabled", True) else [])
    mine = config.home() / "bundle"
    folders = [p for p in own if p.is_dir()]
    if mine.is_dir():
        folders += sorted(p for p in mine.iterdir() if p.name not in ("almanac", "guard") and (p / ".claude-plugin" / "plugin.json").is_file())
    return [arg for folder in folders for arg in ("--plugin-dir", str(folder))] + list(args)


def with_profile(args, cfg):
    """Add the clean profile (the user's own setup minus the off plugins, hooks and skills) to Claude Code's arguments."""
    from . import profile

    try:
        flags, notes = profile.prepare(cfg, args)
    except Exception as exc:  # a broken profile must not stop Claude Code, but it must be said
        flags, notes = [], [f"the clean profile is off for this run: {type(exc).__name__}: {exc}"]
    for note in notes:
        print(f"almanac: {note}", file=sys.stderr)
    return [*flags, *args]


def https_session(cfg):
    """A tls.Session when this launch should run in HTTPS-proxy mode, else None (and says why)."""
    asked = os.environ.get("ALMANAC_HTTPS", "").lower()
    if asked in ("0", "false", "no", "off") or not (cfg["remote_control"] or asked in ("1", "true", "yes", "on")):
        return None
    from urllib.parse import urlsplit

    from . import tls, upstream

    hop = upstream.find(cfg, urlsplit(cfg["upstream"]).hostname or "")  # the user's own proxy, before the child's is set
    if hop and not hop.supported:
        reason = f"the {hop.scheme}:// proxy from {hop.source} is not supported"
    elif urlsplit(cfg["upstream"]).scheme != "https":
        reason = "the upstream is not https"
    else:
        try:
            return tls.Session(urlsplit(cfg["upstream"]).hostname, extra_ca=os.environ.get("NODE_EXTRA_CA_CERTS"))
        except (RuntimeError, OSError) as exc:
            reason = str(exc)
    print(f"almanac: HTTPS-proxy mode is off for this session ({reason}); Remote Control will not be offered.",
          file=sys.stderr)
    return None


def https_env(session, port: int, parent=None, cfg=None) -> dict:
    """Claude Code's environment in HTTPS-proxy mode: its base URL untouched, this proxy as HTTPS_PROXY."""
    env = own_context(dict(os.environ if parent is None else parent), cfg)
    for name in ("ANTHROPIC_BASE_URL", "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL", "https_proxy", "no_proxy"):
        env.pop(name, None)
    env["HTTPS_PROXY"] = session.proxy_url(port)
    env["NODE_EXTRA_CA_CERTS"] = str(session.bundle)
    # Local servers (MCP, memory workers) are reached directly; nothing may bypass the proxy for the upstream.
    current = (os.environ if parent is None else parent).get("NO_PROXY") or ""
    keep = [h for h in current.replace(" ", "").split(",") if h and h != "*" and not session.host.endswith(h.lstrip("*."))]
    env["NO_PROXY"] = ",".join(dict.fromkeys(keep + ["localhost", "127.0.0.1", "::1"]))
    env["ALMANAC_PYTHON"] = Path(sys.executable).as_posix()
    env["ALMANAC_PROXY"] = f"http://127.0.0.1:{port}"
    env["PLAYBOOK_HOME"], env["PLAYBOOK_CONFIG"] = str(config.home()), str(config.config_path())
    return env


def serve_file():
    return config.home() / "serve.json"


def publish(port: int, token: str, key: str):
    """Tell `almanac` where `almanac serve` listens, the address secret and the key that proves it. This user only."""
    # written beside it and moved into place, so a reader never finds the file half written
    draft = serve_file().with_name(f"serve.json.{os.getpid()}.tmp")
    fd = os.open(draft, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"port": port, "token": token, "key": key}, handle)
    os.replace(draft, serve_file())


def unpublish(token: str):
    """Remove serve.json, unless another `almanac serve` has written its own since."""
    try:
        if json.loads(serve_file().read_text(encoding="utf-8")).get("token") == token:
            serve_file().unlink()
    except (OSError, ValueError, AttributeError):
        pass


def shared(port: int):
    """The address of a running `almanac serve` of this version on `port`, or None.

    Something answering on the port is not enough: it must prove it holds the key in serve.json
    by answering a random challenge. The key is never sent, and unlike the token it is in no request path.
    """
    from . import __version__

    try:
        info = json.loads(serve_file().read_text(encoding="utf-8"))
        token, key = info["token"], info["key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if info.get("port") != port or not all(isinstance(x, str) and x for x in (token, key)):
        return None
    challenge = secrets.token_hex(16)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        conn.request("GET", f"/almanac/health?challenge={challenge}")
        answer = json.loads(conn.getresponse().read(65536))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if not isinstance(answer, dict) or answer.get("almanac") != __version__ or not isinstance(answer.get("proof"), str):
        return None
    want = hmac.new(key.encode(), challenge.encode(), "sha256").hexdigest()
    return f"http://127.0.0.1:{port}/{token}" if hmac.compare_digest(answer["proof"].encode("utf-8", "replace"), want.encode()) else None


def start(cfg):
    """Start a proxy on a free loopback port. Returns (proxy, server, url)."""
    proxy = proxy_mod.build(cfg)
    server = proxy.serve()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return proxy, server, f"http://127.0.0.1:{server.server_address[1]}/{proxy.token}"


def run(args, cfg) -> int:
    exe = find_claude()
    if not exe:
        print("almanac: Claude Code was not found. Install it (https://code.claude.com/docs/en/setup) "
              "or set ALMANAC_CLAUDE to its path.", file=sys.stderr)
        return 127
    try:
        from . import skills

        skills.launch_check(cfg)
    except Exception as exc:  # a broken check must never stop Claude Code from starting
        print(f"almanac: the skill check failed: {exc}", file=sys.stderr)
    try:
        from getplaybook import build

        from . import _pbind  # noqa: F401

        build.launch_check(cfg)
        build.keep_checking()
    except Exception:  # the playbook miner is an extra: it must never stop Claude Code from starting
        pass
    # A base URL the user already set (a company gateway) becomes the upstream.
    outer = os.environ.get("ANTHROPIC_BASE_URL")
    if outer and not os.environ.get("ALMANAC_UPSTREAM"):
        cfg["upstream"] = outer
    # A proxy already running (`almanac serve`) is shared: it outlives this command, so
    # background sessions started from Claude Code keep a working address.
    session = https_session(cfg) if not outer else None
    url = shared(cfg["port"]) if not (outer or session) else None
    proxy = server = None
    if not url:
        proxy, server, url = start(cfg)
        proxy.tls = session
    env = https_env(session, server.server_address[1], cfg=cfg) if session else child_env(url, cfg=cfg)
    args = with_memory_tools(list(args), cfg)
    args = with_bundle(args, cfg)
    args = with_profile(args, cfg)
    # Ctrl+C belongs to Claude Code. A handler (not SIG_IGN) so the child does not inherit it.
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        return subprocess.call([exe, *args], env=env)
    finally:
        signal.signal(signal.SIGINT, previous)
        if proxy:
            proxy.keepalive.stop_all()
            proxy.drain(60)  # summary calls may still be running
            for layer in proxy.layers:
                getattr(layer, "close", lambda: None)()
            server.shutdown()
        if session:
            session.close()
