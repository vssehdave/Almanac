import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import SESSION, Rig, sse

from getalmanac import launcher, report
from getalmanac import request as rq
from getalmanac import sse as sse_mod

BODY = {"model": "claude-test", "system": [{"type": "text", "text": "You are Claude Code"}],
        "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>ctx</system-reminder>"},
                                                   {"type": "text", "text": "hello there"}]}]}


class Upper:
    """A layer that changes the body, to exercise the rewrite path."""

    def transform(self, body, ctx):
        body["messages"][0]["content"][-1]["text"] = "HELLO THERE"


class Broken:
    def transform(self, body, ctx):
        raise RuntimeError("boom")


class PassthroughTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_body_and_headers_reach_upstream_unchanged(self):
        raw = json.dumps(BODY).encode()
        status, headers, data = self.rig.messages(raw)
        req = self.rig.upstream.requests[-1]
        self.assertEqual(status, 200)
        self.assertEqual(req["body"], raw)
        self.assertEqual(req["path"], "/v1/messages?beta=true")
        self.assertEqual(req["headers"]["authorization"], "Bearer test-token")
        self.assertEqual(req["headers"]["anthropic-beta"], "a,b,c")
        self.assertEqual(data, sse())

    def test_credentials_never_reach_the_log(self):
        self.rig.messages(BODY)
        self.rig.records()
        text = open(self.rig.log_path, encoding="utf-8").read()
        self.assertNotIn("test-token", text)
        self.assertNotIn("hello there", text)

    def test_usage_and_session_are_logged(self):
        self.rig.messages(BODY)
        record = self.rig.records()[-1]
        self.assertEqual(record["session"], SESSION)
        self.assertEqual(sse_mod.input_tokens(record["usage"]), 100)
        self.assertIn("tool: Bash", record["sections"])

    def test_other_paths_are_relayed(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "application/json"}, b'{"hello":1}')
        status, _, data = self.rig.request("GET", "/api/hello")
        self.assertEqual((status, data), (200, b'{"hello":1}'))
        status, _, data = self.rig.request("HEAD", "/api/hello")
        self.assertEqual((status, data), (200, b""))

    def test_chunked_upstream_is_relayed_whole(self):
        payload = sse("x" * 500)
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream", "x-test-chunked": "1"}, payload)
        status, headers, data = self.rig.messages(BODY)
        self.assertEqual(data, payload)
        self.assertEqual(headers.get("transfer-encoding"), "chunked")

    def test_upstream_errors_pass_through(self):
        self.rig.upstream.respond = lambda req: (429, {"content-type": "application/json", "retry-after": "3"}, b'{"type":"error"}')
        status, headers, data = self.rig.messages(BODY)
        self.assertEqual((status, headers["retry-after"], data), (429, "3", b'{"type":"error"}'))

    def test_non_loopback_host_is_refused(self):
        status, _, _ = self.rig.request("GET", "/almanac/health", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_health(self):
        status, _, data = self.rig.request("GET", "/almanac/health")
        self.assertEqual(status, 200)
        self.assertIn("almanac", json.loads(data))

    def test_unreachable_upstream_gives_502(self):
        self.rig.upstream.close()
        self.rig.cfg["offline"]["hold_minutes"] = 0  # the wait for the network has its own tests
        status, _, data = self.rig.messages(BODY)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data)["error"]["type"], "api_error")


class RewriteTest(unittest.TestCase):
    def test_layer_changes_reach_upstream(self):
        rig = Rig(layers=[Upper()])
        self.addCleanup(rig.close)
        rig.messages(BODY)
        self.assertEqual(rig.sent()["messages"][0]["content"][-1]["text"], "HELLO THERE")
        self.assertEqual(rig.upstream.requests[-1]["headers"]["content-length"], str(len(rig.upstream.requests[-1]["body"])))

    def test_requests_without_a_session_id_are_not_rewritten(self):
        rig = Rig(layers=[Upper()])
        self.addCleanup(rig.close)
        raw = json.dumps(BODY).encode()
        rig.messages(raw, session=None)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)

    def test_broken_layer_fails_open(self):
        rig = Rig(layers=[Broken()])
        self.addCleanup(rig.close)
        raw = json.dumps(BODY).encode()
        status, _, _ = rig.messages(raw)
        self.assertEqual(status, 200)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        self.assertIn("boom", rig.records()[-1]["error"])

    def test_rejected_rewrite_falls_back_and_fuses_the_lane(self):
        def respond(req):
            if b"HELLO THERE" in req["body"]:
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"type":"invalid_request_error"}}'
            return 200, {"content-type": "text/event-stream"}, sse()

        rig = Rig(respond=respond, layers=[Upper()])
        self.addCleanup(rig.close)
        raw = json.dumps(BODY).encode()
        status, _, data = rig.messages(raw)
        self.assertEqual((status, data), (200, sse()))
        self.assertEqual(len(rig.upstream.requests), 2)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        rig.messages(raw)  # the lane is fused: the next request goes out untouched, once
        self.assertEqual(len(rig.upstream.requests), 3)
        self.assertEqual(rig.upstream.requests[-1]["body"], raw)

    def test_count_tokens_is_rewritten_the_same_way(self):
        rig = Rig(respond=lambda req: (200, {"content-type": "application/json"}, b'{"input_tokens":5}'), layers=[Upper()])
        self.addCleanup(rig.close)
        rig.request("POST", "/v1/messages/count_tokens", BODY, {"x-claude-code-session-id": SESSION})
        self.assertEqual(rig.sent()["messages"][0]["content"][-1]["text"], "HELLO THERE")


