"""The loopback server: relay everything, rewrite only what the layers ask for.

Headers and the query string go to the upstream as received. Credentials are
never read, stored or logged. Only the JSON body of a Claude Code model request
is ever changed, and if a layer fails or the upstream rejects the changed body,
the original bytes are sent instead.
"""
import http.client
import hmac
import http.server
import io
import json
import os
import queue
import re
import secrets
import select
import socket
import socketserver
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import accounts, config
from . import request as rq
from . import sse
from . import upstream
from .memory import redact
from .report import Log, cache_marks, sections

REQUEST_SKIP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
                "proxy-authorization", "proxy-connection", "content-length"}
RESPONSE_SKIP = {"connection", "keep-alive", "transfer-encoding"}
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
ROUTES = {"/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens"}
SIZE_ERROR = re.compile(r"prompt is too long|exceed context limit", re.I)  # a size refusal, not a bad rewrite
SIZE_LIMIT = re.compile(r"prompt is too long: \d+ tokens > (\d+) maximum")  # the window the upstream really gives
TEE_LIMIT = 16 * 1024 * 1024
UPSTREAM_TIMEOUT = 900
CONNECT_TIMEOUT = 20
PING_EVERY = 15  # seconds between pings (and connect retries) while a stream waits for the network
SILENT_LIMIT = 540  # a waiting non-stream request answers before the client's 600 s header wait ends
PING = b'event: ping\ndata: {"type": "ping"}\n\n'
ROUTE_REFUSED = (400, 403, 404)  # what an unknown, unavailable or unsupported model comes back as


def _tell(proxy, method, *args):
    """Call `method` on every layer that has it. A failure here must never drop the request."""
    for layer in proxy.layers:
        if hasattr(layer, method):
            try:
                getattr(layer, method)(*args)
            except Exception:
                pass


class Replay:
    """A reply whose body was read to inspect it, served to the client as if it had not been."""

    def __init__(self, resp, body):
        self.status, self.reason, self._headers, self._body = resp.status, resp.reason, resp.getheaders(), io.BytesIO(body)

    def getheaders(self):
        return self._headers

    def getheader(self, name, default=None):
        return next((v for k, v in self._headers if k.lower() == name.lower()), default)

    def read(self):
        return self._body.read()

    def read1(self, size):
        return self._body.read(size)


class Ctx:
    """What the layers know about one request.

    `headers` holds the request's own headers, login included, for background jobs that call
    the upstream as the same user. It lives in memory until the job ends and is never written.
    """

    def __init__(self, proxy, headers, kind, sent=None):
        self.cfg, self.store, self.kind, self.proxy, self.headers = proxy.cfg, proxy.store, kind, proxy, dict(sent or {})
        self.session = headers.get("x-claude-code-session-id") or ""
        self.agent = headers.get("x-claude-code-agent-id") or headers.get("x-claude-code-parent-agent-id") or ""
        self.klass = headers.get("x-claude-code-request-class") or ""
        self.agent_type = headers.get("x-claude-code-agent-type") or ""
        self.parent = headers.get("x-claude-code-parent-agent-id") or ""
        self.lane = self.model = self.cwd = self.project = ""
        self.frozen_lane = ""  # where startup decisions are kept: a fork reads its parent's, so both send the same bytes
        self.stats = {}
        self.after = None  # the rewritten body, when a layer changed it
        self.orig = None  # the body as Claude Code sent it, for layers that decide from the whole history


class Hold:
    """A model request waiting for the network to return, and for how long it may."""

    def __init__(self, started, seconds, stream, stats):
        self.stream, self.stats, self.since, self.begun = stream, stats, 0, False  # begun: the client has its 200
        self.until = started + (seconds if stream else min(seconds, SILENT_LIMIT))


