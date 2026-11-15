import json
import socket
import ssl
import threading
import time
import unittest
from unittest import mock

from helpers import Rig, Upstream, sse

from getalmanac import proxy as proxy_mod
from getalmanac import report

BODY = {"model": "claude-test", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
END = b"0\r\n\r\n"


class OfflineTest(unittest.TestCase):
    def rig(self, respond=None, **overrides):
        rig = Rig(respond, overrides=overrides)
        self.addCleanup(rig.close)
        self.port = rig.upstream.server.server_address[1]
        rig.upstream.close()  # the network is down: nothing listens
        for name, value in (("CONNECT_TIMEOUT", 0.2), ("PING_EVERY", 0.05)):
            patch = mock.patch.object(proxy_mod, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return rig

    def back_after(self, rig, seconds, respond=None):
        def start():
            time.sleep(seconds)
            rig.upstream = Upstream(respond, port=self.port)
        threading.Thread(target=start, daemon=True).start()

    def stream(self, rig):
        """Send one streaming request and read until the proxy closes: the raw bytes."""
        data = json.dumps(BODY).encode()
        head = (f"POST /{rig.proxy.token}/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
                f"x-claude-code-session-id: s1\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        with socket.create_connection(("127.0.0.1", rig.port), timeout=20) as sock:
            sock.sendall(head + data)
            out = b""
            while chunk := sock.recv(65536):
                out += chunk
        return out

    def test_stream_waits_then_delivers_one_request(self):
        rig = self.rig()
        rig.expected = 1
        self.back_after(rig, 1.6)
        out = self.stream(rig)
        self.assertTrue(out.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"event: ping", out)
        self.assertIn(sse(), out)
        self.assertTrue(out.endswith(END))
        self.assertEqual(len(rig.upstream.requests), 1)
        records = rig.records()
        self.assertGreaterEqual(records[-1]["held"], 1)
        self.assertEqual(report.summary(records)["held"], 1)

    def test_non_stream_waits_then_delivers_one_request(self):
        rig = self.rig()
        self.back_after(rig, 1.6)
        status, _, data = rig.messages(dict(BODY, stream=False))
        self.assertEqual(status, 200)
        self.assertIn(b"ok", data)
        self.assertEqual(len(rig.upstream.requests), 1)
        self.assertGreaterEqual(rig.records()[-1]["held"], 1)

    def test_client_that_leaves_is_not_served_later(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                rig = self.rig()
                before = set(threading.enumerate())
                data = json.dumps(dict(BODY, stream=stream)).encode()
                head = (f"POST /{rig.proxy.token}/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        f"x-claude-code-session-id: s1\r\nContent-Length: {len(data)}\r\n\r\n").encode()
                sock = socket.create_connection(("127.0.0.1", rig.port), timeout=20)
                sock.sendall(head + data)
                time.sleep(0.4)  # held by now
                sock.close()
                time.sleep(0.5)  # the proxy notices on its next interval
                rig.upstream = Upstream(port=self.port)  # the network is back
                end = time.time() + 5
                def handlers():  # request threads that this test started
                    return [t for t in set(threading.enumerate()) - before if "process_request" in t.name]
                while handlers() and time.time() < end:
                    time.sleep(0.05)
                time.sleep(0.3)
                self.assertEqual(rig.upstream.requests, [])
                self.assertEqual(handlers(), [])

    def test_error_after_the_wait_cuts_the_stream(self):
        rig = self.rig(lambda req: (429, {"content-type": "application/json"}, b'{"type":"error"}'))
        self.back_after(rig, 0.3, rig.upstream.respond)
        out = self.stream(rig)
        self.assertTrue(out.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"event: ping", out)
        self.assertNotIn(b"event: error", out)
        self.assertFalse(out.endswith(END))
        status, _, data = rig.messages(BODY)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(data)["type"], "error")

    def test_stream_gives_up_when_the_hold_runs_out(self):
        rig = self.rig(**{"offline.hold_minutes": 0.01})
        out = self.stream(rig)
        self.assertTrue(out.startswith(b"HTTP/1.1 200"))
        self.assertFalse(out.endswith(END))

    def test_non_stream_fails_after_the_silent_limit(self):
        rig = self.rig()
        with mock.patch.object(proxy_mod, "SILENT_LIMIT", 0.3):
            status, _, data = rig.messages(dict(BODY, stream=False))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data)["error"]["type"], "api_error")

    def test_hold_zero_fails_at_once(self):
        rig = self.rig(**{"offline.hold_minutes": 0})
        status, _, _ = rig.messages(BODY)
        self.assertEqual(status, 502)

    def test_certificate_error_is_not_held(self):
        rig = self.rig()
        connect = mock.Mock(side_effect=ssl.SSLError("bad"))
        rig.proxy.connect = lambda: mock.Mock(sock=None, connect=connect)
        status, _, _ = rig.messages(BODY)
        self.assertEqual(status, 502)
        self.assertEqual(connect.call_count, 1)

    def test_request_without_a_session_is_not_held(self):
        rig = self.rig()
        with mock.patch.object(proxy_mod, "SILENT_LIMIT", 30), mock.patch.object(proxy_mod, "PING_EVERY", 30):
            start = time.time()
            status, _, _ = rig.messages(BODY, session=None)
        self.assertEqual(status, 502)
        self.assertLess(time.time() - start, 5)


if __name__ == "__main__":
    unittest.main()
