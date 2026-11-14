"""A fake upstream and a proxy wired to it, for tests. Nothing here touches a real API."""
import http.client
import http.server
import json
import os
import tempfile
import threading
import time
import warnings

warnings.simplefilter("ignore", ResourceWarning)
os.environ.setdefault("ALMANAC_HOME", tempfile.mkdtemp(prefix="almanac-test-"))

from getalmanac import config, proxy as proxy_mod  # noqa: E402
from getalmanac.report import Log  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"


def sse(text="ok", usage=None, tool=None) -> bytes:
    usage = usage or {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 70}
    events = [{"type": "message_start", "message": {"usage": dict(usage, output_tokens=1)}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
              {"type": "content_block_stop", "index": 0}]
    if tool:
        events += [{"type": "content_block_start", "index": 1,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": tool[0], "input": {}}},
                   {"type": "content_block_delta", "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool[1])}},
                   {"type": "content_block_stop", "index": 1}]
    events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
                "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Upstream:
    """Records every request. `respond(request_dict) -> (status, headers, body)` decides the answer."""

    def __init__(self, respond=None):
        self.requests = []
        self.respond = respond or (lambda req: (200, {"content-type": "text/event-stream"}, sse()))
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _any(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                req = {"method": self.command, "path": self.path, "body": body,
                       "headers": {k.lower(): v for k, v in self.headers.items()}}
                outer.requests.append(req)
                status, headers, payload = outer.respond(req)
                self.send_response(status)
                chunked = headers.pop("x-test-chunked", None)
                for key, value in headers.items():
                    self.send_header(key, value)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(0, len(payload), 40):
                        part = payload[i:i + 40]
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = do_DELETE = _any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Rig:
    """An upstream plus a proxy in front of it."""

    def __init__(self, respond=None, layers=(), store=None, overrides=None):
        self.upstream = Upstream(respond)
        cfg = config.load()
        cfg["upstream"] = self.upstream.url
        for dotted, value in (overrides or {}).items():
            node = cfg
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = value
        self.cfg = cfg
        self.log_path = os.path.join(tempfile.mkdtemp(prefix="almanac-log-"), "requests.jsonl")
        self.proxy = proxy_mod.Proxy(cfg, store=store, layers=layers, log=Log(self.log_path))
        self.server = self.proxy.serve()
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.server.server_address[1]
        self.expected = 0

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        conn.request(method, path, payload, headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data

    def messages(self, body, session=SESSION, extra=None):
        headers = {"content-type": "application/json", "authorization": "Bearer test-token",
                   "anthropic-beta": "a,b,c"}
        if session:
            headers["x-claude-code-session-id"] = session
            self.expected += 1
        headers.update(extra or {})
        return self.request("POST", "/v1/messages?beta=true", body, headers)

    def sent(self, index=-1) -> dict:
        """The JSON body the upstream received for one request."""
        return json.loads(self.upstream.requests[index]["body"])

    def records(self) -> list:
        end = time.time() + 5  # the log line is written after the client has its answer
        while self.proxy.count < self.expected and time.time() < end:
            time.sleep(0.01)
        self.proxy.drain()
        return Log(self.log_path).tail()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()