class Proxy:
    def __init__(self, cfg, store=None, layers=(), log=None):
        up = urlsplit(cfg["upstream"])
        if up.scheme not in ("http", "https") or not up.netloc:
            raise ValueError(f"upstream must be an http(s) URL, got {cfg['upstream']!r}")
        self.cfg, self.store, self.layers = cfg, store, list(layers)
        self.log = log or Log(config.home() / "logs" / "requests.jsonl")
        self.secure, self.netloc, self.base = up.scheme == "https", up.netloc, up.path.rstrip("/")
        self.host, self.port = up.hostname, up.port or (443 if self.secure else 80)
        # The environment as launched: HTTPS-proxy mode later points the child's proxy at this process.
        self.env = dict(os.environ)
        self.hop = self.hop_for(self.host)
        self.ctx = upstream.context(cfg, self.env) if self.secure else None
        self.token = secrets.token_urlsafe(16)  # the first path segment every client request must carry
        self.key = secrets.token_urlsafe(32)  # proves this process to `almanac`; never sent in any request
        self.last_seen = {}  # lane -> start time of its previous request
        self.fused = {}  # lane -> why its rewrites were switched off
        self.tls = None  # a tls.Session when running as an HTTPS proxy
        self.started, self.count = time.time(), 0
        self._jobs = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def hop_for(self, host):
        """The corporate proxy to reach `host` through, or None to connect directly."""
        hop = upstream.find(self.cfg, host, self.env)
        return hop if hop and hop.supported else None

    def connect(self):
        cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        kw = {"context": self.ctx} if self.secure else {}
        if not self.hop:
            return cls(self.netloc, timeout=UPSTREAM_TIMEOUT, **kw)
        conn = cls(self.hop.host, self.hop.port, timeout=UPSTREAM_TIMEOUT, **kw)
        conn.set_tunnel(self.host, self.port, self.hop.headers)
        return conn

    def is_fused(self, lane: str) -> bool:
        if lane not in self.fused and self.store is not None:
            kept = self.store.get(f"fused:{lane}")
            if kept:
                self.fused[lane] = kept
        return lane in self.fused

    def fuse(self, lane: str, why: str):
        """Stop rewriting one conversation, for good: the upstream refused its rewritten form."""
        self.fused[lane] = why
        if self.store is not None:
            try:
                self.store.put(f"fused:{lane}", why)
            except Exception:  # the mark only outlasts this run; a store error must not drop this reply
                pass

    def rewrite(self, raw: bytes, ctx: Ctx):
        """Return (bytes to send, parsed original or None)."""
        try:
            orig = json.loads(raw)
        except (ValueError, RecursionError):
            return raw, None
        if not isinstance(orig, dict) or not isinstance(orig.get("messages"), list):
            return raw, None
        try:
            ctx.model = str(orig.get("model") or "")
            ctx.lane = rq.lane_key(ctx.session, ctx.agent, orig["messages"])
            ctx.frozen_lane = rq.lane_key(ctx.session, ctx.parent, orig["messages"]) if ctx.agent_type.lower() == "fork" else ctx.lane
            ctx.cwd = rq.working_dir(orig)
            ctx.after, ctx.orig = None, orig
            if not self.layers or self.is_fused(ctx.lane) or self.is_fused(ctx.frozen_lane):
                return raw, orig
            work = json.loads(raw)
        except Exception as exc:  # fail open before any layer runs; nothing is indexed under a half-known lane
            ctx.stats["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return raw, None
        try:
            for layer in self.layers:
                layer.transform(work, ctx)
            if work == orig:
                return raw, orig
            sent = rq.dumps(work)
        except Exception as exc:  # fail open: a broken layer must never block a turn
            ctx.stats["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return raw, orig
        ctx.after = work
        return sent, orig

    def finish(self, ctx, orig, raw, sent, status, tee, started):
        reply = sse.parse(tee) if status == 200 and ctx.kind == "messages" and tee else None
        record = {"ts": round(started, 3), "ms": int((time.time() - started) * 1000), "kind": ctx.kind,
                  "session": ctx.session, "agent": ctx.agent[:16], "lane": ctx.lane, "class": ctx.klass,
                  "model": ctx.model, "status": status, "bytes_in": len(raw), "bytes_out": len(sent)}
        if orig:
            record["messages"] = len(orig["messages"])
        if reply:
            record["usage"] = {k: v for k, v in reply["usage"].items() if isinstance(v, int)}
            if reply.get("model"):
                record["answered"] = reply["model"]  # the model that really replied
        if orig and ctx.kind == "messages":
            try:  # observation only: from the client's own body, and never allowed to fail the request
                record["cache"] = cache_marks(orig)
                before = self.last_seen.get(ctx.lane) if ctx.lane else None
                if ctx.lane:
                    self.last_seen[ctx.lane] = started
                if before is not None:
                    record["idle"] = round(max(0.0, started - before), 1)
                if ctx.agent:  # which sub-agent this is, and (first request of its thread only) what it was asked, for `almanac report`
                    record["agent_type"] = ctx.agent_type[:40]
                    turns = rq.human_turns(orig["messages"])
                    if before is None and turns:
                        record["task"] = " ".join(redact(rq.human_text(orig["messages"][turns[0]])).split())[:80]
            except Exception:
                record.pop("cache", None)
        record.update(ctx.stats)
        if orig and reply and len(orig["messages"]) <= 3 and orig.get("tools"):
            record["sections"] = sections(ctx.after or orig)
            if ctx.after:
                record["sections_before"] = sections(orig)
        self.log.write(record)
        self.count += 1
        _tell(self, "outcome", ctx, orig, status)  # a few counters under a lock: no I/O
        if reply and orig:
            for layer in self.layers:
                if hasattr(layer, "observe"):
                    self._jobs.put((layer, ctx, orig, reply))

    def _work(self):
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                layer, ctx, orig, reply = job
                layer.observe(ctx, orig, reply)
            except Exception as exc:  # background work never reaches the user
                self.log.write({"ts": time.time(), "kind": "error", "where": "observe", "error": f"{type(exc).__name__}: {exc}"[:300]})
            finally:
                self._jobs.task_done()

    def drain(self, timeout=10.0):
        """Wait for background work (indexing, summaries) to finish. For shutdown and tests."""
        end = time.time() + timeout
        while self._jobs.unfinished_tasks and time.time() < end:
            time.sleep(0.02)

    def health(self, challenge="") -> dict:
        from . import __version__
        info = {"almanac": __version__, "upstream": ("https://" if self.secure else "http://") + self.host,  # no login, path or secret
                "requests": self.count, "uptime_s": int(time.time() - self.started),
                "layers": [type(layer).__name__ for layer in self.layers], "fused_lanes": len(self.fused)}
        if challenge:  # proves this process holds the key without sending it (see launcher.shared)
            info["proof"] = hmac.new(self.key.encode(), challenge.encode(), "sha256").hexdigest()
        return info

    def serve(self, host="127.0.0.1", port=0):
        server = Server((host, port), Handler)
        server.proxy = self
        return server


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":  # no other program may bind this port while it is in use
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind would also call socket.getfqdn, a reverse DNS lookup that can hang for many seconds on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # the client closed its connection: nothing to report
            return
        self.proxy.log.write({"ts": time.time(), "kind": "error", "where": "handler",
                              "error": f"{type(exc).__name__}: {exc}"[:300]})


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 960  # an idle or stalled local client must not hold a thread for ever
    _conn = None
    hold = None
    tunneled = False

    def log_message(self, *args):
        pass

    def finish(self):
        self._drop()
        super().finish()

    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            out = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size < 0:
                    raise ValueError("negative chunk size")
                if not size:
                    while self.rfile.readline().strip():
                        pass
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.readline()
        size = int(self.headers.get("content-length") or 0)
        if size < 0:
            raise ValueError("negative content-length")
        return self.rfile.read(size)

    def _drop(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status, why, message):
        """Answer without forwarding anything, and log it (never the token or the query)."""
        self.close_connection = True  # the request body is left unread
        proxy = self.server.proxy
        path = self.path.split("?", 1)[0]
        # A wrong token may be another proxy's real one: log the path without its first segment.
        path = ("/<?>/" + path[1:].partition("/")[2] if why == "token" else path.replace(proxy.token, "<token>"))[:80]
        proxy.log.write({"ts": round(time.time(), 3), "kind": "refused", "why": why, "method": self.command, "path": path})
        self._reply(status, {"error": message})

    def _open(self, conn):
        """Connect (TLS and tunnel included) quickly, and wait out an outage while the hold allows."""
        hold = self.hold
        try:
            while True:
                conn.timeout = CONNECT_TIMEOUT
                try:
                    return conn.connect()
                except OSError as exc:
                    conn.close()
                    # a certificate problem, or a proxy that answered with a refusal, is not an outage
                    if isinstance(exc, ssl.SSLError) or str(exc).startswith("Tunnel connection failed"):
                        raise
                    self._wait()
        finally:
            conn.timeout = UPSTREAM_TIMEOUT
            if conn.sock:
                conn.sock.settimeout(UPSTREAM_TIMEOUT)
            if hold.since and round(time.time() - hold.since):
                hold.stats["held"] = round(time.time() - hold.since)

    def _wait(self):
        """Keep the client alive for one more interval, or raise when the hold has run out."""
        hold, now = self.hold, time.time()
        hold.since = hold.since or now
        if now >= hold.until:
            raise OSError("the upstream stayed unreachable for the whole hold")
        # a silent wait cannot notice a client that left by writing, so look for its close
        if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
            raise OSError("the client left while its request waited")
        if hold.stream:
            if not hold.begun:
                self.send_response_only(200)
                for pair in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"), ("Transfer-Encoding", "chunked")):
                    self.send_header(*pair)
                self.end_headers()
                hold.begun = True
            self.wfile.write(b"%x\r\n%s\r\n" % (len(PING), PING))
            self.wfile.flush()
        time.sleep(min(PING_EVERY, hold.until - now))

    def _send(self, body: bytes, identity: bool, authorization=None):
        """Send this request upstream with its own headers. Returns the response.

        `authorization` replaces the Authorization header and nothing else."""
        proxy = self.server.proxy
        for attempt in (0, 1):
            reused = self._conn is not None
            conn = self._conn = self._conn or proxy.connect()
            try:
                if self.hold and conn.sock is None:
                    reused = False  # a failed connect is no stale connection: never open twice
                    self._open(conn)
                conn.putrequest(self.command, proxy.base + self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", proxy.netloc)
                for key, value in self.headers.items():
                    low = key.lower()
                    if low in REQUEST_SKIP or (identity and low == "accept-encoding"):
                        continue
                    conn.putheader(key, authorization if authorization and low == "authorization" else value)
                if identity:  # the reply is read here, so ask for it uncompressed
                    conn.putheader("Accept-Encoding", "identity")
                if body or self.command in ("POST", "PUT", "PATCH"):
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body or None)
                return conn.getresponse()
            except (OSError, http.client.HTTPException):
                self._drop()
                if not reused or attempt:  # only a stale kept-alive connection is retried
                    raise

    def _account(self, ctx):
        """(Authorization value or None, the token this request goes out with, the folder it came from)."""
        try:
            if not ctx or not self.server.proxy.cfg["accounts"]["fallback"] or not accounts.allowed(self.server.proxy):
                return None, "", None
            mine = (self.headers.get("authorization") or "")[7:].strip()  # after "Bearer "
            if mine and accounts.cooling_until(mine):
                alt = accounts.alternative(self.server.proxy.cfg, mine)
                if alt:
                    return "Bearer " + alt[1], alt[1], alt[0]
            return None, mine, None
        except Exception:  # fail open
            return None, "", None

    def _switch(self, ctx, resp, sent, used, folder):
        """A 429 came back. If it is a usage limit, send the same body once on another login.

        Returns (the reply for the client, the folder it went out on or None). Any failure, or no
        working alternative, returns the original 429 unchanged."""
        cfg = self.server.proxy.cfg
        body = resp.read()  # a 429 is small JSON: read it whole and serve it again if nothing changes
        original = Replay(resp, body)
        try:
            if not cfg["accounts"]["fallback"] or not accounts.allowed(self.server.proxy)                     or not accounts.is_limit(429, body, original.getheader):
                return original, folder
            accounts.mark(cfg, used, accounts.reset_at(cfg, original.getheader, body))
            alt = accounts.alternative(cfg, used)
            if not alt:
                return original, folder
            new = self._send(sent, identity=True, authorization="Bearer " + alt[1])
            if new.status < 400:
                accounts.announce(cfg, used, alt[0])
                return new, alt[0]
            if new.status == 429:
                data = new.read()
                if accounts.is_limit(429, data, new.getheader):
                    accounts.mark(cfg, alt[1], accounts.reset_at(cfg, new.getheader, data))
            else:
                new.read()
            return original, folder
        except Exception:
            self._drop()
            return original, folder

    def _pipe(self, resp, tee: bool) -> bytes:
        held = bool(self.hold and self.hold.begun)  # the client already has a 200 from the wait
        if held and resp.status != 200:  # no invented error: cut the stream so the client re-issues and sees the real status
            try:
                resp.read()
            except (OSError, http.client.HTTPException):
                self._drop()
            self.close_connection = True
            return b""
        bodiless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
        chunked = held or (resp.getheader("content-length") is None and not bodiless)
        if not held:
            self.send_response_only(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in RESPONSE_SKIP:
                    self.send_header(key, value)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        kept, size = [], 0
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if bodiless:
                    continue
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk) if chunked else chunk)
                self.wfile.flush()
                if tee and size < TEE_LIMIT:
                    kept.append(chunk)
                    size += len(chunk)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (OSError, http.client.HTTPException):  # one side went away mid-stream
            self._drop()
            self.close_connection = True
        return b"".join(kept)

    def _connect(self):
        """HTTPS-proxy mode: end TLS for the upstream host, tunnel everything else untouched."""
        proxy = self.server.proxy
        tls = proxy.tls
        self.close_connection = True
        if tls is None:
            return self._reply(405, {"error": "this almanac proxy is not running in HTTPS-proxy mode"})
        if not tls.authorized(self.headers.get("proxy-authorization")):
            return self._reply(407, {"error": "this proxy belongs to another session"})
        host, _, port = self.path.rpartition(":")
        if host.lower() == tls.host and port == "443":
            self.send_response_only(200, "Connection established")
            self.end_headers()
            try:
                inner = tls.context.wrap_socket(self.connection, server_side=True)
            except (ssl.SSLError, OSError):
                return
            try:
                Tunneled(inner, self.client_address, self.server)  # serves the requests inside the tunnel
            except (ssl.SSLError, OSError):
                pass
            return
        try:
            far = upstream.open_socket(proxy.hop_for(host), host, int(port))
        except (OSError, ValueError) as exc:
            return self._reply(502, {"error": f"almanac could not reach {self.path}: {exc}"})
        self.send_response_only(200, "Connection established")
        self.end_headers()
        relay(self.connection, far)

    def _upgrade(self, raw: bytes):
        """A protocol switch (WebSocket) inside the tunnel: pass the bytes both ways, unread."""
        proxy = self.server.proxy
        try:
            far = upstream.open_socket(proxy.hop, proxy.host, proxy.port)
            if proxy.secure:
                far = proxy.ctx.wrap_socket(far, server_hostname=proxy.host)
        except OSError as exc:
            return self._reply(502, {"error": f"almanac could not reach the upstream: {exc}"})
        head = f"{self.command} {proxy.base + self.path} {self.request_version}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in self.headers.items() if not k.lower().startswith("proxy-"))
        far.sendall(head.encode("latin-1") + b"\r\n" + raw)
        self.close_connection = True
        relay(self.connection, far)

    def _handle(self):
        proxy = self.server.proxy
        if self.command == "CONNECT":
            return self._connect()
        route = self.path.split("?", 1)[0]
        if not self.tunneled:
            if self.headers.get("origin") is not None or self.command == "OPTIONS":  # a web page must not drive this server
                return self._refuse(403, "browser request", "almanac does not answer browser requests")
            host = (self.headers.get("host") or "").rsplit(":", 1)[0].lower()
            if host not in LOOPBACK:  # nor reach it through a DNS trick
                return self._refuse(403, "host", "almanac only answers loopback requests")
            if route != "/almanac/health":  # every other request carries the secret as its first path segment
                seg, _, rest = self.path[1:].partition("/")
                if not hmac.compare_digest(seg.encode("latin-1"), proxy.token.encode()):
                    return self._refuse(403, "token", "the address is missing its secret or has an old one. Use the address almanac printed. "
                                 "If `almanac serve` was restarted, start Claude Code again: it gets a new secret each time.")
                self.path = "/" + rest  # the upstream never sees the token
                route = "/" + rest.split("?", 1)[0]
        try:
            raw = self._body()
        except ValueError:
            return self._refuse(400, "framing", "bad request length")
        if self.tunneled and self.headers.get("upgrade"):
            return self._upgrade(raw)
        if route == "/almanac/health":
            challenge = (parse_qs(urlsplit(self.path).query).get("challenge") or [""])[0][:128]
            return self._reply(200, proxy.health(challenge))
        kind = ROUTES.get(route) if self.command == "POST" else None
        marks = {k.lower(): v for k, v in self.headers.items() if k.lower().startswith("x-claude-code-")}
        # Only Claude Code's own model requests are touched; they always carry a session id.
        ctx = Ctx(proxy, marks, kind, self.headers.items()) if kind and marks.get("x-claude-code-session-id") else None
        started = time.time()
        try:
            sent, orig = proxy.rewrite(raw, ctx) if ctx else (raw, None)
        except Exception:  # fail open, whatever went wrong
            sent, orig = raw, None
        self.hold = None
        try:
            seconds = proxy.cfg["offline"]["hold_minutes"] * 60 if ctx else 0
            if seconds > 0:
                self.hold = Hold(started, seconds, bool(orig) and orig.get("stream") is True, ctx.stats)
        except Exception:  # a setting that is not a number turns the wait off, never the request
            pass
        try:
            auth, used, folder = self._account(ctx)  # a login known to be out of usage is skipped from the start
            resp = self._send(sent, identity=ctx is not None, authorization=auth)
            if ctx and sent is not raw and (resp.status == 400 or (resp.status in ROUTE_REFUSED and "route" in ctx.stats)):
                detail = resp.read()[:400].decode("utf-8", "replace")
                routed = ctx.stats.get("route")  # kept: the loop below removes it from the stats
                ctx.stats["fallback"] = detail
                limit = SIZE_LIMIT.search(detail)
                if limit and proxy.store is not None:
                    # The window table can be wrong for this conversation (the same model id has a 200k and a 1M form).
                    # Keep the upstream's own number, and let the retry below relieve the turn against it.
                    try:
                        proxy.store.put(f"window:{ctx.lane}", int(limit.group(1)))
                        ctx.stats.setdefault("compaction", {})
                    except Exception:  # without it the request falls back to the original, as before
                        pass
                retry, resent = raw, False
                # The changes most likely to be refused go first, one at a time, so the other savings stay.
                for mark in ("route", "compaction"):
                    if mark not in ctx.stats or proxy.store is None:
                        continue
                    if mark == "route":
                        _tell(proxy, "refused", ctx, resp.status, detail)
                    else:
                        try:
                            if SIZE_ERROR.search(detail):  # too long says nothing against the rewrite: estimate by size next time
                                proxy.store.run("delete from state where key = ?", (f"usage:{ctx.lane}",))
                            else:
                                proxy.store.put(f"nocompact:{ctx.lane}", detail)
                        except Exception:  # without the mark the retry may compact again, be refused again, and go out raw
                            pass
                    del ctx.stats[mark]
                    try:
                        retry, _ = proxy.rewrite(raw, ctx)
                    except Exception:  # fail open on the retry too
                        retry = raw
                    resp = self._send(retry, identity=True, authorization=auth)
                    if mark == "route" and resp.status < 400:  # only now is the model known to be the cause
                        _tell(proxy, "blame", ctx, routed)
                    if retry is raw or resp.status not in ROUTE_REFUSED + (400,):
                        resent = retry is raw  # the original has just been sent: never send it twice
                        break
                    resp.read()
                    retry = raw
                if retry is raw:  # the rewrite was refused: send the original
                    if not resent:
                        resp = self._send(raw, identity=True, authorization=auth)
                        if resp.status < 400:
                            proxy.fuse(ctx.lane, detail)
                    ctx.after = None
                if resp.status in ROUTE_REFUSED + (400,) and routed:  # the original fails too, so the model was not the cause
                    _tell(proxy, "forgive", ctx, routed)
                sent = retry
            if ctx and used and resp.status == 429:
                resp, folder = self._switch(ctx, resp, sent, used, folder)
            if ctx and folder:
                ctx.stats["account"] = {"switched": accounts.name(folder), "reason": "usage limit",
                                        "until": accounts.cooling_until((self.headers.get("authorization") or "")[7:].strip())}
        except (OSError, http.client.HTTPException) as exc:
            self._drop()
            if self.hold and self.hold.begun:  # the 200 is out, so no 502 can follow: cut the stream and let the client re-issue
                self.close_connection = True
                return proxy.finish(ctx, orig, raw, sent, 502, b"", started)
            via = f" through the proxy {proxy.hop.label}" if proxy.hop else ""
            message = f"almanac could not reach the upstream{via}: {type(exc).__name__}: {exc}"
            if ctx:
                _tell(proxy, "outcome", ctx, orig, 502)
            return self._reply(502, {"type": "error", "error": {"type": "api_error", "message": message}})
        tee = self._pipe(resp, tee=ctx is not None)
        if ctx:
            proxy.finish(ctx, orig, raw, sent, resp.status, tee, started)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_CONNECT = _handle


class Tunneled(Handler):
    """Requests arriving inside a TLS tunnel this proxy ended (HTTPS-proxy mode)."""

    tunneled = True


def relay(a, b):
    """Copy bytes both ways between two sockets until either side closes."""
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    for sock in (a, b):
        sock.settimeout(None)
    back = threading.Thread(target=pump, args=(b, a), daemon=True)
    back.start()
    pump(a, b)
    back.join(timeout=5)
    b.close()


def build(cfg=None) -> Proxy:
    """A proxy with every layer attached. Each layer reads its own on/off setting."""
    from .cheatsheet import CheatsheetLayer
    from .compact import CompactLayer
    from .memory import Memory, MemoryLayer, ProjectLayer
    from .playbook import PlaybookLayer
    from .routing import RoutingLayer
    from .startup import AgentsLayer, SkillsLayer, ToolsLayer
    from . import semantic
    from .store import Store
    from .summarize import SummaryLayer
    from .sysprefix import SysPrefixLayer
    from .prefixdiv import PrefixLayer

    cfg = cfg or config.load()
    store = Store(config.home() / "almanac.db")
    memory = Memory(store)
    memory.prune(cfg["memory"]["keep_days"])
    threading.Thread(target=semantic.active, daemon=True).start()  # the first recall should not wait a second for the model
    # Order matters: the project first, then the history is settled, then the parts inside it.
    # Routing comes after them: it prices the prefix as it will really be sent.
    # The cheatsheet comes after routing: it is chosen by the model the request goes out on.
    # The system prefix comes last of the rewriters: it moves what they leave, and must see their final text.
    layers = [ProjectLayer(memory), CompactLayer(memory), MemoryLayer(memory), PlaybookLayer(), SkillsLayer(), AgentsLayer(), ToolsLayer(), RoutingLayer(),
              CheatsheetLayer(), SysPrefixLayer(), SummaryLayer(memory)]
    if cfg["cloud"]["auto"] is True:  # off adds nothing: no layer, no thread, no git; a hand-typed "false" is off too
        from .cloud import HandoffLayer
        layers.append(HandoffLayer(memory, cfg))
    layers.append(PrefixLayer())  # last: it reads the body as every other layer left it
    return Proxy(cfg, store=store, layers=layers)
