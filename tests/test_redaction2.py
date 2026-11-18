"""Secrets that redact() used to miss, and proof that the patterns stay linear."""
import random
import time
import unittest

from getalmanac.memory import redact

BODY64 = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQ"
BS = chr(92)
# (name, text, the secret that must go, text that must survive)
SAMPLES = [
    ("pgp", "before\n-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG\n\n" + BODY64 + "\n-----END PGP PRIVATE KEY BLOCK-----\nafter", BODY64, "after"),
    ("ppk", "PuTTY-User-Key-File-3: ssh-ed25519\nPublic-Lines: 1\nAAAAC3Nza\nPrivate-Lines: 2\n" + BODY64 + "\n" + BODY64 + "\nPrivate-MAC: ab12\nend-of-file",
     BODY64, "Public-Lines: 1"),
    ("aws json secret", '{"AccessKeyId": "x", "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "Expiration": "2026"}',
     "wJalrXUtnFEMI", '"Expiration": "2026"'),
    ("aws session key", "id ASIAIOSFODNN7EXAMPLE ok", "ASIAIOSFODNN7EXAMPLE", "ok"),
    ("odbc Pwd", "Server=db1;Uid=app;Pwd=hunter22x;Database=sales;", "hunter22x", "Database=sales"),
    ("DB_PWD", "export DB_PWD=hunter22x && run", "hunter22x", "&& run"),
    ("env KEY", "STRIPE_KEY=zk9Qw3Lm7RtX2 run it", "zk9Qw3Lm7RtX2", "run it"),
    ("env TOKEN", "DEPLOY_TOKEN=zk9Qw3Lm7RtX2 run it", "zk9Qw3Lm7RtX2", "run it"),
    ("env SECRET", "APP_SECRET=zk9Qw3Lm7RtX2 run it", "zk9Qw3Lm7RtX2", "run it"),
    ("env PASSWORD", "ADMIN_PASSWORD=zk9Qw3Lm7RtX2 run it", "zk9Qw3Lm7RtX2", "run it"),
    ("quoted phrase", 'PASSWORD="correct horse battery staple" and more', "horse battery", "and more"),
    ("quoted phrase single", "db_password: 'blue green yellow pink'; next", "green yellow", "next"),
    ("quoted phrase json escaped", "run PASSWORD=" + BS + '"correct horse battery staple' + BS + '" now', "horse battery", "now"),
    ("url empty user", "see redis://:s3cretpw@cache.local:6379/0 here", "s3cretpw", "@cache.local:6379/0 here"),
    ("url slash", "postgres://u:ab/cd9Zq@db.local/app ok", "ab/cd9Zq", "@db.local/app ok"),
    ("url at", "postgres://u:p@ssw0rd@db.local/app ok", "ssw0rd", "@db.local/app ok"),
    ("google access", "access ya29." + "a0AfH6SMBx" * 4 + " done", "a0AfH6SMBx", "done"),
    ("google refresh", "refresh 1//0g" + "Xk3Lm9Qw2" * 5 + " done", "Xk3Lm9Qw2", "done"),
    ("slack app", "token xapp-1-A0123456789-0123456789012-abcdef0123456789 done", "A0123456789", "done"),
    ("telegram", "bot 123456789:AAH" + "x9Qz3Lm7Rt" * 3 + "ab done", "x9Qz3Lm7Rt", "done"),
    ("azure sas", "https://acct.blob.core.windows.net/c/f?sv=2022&se=2027&sig=Zm9vYmFyJTJCYmF6JTNE%2Bq9Lk&sp=r ok", "Zm9vYmFy", "&sp=r ok"),
    ("curl -u", "curl -s -u admin:Sup3rSecretpw https://api.local/x", "Sup3rSecretpw", "admin:"),
    ("quoted constant", 'STRIPE_KEY = "abc"; next', "abc", "next"),
    ("pwd after code", "x = 1; Pwd=hunter22x;", "hunter22x", "x = 1;"),
    ("curl after a chain", "ls && curl -u admin:Sup3rSecretpw https://api.local/x", "Sup3rSecretpw", "https://api.local/x"),
    ("curl --user", "curl --user admin:Sup3rSecretpw https://api.local/x", "Sup3rSecretpw", "https://api.local/x"),
]


class RedactionGapsTest(unittest.TestCase):
    def test_each_sample_loses_its_secret_and_keeps_the_text_around_it(self):
        for name, text, secret, keep in SAMPLES:
            with self.subTest(name):
                out = redact(text)
                self.assertNotIn(secret, out)
                self.assertIn(keep, out)

    def test_ordinary_text_with_similar_shapes_is_left_alone(self):
        for text in ("cd $PWD and PWD=/home/me/work", "a 1//2 split and 12345678:short", "page?sig=ab&x=1", "curl -s -u admin https://x.local",
                     "KEY=ab and primary_key: id_column", "docker run -u 1000:1000 img", "MY_KEY = get_key()", "MAX_TOKEN = 4096", "DEFAULT_PASSWORD = None",
                     "API_TOKEN = load_token_value2(path)", "DB_KEY = keys_table_v2[name]", "AUTH_SECRET = False", "OLD_PWD=/home/me/work",
                     "pwd=$(pwd)", "pwd = os.getcwd()", "Pwd=C:" + BS + "Users" + BS + "me", "my_pwd=./sub/dir",
                     "ref 123456789:" + "Zq" * 17 + "Z here", "curl -s https://x.local | grep -u ab:cd12 f", "make && grep -u ab:cd12 f",
                     "curl -u $USER:$TOKEN https://x.local", 'curl -u "$USER:${TOKEN}" https://x.local', "http://host:8080/docs?mail=a@b.com"):
            with self.subTest(text):
                self.assertEqual(redact(text), text)

    def test_redaction_of_200_kb_of_random_text_is_fast(self):
        rnd = random.Random(7)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 \n=:/@-_.\"'?&;<>"
        text = "".join(rnd.choice(alphabet) for _ in range(200_000))
        start = time.time()
        redact(text)
        self.assertLess(time.time() - start, 2.0)

    def test_pathological_runs_of_the_new_shapes_are_linear(self):
        for text in ("postgres://a:" * 20000, "curl -u a:" * 20000, "curl " * 40000, "ABC_KEY=" * 30000, "Private-Lines: 1" + "\nAAAA" * 40000,
                     "-----BEGIN PGP PRIVATE KEY BLOCK-----" * 6000, "1//" * 60000, "PASSWORD='" * 20000, "Pwd=" * 50000):
            with self.subTest(text[:20]):
                start = time.time()
                redact(text)
                self.assertLess(time.time() - start, 2.0)


if __name__ == "__main__":
    unittest.main()
