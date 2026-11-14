import base64
import contextlib
import io
import json
import os
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import Rig, sse

from getalmanac import cli, config, launcher, tls, upstream
from getalmanac import proxy as proxy_mod
from test_proxy import BODY
from test_tls import connect, read_all

LOGIN = "alice:s3cret"
ENCODED = base64.b64encode(LOGIN.encode()).decode()


class FakeProxy:
    """A CONNECT proxy on loopback. Records what it saw, then relays to `target`."""

    def __init__(self, status=200):
        self.seen, self.status, self.target = [], status, None
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline().decode().strip()
                headers = {}
                while True:
                    raw = self.rfile.readline().decode().strip()
                    if not raw:
                        break
                    key, _, value = raw.partition(":")
                    headers[key.lower()] = value.strip()
                outer.seen.append((line, headers))
                if outer.status != 200:
                    self.wfile.write(f"HTTP/1.1 {outer.status} Refused\r\nContent-Length: 0\r\n\r\n".encode())
                    return
                far = socket.create_connection(outer.target, timeout=10)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                proxy_mod.relay(self.connection, far)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Case(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxy()
        self.addCleanup(self.fake.close)

    def env(self, **values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def rig(self, **overrides):
        # "upstream.test" does not resolve: the request can only arrive through the proxy.
        rig = Rig(overrides={"upstream": "http://upstream.test:80", **overrides})
        self.addCleanup(rig.close)
        self.fake.target = rig.upstream.server.server_address
        return rig


class ThroughTheProxyTest(Case):
    def test_a_configured_proxy_carries_the_request(self):
        rig = self.rig(upstream_proxy=self.fake.url)
        status, _, data = rig.messages(BODY)
        self.assertEqual(status, 200)
        self.assertEqual(data, sse())
        self.assertTrue(self.fake.seen[0][0].startswith("CONNECT upstream.test:80 "))
        self.assertEqual(len(rig.upstream.requests), 1)

    def test_a_proxy_set_in_the_environment_carries_the_request(self):
        for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            with self.subTest(name):
                self.fake.seen.clear()
                with mock.patch.dict(os.environ, {name: self.fake.url.removeprefix("http://")}):  # no scheme is fine
                    rig = self.rig()
                self.assertEqual(rig.messages(BODY)[0], 200)
                self.assertEqual(len(self.fake.seen), 1)

    def test_no_proxy_sends_the_request_direct(self):
        self.env(HTTPS_PROXY=self.fake.url, NO_PROXY="example.com,127.0.0.1")
        rig = Rig()
        self.addCleanup(rig.close)
        self.assertEqual(rig.messages(BODY)[0], 200)
        self.assertEqual(self.fake.seen, [])
        self.assertEqual(len(rig.upstream.requests), 1)

    def test_no_proxy_patterns(self):
        cfg = config.load()
        for pattern, host, direct in [("*", "a.corp", True), (".corp", "a.corp", True), ("*.corp", "a.corp", True),
                                      ("corp", "a.corp", True), ("corp", "acorp", False), ("other", "a.corp", False)]:
            with self.subTest(pattern=pattern, host=host):
                found = upstream.find(cfg, host, {"HTTPS_PROXY": "http://p:1", "NO_PROXY": pattern})
                self.assertEqual(found is None, direct)

    def test_the_config_setting_wins_over_the_environment(self):
        hop = upstream.find({**config.load(), "upstream_proxy": "http://cfg:1"}, "h", {"HTTPS_PROXY": "http://env:2"})
        self.assertEqual((hop.label, hop.source), ("cfg:1", "config upstream_proxy"))

    def test_other_connection_kinds_use_the_proxy_too(self):
        rig = self.rig()
        sock = upstream.open_socket(upstream.Hop(self.fake.url, "test"), "upstream.test", 80)
        sock.sendall(b"GET /raw HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertIn(b"200", read_all(sock).split(b"\r\n", 1)[0])
        self.assertEqual(rig.upstream.requests[-1]["path"], "/raw")

    @unittest.skipUnless(tls.find_openssl(), "needs the openssl program")
    def test_hosts_almanac_does_not_read_are_tunnelled_through_the_proxy_as_well(self):
        self.env(HTTPS_PROXY=self.fake.url)
        rig = Rig()
        self.addCleanup(rig.close)
        session = tls.Session("api.test")
        self.addCleanup(session.close)
        rig.proxy.tls = session
        self.fake.target = rig.upstream.server.server_address
        status, sock = connect(rig.port, "other.test:8080", token=session.token)
        self.assertIn("200", status)
        sock.sendall(b"GET /far HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertIn(b"200", read_all(sock).split(b"\r\n", 1)[0])
        self.assertTrue(self.fake.seen[0][0].startswith("CONNECT other.test:8080 "))


class RefusalTest(Case):
    def test_a_refusing_proxy_gives_a_clear_error(self):
        for code in (407, 502):
            with self.subTest(code):
                self.fake.status = code
                rig = self.rig(upstream_proxy=self.fake.url)
                status, _, data = rig.messages(BODY)
                self.assertEqual(status, 502)
                message = json.loads(data)["error"]["message"]
                self.assertIn(str(code), message)
                self.assertIn(f"127.0.0.1:{self.fake.port}", message)

    def test_a_raw_connection_through_a_refusing_proxy_raises(self):
        self.fake.status = 407
        with self.assertRaisesRegex(OSError, "refused CONNECT: HTTP/1.1 407"):
            upstream.open_socket(upstream.Hop(self.fake.url, "test"), "upstream.test", 80)


class LoginTest(Case):
    def test_the_login_is_sent_and_kept_out_of_every_file_and_output(self):
        rig = self.rig(upstream_proxy=f"http://{LOGIN}@127.0.0.1:{self.fake.port}")
        self.assertEqual(rig.messages(BODY)[0], 200)
        rig.records()
        self.assertEqual(self.fake.seen[0][1]["proxy-authorization"], "Basic " + ENCODED)
        self.assertNotIn("proxy-authorization", rig.upstream.requests[0]["headers"])
        env = {"ALMANAC_UPSTREAM": rig.upstream.url, "HTTPS_PROXY": f"http://{LOGIN}@127.0.0.1:{self.fake.port}"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home, mock.patch.dict(os.environ, {"ALMANAC_HOME": home, **env}):
            (config.home() / "config.json").write_text(json.dumps({"upstream_proxy": f"http://{LOGIN}@proxy.example:3128"}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli._doctor([])
                cli._status([])
                cli._config([])
            shown = out.getvalue()
        self.assertIn("proxy.example:3128", shown)  # the config proxy is named as host:port only
        for text in (shown, str(rig.proxy.hop), json.dumps(rig.proxy.health())):
            self.assertNotIn("s3cret", text)
            self.assertNotIn(ENCODED, text)
        for root in (os.environ["ALMANAC_HOME"], os.path.dirname(rig.log_path)):
            for folder, _, names in os.walk(root):
                for name in names:
                    with open(os.path.join(folder, name), "rb") as handle:
                        blob = handle.read()
                    self.assertNotIn(b"s3cret", blob, name)
                    self.assertNotIn(ENCODED.encode(), blob, name)

    def test_hide_keeps_only_scheme_host_and_port(self):
        self.assertEqual(upstream.hide("http://a:b@h.example:3128"), "http://h.example:3128")
        self.assertEqual(upstream.hide(""), "")


class DoctorTest(Case):
    def setUp(self):
        super().setUp()
        rig = Rig()
        self.addCleanup(rig.close)
        self.rig_url = rig.upstream.url
        self.fake.target = rig.upstream.server.server_address

    def doctor(self, **env):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
            values = {"ALMANAC_HOME": home, "ALMANAC_UPSTREAM": self.rig_url, **env}
            with mock.patch.dict(os.environ, values), contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli._doctor([])
        return code, out.getvalue()

    def test_reports_the_proxy_its_source_and_that_connect_works(self):
        _, out = self.doctor(HTTPS_PROXY=self.fake.url)
        self.assertIn(f"ok   upstream proxy: 127.0.0.1:{self.fake.port} (from environment HTTPS_PROXY)", out)
        self.assertIn("CONNECT through", out)
        self.assertEqual(len(self.fake.seen), 1)

    def test_reports_a_proxy_that_refuses(self):
        self.fake.status = 407
        code, out = self.doctor(HTTPS_PROXY=self.fake.url)
        self.assertEqual(code, 1)
        self.assertIn("FAIL upstream:", out)
        self.assertIn("407", out)

    def test_reports_no_proxy(self):
        _, out = self.doctor()
        self.assertIn("upstream proxy: none", out)
        self.assertEqual(self.fake.seen, [])

    def test_an_unsupported_proxy_is_named_and_the_upstream_is_tried_directly(self):
        for url in ("socks5://127.0.0.1:1080", "https://127.0.0.1:3128"):
            with self.subTest(url):
                _, out = self.doctor(HTTPS_PROXY=url)
                self.assertIn("FAIL upstream proxy", out)
                self.assertIn("is not supported", out)
                self.assertIn("ok   upstream:", out)  # connected directly
        self.assertEqual(self.fake.seen, [])

    @unittest.skipUnless(tls.find_openssl(), "needs the openssl program")
    def test_reports_the_extra_ca_file(self):
        session = tls.Session("x.test")
        self.addCleanup(session.close)
        path = str(session.dir / "ca.pem")
        _, out = self.doctor(NODE_EXTRA_CA_CERTS=path)
        self.assertIn(f"ok   extra CA file: {path}", out)
        _, out = self.doctor(SSL_CERT_FILE=path + ".missing")
        self.assertIn("FAIL extra CA file", out)


@unittest.skipUnless(tls.find_openssl(), "needs the openssl program")
class TrustTest(unittest.TestCase):
    def test_extra_ca_files_are_added_and_verification_stays_on(self):
        session = tls.Session("x.test")
        self.addCleanup(session.close)
        path = str(session.dir / "ca.pem")
        cfg = {**config.load(), "upstream_ca": path}
        ctx = ssl.create_default_context()
        before = ctx.cert_store_stats()["x509_ca"]
        self.assertEqual(upstream.load_cas(ctx, cfg, {"NODE_EXTRA_CA_CERTS": "nowhere.pem"}), ([path], ["nowhere.pem"]))
        self.assertEqual(ctx.cert_store_stats()["x509_ca"], before + 1)
        built = upstream.context(cfg, {})
        self.assertEqual((built.verify_mode, built.check_hostname), (ssl.CERT_REQUIRED, True))


class LauncherTest(unittest.TestCase):
    PARENT = {"PATH": "x", "HTTPS_PROXY": "http://alice:s3cret@corp.example:3128", "NO_PROXY": "corp.local,.test"}

    def test_the_childs_proxy_is_never_taken_as_the_upstream_proxy(self):
        cfg = config.load()
        session = SimpleNamespace(host="api.anthropic.com", bundle="trust.pem",
                                  proxy_url=lambda port: f"http://almanac:tok@127.0.0.1:{port}")
        env = launcher.https_env(session, 4321, parent=self.PARENT)
        self.assertEqual(env["HTTPS_PROXY"], "http://almanac:tok@127.0.0.1:4321")
        self.assertEqual(env["NO_PROXY"], "corp.local,.test,localhost,127.0.0.1,::1")  # as before
        self.assertEqual(upstream.find(cfg, "api.anthropic.com", self.PARENT).label, "corp.example:3128")
        self.assertIsNone(upstream.find(cfg, "api.anthropic.com", env))  # no loop back into this process
        # In-process the proxy keeps the environment it was launched with, not the child's.
        with mock.patch.dict(os.environ, self.PARENT):
            rig = Rig(overrides={"upstream": "https://api.anthropic.com"})
            self.addCleanup(rig.close)
        self.assertEqual(rig.proxy.hop.label, "corp.example:3128")

    def test_the_child_in_plain_mode_reaches_almanac_directly_and_keeps_its_proxy(self):
        parent = {"HTTPS_PROXY": "http://corp.example:3128", "no_proxy": "a.example", "NO_PROXY": "b.example"}
        env = launcher.child_env("http://127.0.0.1:1", parent=parent)
        self.assertEqual(env["NO_PROXY"], "a.example,b.example,localhost,127.0.0.1,::1")
        self.assertNotIn("no_proxy", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://corp.example:3128")

    def test_https_mode_is_refused_for_an_unsupported_proxy_and_says_why(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ALL_PROXY": "socks5://corp.example:1080"}), contextlib.redirect_stderr(err):
            self.assertIsNone(launcher.https_session({**config.load(), "remote_control": True}))
        self.assertIn("socks5:// proxy from environment ALL_PROXY is not supported", err.getvalue())

    @unittest.skipUnless(tls.find_openssl(), "needs the openssl program")
    def test_https_mode_still_starts_behind_an_http_proxy(self):
        with mock.patch.dict(os.environ, self.PARENT):
            session = launcher.https_session({**config.load(), "remote_control": True})
        self.assertIsNotNone(session)
        session.close()


if __name__ == "__main__":
    unittest.main()