class RequestHelpersTest(unittest.TestCase):
    def test_human_text_skips_reminders_and_tool_results(self):
        self.assertEqual(rq.human_text(BODY["messages"][0]), "hello there")
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}
        self.assertEqual(rq.human_text(result), "")

    def test_lane_differs_by_agent_and_first_message(self):
        a = rq.lane_key("s", "", BODY["messages"])
        self.assertEqual(a, rq.lane_key("s", "", BODY["messages"] + [{"role": "assistant", "content": "hi"}]))
        self.assertNotEqual(a, rq.lane_key("s", "agent-1", BODY["messages"]))
        self.assertNotEqual(a, rq.lane_key("s", "", [{"role": "user", "content": "other"}]))

    def test_working_dir(self):
        body = {"messages": [{"role": "system", "content": [{"type": "text", "text": "# Environment\n - Primary working directory: C:\\repo\\x\n - Is git: true"}]}]}
        self.assertEqual(rq.working_dir(body), "C:\\repo\\x")

    def test_sse_parse_rebuilds_tool_calls(self):
        reply = sse_mod.parse(sse("hi", tool=("Skill", {"skill": "pdf"})))
        self.assertEqual(reply["content"][0]["text"], "hi")
        self.assertEqual(reply["content"][1]["input"], {"skill": "pdf"})
        self.assertEqual(reply["stop_reason"], "tool_use")


class ReportTest(unittest.TestCase):
    def test_sections_split_the_session_text(self):
        text = ("SessionStart:startup hook success: hi\n\n# Environment\n - x\n\n"
                "The following skills are available for use with the Skill tool:\n\n- a: b\n\nToday's date is 2026-01-01.")
        body = {"system": "s" * 10, "tools": [{"name": "Bash"}, {"name": "Cold", "defer_loading": True}],
                "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": [{"type": "text", "text": text}]}]}
        parts = report.sections(body)
        self.assertEqual(parts["system prompt"], 10)
        self.assertNotIn("tool: Cold", parts)
        for label in ("hook output", "environment", "skill listing", "date", "conversation"):
            self.assertIn(label, parts)

    def test_render_shows_savings(self):
        record = {"ts": 1.0, "kind": "messages", "model": "m", "usage": {"input_tokens": 1000},
                  "sections": {"skill listing": 100, "system prompt": 900},
                  "sections_before": {"skill listing": 1100, "system prompt": 900}}
        out = report.render([record])
        self.assertIn("1,000 tokens sent", out)
        self.assertIn("about 2,000 without Almanac", out)


