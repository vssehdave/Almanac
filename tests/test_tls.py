import base64
import socket
import ssl
import unittest

from helpers import Rig

from getalmanac import launcher, tls


def connect(port, target, token=None):
    """Open a CONNECT tunnel through the proxy. Returns (status line, socket)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    auth = ""
    if token is not None:
        auth = "Proxy-Authorization: Basic " + base64.b64encode(f"almanac:{token}".encode()).decode() + "\r\n"
    sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return head.split(b"\r\n", 1)[0].decode(), sock


def read_all(sock) -> bytes:
    data = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except (ssl.SSLError, OSError):
            break
        if not chunk:
            break
        data += chunk
    return data


@unittest.skipUnless(tls.find_openssl(), "needs the openssl program")
class HttpsModeTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        self.session = tls.Session("api.test")
        self.addCleanup(self.session.close)
        self.rig.proxy.tls = self.session

    def test_the_signing_key_is_gone_and_only_the_bundle_remains(self):
        self.assertEqual(sorted(p.name for p in self.session.dir.iterdir()), ["ca.pem", "leaf.pem", "trust.pem"])

    def test_a_connect_without_the_session_token_is_refused(self):
        self.assertIn("407", connect(self.rig.port, "api.test:443")[0])
        self.assertIn("407", connect(self.rig.port, "api.test:443", token="wrong")[0])

    def test_the_upstream_host_is_decrypted_and_served_by_the_proxy(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "application/json"}, b'{"hello":1}')
        status, sock = connect(self.rig.port, "api.test:443", token=self.session.token)
        self.assertIn("200", status)
        trust = ssl.create_default_context(cafile=str(self.session.bundle))
        secure = trust.wrap_socket(sock, server_hostname="api.test")
        secure.sendall(b"GET /api/hello HTTP/1.1\r\nHost: api.test\r\nConnection: close\r\n\r\n")
        answer = read_all(secure)
        self.assertIn(b"200", answer.split(b"\r\n", 1)[0])
        self.assertTrue(answer.endswith(b'{"hello":1}'))
        self.assertEqual(self.rig.upstream.requests[-1]["path"], "/api/hello")

    def test_the_certificate_cannot_vouch_for_another_host(self):
        status, sock = connect(self.rig.port, "api.test:443", token=self.session.token)
        trust = ssl.create_default_context(cafile=str(self.session.bundle))
        with self.assertRaises(ssl.SSLError):
            trust.wrap_socket(sock, server_hostname="other.test")

    def test_other_hosts_are_tunnelled_without_being_read(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/plain"}, b"plain")
        target = f"127.0.0.1:{self.rig.upstream.server.server_address[1]}"
        status, sock = connect(self.rig.port, target, token=self.session.token)
        self.assertIn("200", status)
        sock.sendall(b"GET /raw HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertTrue(read_all(sock).endswith(b"plain"))
        self.assertEqual(self.rig.upstream.requests[-1]["path"], "/raw")

    def test_child_environment_keeps_the_base_url_and_points_at_the_proxy(self):
        parent = {"PATH": "x", "ANTHROPIC_BASE_URL": "http://old", "NO_PROXY": "corp.example, .test, *",
                  "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1"}
        env = launcher.https_env(self.session, 4321, parent=parent)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL", env)
        self.assertEqual(env["HTTPS_PROXY"], f"http://almanac:{self.session.token}@127.0.0.1:4321")
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], str(self.session.bundle))
        self.assertEqual(env["NO_PROXY"], "corp.example,localhost,127.0.0.1,::1")  # nothing may bypass for the upstream


class PlainModeTest(unittest.TestCase):
    def test_connect_is_refused_when_not_in_https_mode(self):
        rig = Rig()
        self.addCleanup(rig.close)
        self.assertIn("405", connect(rig.port, "api.test:443")[0])


if __name__ == "__main__":
    unittest.main()
