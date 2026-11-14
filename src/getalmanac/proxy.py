"""The loopback server: relay everything, rewrite only what the layers ask for.

Headers and the query string go to the upstream as received. Credentials are
never read, stored or logged. Only the JSON body of a Claude Code model request
is ever changed, and if a layer fails or the upstream rejects the changed body,
the original bytes are sent instead.
"""
import http.client
import http.server
import json
import queue
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

from . import config
from . import request as rq
from . import sse
from .report import Log, sections

REQUEST_SKIP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
                "proxy-authorization", "proxy-connection", "content-length"}
RESPONSE_SKIP = {"connection", "keep-alive", "transfer-encoding"}
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
ROUTES = {"/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens"}
TEE_LIMIT = 16 * 1024 * 1024
UPSTREAM_TIMEOUT = 900


class Ctx:
    """What the layers know about one request. Holds no credentials."""

    def __init__(self, proxy, headers, kind):
        self.cfg, self.store, self.kind = proxy.cfg, proxy.store, kind
        self.session = headers.get("x-claude-code-session-id") or ""
        self.agent = headers.get("x-claude-code-agent-id") or headers.get("x-claude-code-parent-agent-id") or ""
        self.klass = headers.get("x-claude-code-request-class") or ""
        self.lane = self.model = self.cwd = self.project = ""
        self.stats = {}
        self.after = None  # the rewritten body, when a layer changed it


