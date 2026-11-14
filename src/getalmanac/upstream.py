"""Reaching the upstream through a corporate HTTP proxy, when there is one.

The proxy comes from the `upstream_proxy` setting, else the environment
(HTTPS_PROXY, then ALL_PROXY), unless NO_PROXY names the host. Only `http://`
proxies (the usual CONNECT kind) are supported. A login in the proxy URL is
sent as Proxy-Authorization and kept in memory only. Company TLS inspection
needs the company root CA: the `upstream_ca` setting, SSL_CERT_FILE and
NODE_EXTRA_CA_CERTS are trusted in addition to the system's. Verification is
never turned off.
"""
import base64
import os
import socket
import ssl
from urllib.parse import unquote, urlsplit

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
CA_VARS = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")


class Hop:
    """A proxy to go through. Its login lives here, in memory, and is never printed."""

    def __init__(self, url, source):
        parts = urlsplit(url if "://" in url else "http://" + url)
        self.source, self.scheme, self.headers = source, parts.scheme.lower(), {}
        try:
            self.host, self.port = parts.hostname or "", parts.port or 80
        except ValueError:
            self.host, self.port = "", 80
        if parts.username is not None:
            login = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
            self.headers["Proxy-Authorization"] = "Basic " + base64.b64encode(login.encode()).decode()

    @property
    def supported(self):
        return self.scheme == "http" and bool(self.host)

    @property
    def label(self):
        return f"{self.host}:{self.port}"

    def __repr__(self):
        return f"Hop({self.label})"


def hide(url):
    """A proxy URL without its login, for any output."""
    return f"{Hop(url, '').scheme}://{Hop(url, '').label}" if url else ""


def bypassed(host, env):
    entries = (env.get("NO_PROXY") or env.get("no_proxy") or "").lower().replace(" ", "").split(",")
    return "*" in entries or any(e and (host == e or host.endswith("." + e)) for e in (x.lstrip("*.") for x in entries))


def find(cfg, host, env=None):
    """The Hop to reach `host` through, or None to connect directly (an unsupported one is still returned)."""
    env = os.environ if env is None else env
    if bypassed(host.lower(), env):
        return None
    mine = urlsplit(env.get("ALMANAC_PROXY") or "")  # in HTTPS-proxy mode that is this very process
    for url, source in [(cfg["upstream_proxy"], "config upstream_proxy")] + [(env.get(n), f"environment {n}") for n in PROXY_VARS]:
        if url:
            hop = Hop(url, source)
            if (hop.host, hop.port) != (mine.hostname, mine.port):
                return hop
    return None


def open_socket(hop, host, port, timeout=30):
    """A connected socket to host:port, through `hop` with CONNECT when there is one."""
    if hop is None:
        return socket.create_connection((host, port), timeout=timeout)
    sock = socket.create_connection((hop.host, hop.port), timeout=timeout)
    try:
        head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in hop.headers.items())
        sock.sendall(head.encode("latin-1") + b"\r\n")
        reply = b""
        while not reply.endswith(b"\r\n\r\n") and len(reply) < 65536:  # one byte at a time: nothing past the headers is read
            byte = sock.recv(1)
            if not byte:
                break
            reply += byte
        line = (reply.decode("latin-1").splitlines() or [""])[0]
        if not line.split()[1:2]:
            raise OSError(f"the proxy {hop.label} gave no answer to CONNECT")
        if not line.split()[1].startswith("2"):
            raise OSError(f"the proxy {hop.label} refused CONNECT: {line}")
    except Exception:
        sock.close()
        raise
    return sock


def load_cas(ctx, cfg, env=None):
    """Trust the extra CA files that are set. Returns (loaded, failed) paths."""
    env = os.environ if env is None else env
    loaded, failed = [], []
    for path in (cfg["upstream_ca"], *(env.get(n) for n in CA_VARS)):
        if path:
            try:
                ctx.load_verify_locations(cafile=path)
                loaded.append(path)
            except (OSError, ssl.SSLError):
                failed.append(path)
    return loaded, failed


def context(cfg, env=None):
    ctx = ssl.create_default_context()
    load_cas(ctx, cfg, env)
    return ctx
