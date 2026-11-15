"""HTTPS-proxy mode: reach Claude Code without changing its base URL.

Claude Code hides some features (Remote Control among them) when its base URL is
not Anthropic's. In this mode the base URL is left alone and Almanac is set as
the HTTPS proxy instead. To read the model requests it must end the TLS
connection itself, so each session gets its own certificate:

- A certificate authority is made for the session and limited (a name
  constraint) to the one upstream host. Its key is deleted as soon as it has
  signed one certificate, so nothing can ever sign another.
- Only Claude Code trusts it, through NODE_EXTRA_CA_CERTS in its own
  environment. Nothing is added to the system or user trust store.
- Connections to every other host pass through as opaque tunnels.
- The proxy address carries a random token, so other local programs cannot use it.

Certificates are made with the `openssl` program, which Git for Windows, macOS
and Linux distributions ship. Without it this mode is not offered.
"""
import base64
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

CA_CONFIG = """[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = Almanac session CA (this machine only)
[ext]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign
nameConstraints = critical,permitted;DNS:{host}
"""
LEAF_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = {host}
[ext]
subjectAltName = DNS:{host}
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
"""


def find_openssl():
    found = shutil.which("openssl")
    if found:
        return found
    git = shutil.which("git")
    roots = [Path(git).resolve().parents[1]] if git else []
    roots += [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git"]
    for root in roots:
        for sub in ("mingw64/bin/openssl.exe", "usr/bin/openssl.exe"):
            if (root / sub).exists():
                return str(root / sub)
    return None


class Session:
    """One launch's certificate, proxy token and the files Claude Code needs."""

    def __init__(self, host: str, extra_ca=None):
        openssl = find_openssl()
        if not openssl:
            raise RuntimeError("the openssl program was not found")
        self.host = host.lower()
        self.token = secrets.token_urlsafe(24)
        self.dir = Path(tempfile.mkdtemp(prefix="almanac-tls-"))
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        d = self.dir

        def run(*args):
            done = subprocess.run([openssl, *args], cwd=d, capture_output=True, text=True)
            if done.returncode:
                raise RuntimeError(f"openssl {args[0]} failed: {done.stderr.strip()[-300:]}")

        try:
            (d / "ca.cnf").write_text(CA_CONFIG.format(host=self.host))
            (d / "leaf.cnf").write_text(LEAF_CONFIG.format(host=self.host))
            run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
                "-days", "30", "-config", "ca.cnf")
            run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
                "-config", "leaf.cnf")
            run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                "-out", "leaf.pem", "-days", "30", "-extfile", "leaf.cnf", "-extensions", "ext")
        except Exception:
            self.close()
            raise
        finally:
            for name in ("ca.key", "leaf.csr", "ca.cnf", "leaf.cnf", "ca.srl"):  # the CA can sign nothing more
                try:
                    (d / name).unlink()
                except OSError:
                    pass
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(d / "leaf.pem"), str(d / "leaf.key"))
        try:
            (d / "leaf.key").unlink()  # it lives in memory from here on
        except OSError:
            pass
        bundle = (d / "ca.pem").read_text()
        if extra_ca:  # keep trusting whatever the user already added
            try:
                bundle += "\n" + Path(extra_ca).read_text()
            except OSError:
                pass
        self.bundle = d / "trust.pem"
        self.bundle.write_text(bundle)

    def authorized(self, header) -> bool:
        """Whether a CONNECT carries this session's token."""
        try:
            scheme, _, value = (header or "").partition(" ")
            given = base64.b64decode(value).decode("utf-8", "replace").partition(":")[2]
        except ValueError:
            return False
        return scheme.lower() == "basic" and secrets.compare_digest(given.encode("utf-8"), self.token.encode("utf-8"))

    def proxy_url(self, port: int) -> str:
        return f"http://almanac:{self.token}@127.0.0.1:{port}"

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
