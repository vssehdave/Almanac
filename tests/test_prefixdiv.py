import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from helpers import SESSION, Rig

from getalmanac import prefixdiv, report
from getalmanac.prefixdiv import PrefixLayer
from getalmanac.store import State, Store

TOKEN = "Bearer secret-login-value"
OTHER = "22222222-2222-3333-4444-555555555555"


def body(typed="fix the bug", system="rules"):
    return {"model": "claude-test", "system": [{"type": "text", "text": "id"}, {"type": "text", "text": system}],
            "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}},
                      {"name": "Skip", "defer_loading": True, "input_schema": {}}],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>\nAs you answer, you can use the following context\n</system-reminder>"},
                {"type": "text", "text": typed, "cache_control": {"type": "ephemeral"}}]}]}


class Project:
    def transform(self, body, ctx):
        ctx.project = "proj"


class Upper:
    def transform(self, body, ctx):
        body["system"][1]["text"] = body["system"][1]["text"].upper()


class PrefixTest(unittest.TestCase):
    def rig(self, *layers):
        store = State(os.path.join(tempfile.mkdtemp(prefix="almanac-prefix-"), "a.db"))
        self.addCleanup(store.close)
        rig = Rig(layers=[Project(), *layers, PrefixLayer()], state=store)
        self.addCleanup(rig.close)
        self.state = store
        return rig

    def prefixes(self, rig):
        return [r.get("prefix") for r in rig.records()]

    def test_typed_text_is_where_it_diverges(self):
        rig = self.rig()
        rig.messages(body("one"), session=SESSION)
        rig.messages(body("two"), session=OTHER)
        first, second = self.prefixes(rig)
        self.assertIsNone(first)
        self.assertEqual(second["diverge_at"], "m0.1 conversation")
        self.assertEqual(second["index"], 4)  # Bash, system 0, system 1, m0.0
        self.assertGreater(second["tokens_before"], 0)
        self.assertGreater(second["tokens_after"], 0)
        self.assertTrue(second["same_account"])
        self.assertLess(second["since_project"], 60)
        self.assertNotIn("Skip", json.dumps(self.state.get("firstreq:proj")))  # deferred tools are not in the prefix

    def test_identical_prefix_is_none(self):
        rig = self.rig()
        rig.messages(body(), session=SESSION)
        rig.messages(body(), session=OTHER)
        second = self.prefixes(rig)[1]
        self.assertEqual(second["diverge_at"], "none")
        self.assertEqual(second["tokens_after"], 0)

    def test_system_change_is_named(self):
        rig = self.rig()
        rig.messages(body(system="a"), session=SESSION)
        rig.messages(body(system="b"), session=OTHER)
        self.assertEqual(self.prefixes(rig)[1]["diverge_at"], "system 1")

    def test_other_account(self):
        rig = self.rig()
        rig.messages(body(), session=SESSION)
        rig.messages(body(), session=OTHER, extra={"authorization": "Bearer another"})
        self.assertFalse(self.prefixes(rig)[1]["same_account"])

    def test_later_requests_and_sub_agents_log_nothing(self):
        rig = self.rig()
        rig.messages(body("one"), session=SESSION)
        later = body("one")
        later["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                              {"role": "user", "content": [{"type": "text", "text": "more"}]}]
        rig.messages(later, session=SESSION)
        rig.messages(body("sub"), session=OTHER, extra={"x-claude-code-agent-id": "agent1"})
        rig.messages(body("one"), session=SESSION)  # the same first request sent again
        self.assertEqual(self.prefixes(rig), [None] * 4)
        self.assertEqual(len(self.state.run("select 1 from state where key like 'firstreq:%'")), 1)
        self.assertEqual(self.state.get("firstreq:proj")["blocks"][-1][0], "m0.1 conversation")

    def test_no_login_value_stored_or_logged(self):
        rig = self.rig()
        rig.messages(body("one"), session=SESSION, extra={"authorization": TOKEN})
        rig.messages(body("two"), session=OTHER, extra={"authorization": TOKEN})
        rig.records()
        stored = json.dumps(self.state.get("firstreq:proj"))
        self.assertNotIn("secret-login-value", stored)
        self.assertNotIn("secret-login-value", Path(rig.log_path).read_text(encoding="utf-8"))
        self.assertEqual(len(self.state.get("firstreq:proj")["acct"]), 12)

    def test_request_bytes_do_not_change(self):
        sent = []
        for layers in ((Upper(),), (Upper(), PrefixLayer()), ()):
            rig = Rig(layers=[Project(), *layers], state=State(os.path.join(tempfile.mkdtemp(), "a.db")))
            self.addCleanup(rig.close)
            for session, typed in ((SESSION, "one"), (OTHER, "two")):
                rig.messages(json.dumps(body(typed)).encode(), session=session)
            sent.append([r["body"] for r in rig.upstream.requests])
        self.assertEqual(sent[0], sent[1])
        self.assertNotEqual(sent[0], sent[2])  # the rewriting layer really ran

    def test_broken_store_does_not_fail_the_request(self):
        rig = self.rig()
        rig.proxy.state = None
        status, _, _ = rig.messages(body())
        self.assertEqual(status, 200)


class LineTest(unittest.TestCase):
    def test_counts(self):
        def rec(at, since=10, same=True):
            return {"ts": 90000.0, "prefix": {"diverge_at": at, "since_project": since, "same_account": same}}
        records = [rec("m0.1 conversation"), rec("m0.1 conversation"), rec("system 1"), rec("none"),
                   rec("system 0", since=7200), rec("tool: Bash", same=False), {"ts": 90000.0},
                   {"ts": 1.0, "prefix": {"diverge_at": "system 0", "since_project": 1, "same_account": True}}]
        line = report.prefix_line(records, now=100000.0)
        self.assertIn("6 new conversation(s)", line)
        self.assertIn("3 diverged (m0.1 conversation x2, system 1 x1)", line)
        self.assertIn("2 cold", line)
        self.assertIn("1 identical", line)
        self.assertEqual(report.prefix_line([], now=100000.0), "")

    def test_cold_uses_the_shared_threshold(self):
        records = [{"ts": 90000.0, "prefix": {"diverge_at": "none", "since_project": 100, "same_account": True}}]
        self.assertIn("0 cold", report.prefix_line(records, now=100000.0))
        with mock.patch.object(report, "COLD_SECONDS", 50):
            self.assertIn("1 cold", report.prefix_line(records, now=100000.0))


class AccountTest(unittest.TestCase):
    def test_api_key_header_is_hashed_when_there_is_no_authorization(self):
        got = prefixdiv.account({"X-Api-Key": "sk-secret-key"})
        self.assertEqual(len(got), 12)
        self.assertNotIn("secret", got)
        self.assertEqual(got, prefixdiv.account({"x-api-key": "sk-secret-key"}))
        self.assertNotEqual(got, prefixdiv.account({"x-api-key": "sk-other"}))
        self.assertEqual(prefixdiv.account({"authorization": "Bearer a", "x-api-key": "b"}), prefixdiv.account({"authorization": "Bearer a"}))
        self.assertEqual(prefixdiv.account({}), "")


class BillingLineTest(unittest.TestCase):
    def test_the_billing_line_is_not_a_divergence(self):
        def body(version):
            return {"system": [{"type": "text", "text": f"x-anthropic-billing-header: cc_version=2.1.326.{version}; cc_entrypoint=cli;"},
                               {"type": "text", "text": "You are Claude Code"}],
                    "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(prefixdiv.compare(prefixdiv.blocks(body("aaa")), prefixdiv.blocks(body("bbb")))[0], "none")


if __name__ == "__main__":
    unittest.main()