class Proxy:
    def __init__(self, cfg, store=None, layers=(), log=None):
        up = urlsplit(cfg["upstream"])
        if up.scheme not in ("http", "https") or not up.netloc:
            raise ValueError(f"upstream must be an http(s) URL, got {cfg['upstream']!r}")
        self.cfg, self.store, self.layers = cfg, store, list(layers)
        self.log = log or Log(config.home() / "logs" / "requests.jsonl")
        self.secure, self.netloc, self.base = up.scheme == "https", up.netloc, up.path.rstrip("/")
        self.fused = {}  # lane -> why its rewrites were switched off
        self.tls = None  # a tls.Session when running as an HTTPS proxy
        self.started, self.count = time.time(), 0
        self._jobs = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def connect(self):
        cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        return cls(self.netloc, timeout=UPSTREAM_TIMEOUT)

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
            self.store.put(f"fused:{lane}", why)

    def rewrite(self, raw: bytes, ctx: Ctx):
        """Return (bytes to send, parsed original or None)."""
        try:
            orig = json.loads(raw)
        except ValueError:
            return raw, None
        if not isinstance(orig, dict) or not isinstance(orig.get("messages"), list):
            return raw, None
        ctx.model = str(orig.get("model") or "")
        ctx.lane = rq.lane_key(ctx.session, ctx.agent, orig["messages"])
        ctx.cwd = rq.working_dir(orig)
        ctx.after = None
        if not self.layers or self.is_fused(ctx.lane):
            return raw, orig
        work = json.loads(raw)
        try:
            for layer in self.layers:
                layer.transform(work, ctx)
        except Exception as exc:  # fail open: a broken layer must never block a turn
            ctx.stats["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return raw, orig
        if work == orig:
            return raw, orig
        ctx.after = work
        return rq.dumps(work), orig

    def finish(self, ctx, orig, raw, sent, status, tee, started):
        reply = sse.parse(tee) if status == 200 and ctx.kind == "messages" and tee else None
        record = {"ts": round(started, 3), "ms": int((time.time() - started) * 1000), "kind": ctx.kind,
                  "session": ctx.session, "agent": ctx.agent[:16], "lane": ctx.lane, "class": ctx.klass,
                  "model": ctx.model, "status": status, "bytes_in": len(raw), "bytes_out": len(sent)}
        if orig:
            record["messages"] = len(orig["messages"])
        if reply:
            record["usage"] = {k: v for k, v in reply["usage"].items() if isinstance(v, int)}
        record.update(ctx.stats)
        if orig and reply and len(orig["messages"]) <= 3 and orig.get("tools"):
            record["sections"] = sections(ctx.after or orig)
            if ctx.after:
                record["sections_before"] = sections(orig)
        self.log.write(record)
        self.count += 1
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
        """Wait for background work (indexing) to finish. For shutdown and tests."""
        end = time.time() + timeout
        while self._jobs.unfinished_tasks and time.time() < end:
            time.sleep(0.02)

    def health(self) -> dict:
        from . import __version__
        return {"almanac": __version__, "upstream": ("https://" if self.secure else "http://") + self.netloc + self.base,
                "requests": self.count, "uptime_s": int(time.time() - self.started),
                "layers": [type(layer).__name__ for layer in self.layers], "fused_lanes": len(self.fused)}

    def serve(self, host="127.0.0.1", port=0):
        server = Server((host, port), Handler)
        server.proxy = self
        return server


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # the client closed its connection: nothing to report
            return
        self.proxy.log.write({"ts": time.time(), "kind": "error", "where": "handler",
                              "error": f"{type(exc).__name__}: {exc}"[:300]})


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _conn = None
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
                if not size:
                    while self.rfile.readline().strip():
                        pass
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

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

    def _send(self, body: bytes, identity: bool):
        """Send this request upstream with its own headers. Returns the response."""
        proxy = self.server.proxy
        for attempt in (0, 1):
            reused = self._conn is not None
            conn = self._conn = self._conn or proxy.connect()
            try:
                conn.putrequest(self.command, proxy.base + self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", proxy.netloc)
                for key, value in self.headers.items():
                    low = key.lower()
                    if low in REQUEST_SKIP or (identity and low == "accept-encoding"):
                        continue
                    conn.putheader(key, value)
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

    def _pipe(self, resp, tee: bool) -> bytes:
        self.send_response_only(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() not in RESPONSE_SKIP:
                self.send_header(key, value)
        bodiless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
        chunked = resp.getheader("content-length") is None and not bodiless
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
            far = socket.create_connection((host, int(port)), timeout=30)
        except (OSError, ValueError) as exc:
            return self._reply(502, {"error": f"almanac could not reach {self.path}: {exc}"})
        self.send_response_only(200, "Connection established")
        self.end_headers()
        relay(self.connection, far)

    def _upgrade(self, raw: bytes):
        """A protocol switch (WebSocket) inside the tunnel: pass the bytes both ways, unread."""
        proxy = self.server.proxy
        try:
            far = socket.create_connection((proxy.netloc.rsplit(":", 1)[0], 443 if proxy.secure else 80), timeout=30)
            if proxy.secure:
                far = ssl.create_default_context().wrap_socket(far, server_hostname=proxy.netloc.rsplit(":", 1)[0])
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
        host = (self.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if not self.tunneled and host not in LOOPBACK:  # a web page must not reach this server through a DNS trick
            return self._reply(403, {"error": "almanac only answers loopback requests"})
        raw = self._body()
        if self.tunneled and self.headers.get("upgrade"):
            return self._upgrade(raw)
        route = self.path.split("?", 1)[0]
        if route == "/almanac/health":
            return self._reply(200, proxy.health())
        kind = ROUTES.get(route) if self.command == "POST" else None
        marks = {k.lower(): v for k, v in self.headers.items() if k.lower().startswith("x-claude-code-")}
        # Only Claude Code's own model requests are touched; they always carry a session id.
        ctx = Ctx(proxy, marks, kind) if kind and marks.get("x-claude-code-session-id") else None
        started = time.time()
        sent, orig = proxy.rewrite(raw, ctx) if ctx else (raw, None)
        try:
            resp = self._send(sent, identity=ctx is not None)
            if ctx and sent is not raw and resp.status == 400:
                detail = resp.read()[:400].decode("utf-8", "replace")
                ctx.stats["fallback"] = detail
                retry = raw
                if "compaction" in ctx.stats and proxy.store is not None:
                    # Compaction changes the most, so drop it first and keep the other savings.
                    proxy.store.put(f"nocompact:{ctx.lane}", detail)
                    del ctx.stats["compaction"]
                    retry, _ = proxy.rewrite(raw, ctx)
                    resp = self._send(retry, identity=True)
                    if retry is not raw and resp.status == 400:
                        resp.read()
                        retry = raw
                if retry is raw:  # the rewrite was refused: send the original
                    resp = self._send(raw, identity=True)
                    if resp.status < 400:
                        proxy.fuse(ctx.lane, detail)
                    ctx.after = None
                sent = retry
        except (OSError, http.client.HTTPException) as exc:
            self._drop()
            message = f"almanac could not reach the upstream: {type(exc).__name__}: {exc}"
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
    from .compact import CompactLayer
    from .memory import Memory, MemoryLayer, ProjectLayer
    from .startup import SkillsLayer, ToolsLayer
    from .store import Store

    cfg = cfg or config.load()
    store = Store(config.home() / "almanac.db")
    memory = Memory(store)
    memory.prune(cfg["memory"]["keep_days"])
    # Order matters: the project first, then the history is settled, then the parts inside it.
    layers = [ProjectLayer(memory), CompactLayer(memory), MemoryLayer(memory), SkillsLayer(), ToolsLayer()]
    return Proxy(cfg or config.load(), store=store, layers=layers)