class LauncherTest(unittest.TestCase):
    def test_child_env_sets_base_url_and_no_credentials(self):
        env = launcher.child_env("http://127.0.0.1:1", parent={"PATH": "x"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:1")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_claude_codes_own_compaction_is_switched_off_only_when_almanac_keeps_the_context(self):
        def env(**compaction):
            cfg = {"compaction": {"enabled": True, "own_the_context": True, **compaction}}
            return launcher.child_env("http://127.0.0.1:1", parent={"PATH": "x"}, cfg=cfg)

        self.assertEqual(env()["DISABLE_AUTO_COMPACT"], "1")
        self.assertNotIn("DISABLE_AUTO_COMPACT", env(own_the_context=False))
        self.assertNotIn("DISABLE_AUTO_COMPACT", env(enabled=False))
        self.assertNotIn("DISABLE_AUTO_COMPACT", launcher.child_env("http://127.0.0.1:1", parent={"PATH": "x"}))

    def test_the_package_plugin_and_the_users_bundle_are_passed_unless_turned_off(self):
        mine = os.path.join(os.environ["ALMANAC_HOME"], "bundle")
        for name, manifest in (("one", True), ("two", True), ("notaplugin", False)):
            os.makedirs(os.path.join(mine, name, ".claude-plugin" if manifest else "x"), exist_ok=True)
            if manifest:
                with open(os.path.join(mine, name, ".claude-plugin", "plugin.json"), "w") as handle:
                    handle.write('{"name": "%s"}' % name)
        os.makedirs(os.path.join(mine, "almanac", ".claude-plugin"))  # a stale copy must not load beside the package's own
        with open(os.path.join(mine, "almanac", ".claude-plugin", "plugin.json"), "w") as handle:
            handle.write('{"name": "almanac"}')
        self.addCleanup(shutil.rmtree, mine)
        on = launcher.with_bundle(["-p", "hi"], {"bundle": {"enabled": True}})
        self.assertEqual(on[0::2][:-1], ["--plugin-dir"] * 3)
        self.assertEqual([os.path.basename(p) for p in on[1:6:2]], ["almanac", "one", "two"])
        self.assertTrue(os.path.isfile(os.path.join(on[1], ".claude-plugin", "plugin.json")))
        self.assertEqual(on[6:], ["-p", "hi"])
        self.assertEqual(launcher.with_bundle(["-p", "hi"], {"bundle": {"enabled": False}}), ["-p", "hi"])

    def test_every_bundled_plugin_has_a_manifest_and_a_licence(self):
        folder = os.path.join(os.path.dirname(launcher.__file__), "bundle")
        for name in os.listdir(folder):
            if os.path.isdir(os.path.join(folder, name)):
                with open(os.path.join(folder, name, ".claude-plugin", "plugin.json"), encoding="utf-8") as handle:
                    self.assertTrue(json.load(handle)["name"])
                self.assertTrue(os.path.isfile(os.path.join(folder, name, "LICENSE")), name)

    def test_run_launches_the_client_through_the_proxy(self):
        # A stand-in "claude" that makes one request to the base URL it was given.
        rig = Rig()
        self.addCleanup(rig.close)
        script = os.path.join(tempfile.mkdtemp(), "fake_claude.py")
        with open(script, "w") as handle:
            handle.write("import os,sys,urllib.request\n"
                         "r=urllib.request.urlopen(os.environ['ANTHROPIC_BASE_URL']+'/api/hello')\n"
                         "sys.exit(7 if r.status==200 else 1)\n")
        wrapper = os.path.join(os.path.dirname(script), "claude.cmd" if os.name == "nt" else "claude")
        with open(wrapper, "w") as handle:
            handle.write(f'@"{sys.executable}" "{script}" %*\n' if os.name == "nt" else f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        os.chmod(wrapper, 0o755)
        env = dict(os.environ, ALMANAC_CLAUDE=wrapper, ALMANAC_UPSTREAM=rig.upstream.url)
        env.pop("ANTHROPIC_BASE_URL", None)
        code = subprocess.call([sys.executable, "-m", "getalmanac", "-p", "hi"], env=env)
        self.assertEqual(code, 7)
        self.assertEqual(rig.upstream.requests[-1]["path"], "/api/hello")


class SharedProxyTest(unittest.TestCase):
    def tearDown(self):
        try:
            launcher.serve_file().unlink()
        except OSError:
            pass

    def test_a_running_proxy_of_this_version_is_found(self):
        rig = Rig()
        self.addCleanup(rig.close)
        launcher.publish(rig.port, rig.proxy.token, rig.proxy.key)
        self.assertEqual(launcher.shared(rig.port), f"http://127.0.0.1:{rig.port}/{rig.proxy.token}")

    def test_nothing_listening_and_other_servers_are_ignored(self):
        rig = Rig()
        self.addCleanup(rig.close)
        other = rig.upstream.server.server_address[1]  # answers, but is not an almanac proxy
        launcher.publish(other, rig.proxy.token, rig.proxy.key)
        self.assertIsNone(launcher.shared(other))
        launcher.publish(rig.port, rig.proxy.token, rig.proxy.key)
        rig.close()
        self.assertIsNone(launcher.shared(rig.port))


if __name__ == "__main__":
    unittest.main()


class StaleRetryTest(unittest.TestCase):
    """A request goes upstream a second time only when a kept-alive connection was closed with no reply."""

    def _run(self, error):
        from getalmanac import proxy as px
        sent = []

        class Conn:
            sock = object()
            def putrequest(self, *a, **k): pass
            def putheader(self, *a): pass
            def endheaders(self, body=None): sent.append(body)
            def getresponse(self):
                if len(sent) == 1:
                    raise error
                return "ok"
            def close(self): pass

        h = px.Handler.__new__(px.Handler)
        h.command, h.path, h.headers, h.hold = "POST", "/v1/messages", {}, None
        h._conn = Conn()
        h.server = type("S", (), {"proxy": type("P", (), {"base": "", "netloc": "x", "connect": lambda self: Conn()})()})()
        try:
            h._send(b"{}", False)
        except Exception:
            pass
        return len(sent)

    def test_a_timeout_is_not_sent_again(self):
        self.assertEqual(self._run(TimeoutError("read timed out")), 1)

    def test_a_closed_kept_alive_connection_is_retried(self):
        import http.client
        self.assertEqual(self._run(http.client.RemoteDisconnected("closed")), 2)
