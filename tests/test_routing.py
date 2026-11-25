import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import Rig, SESSION, sse
from test_memory import make_repo

from getalmanac import cli, report, routing
from getalmanac.memory import Memory
from getalmanac.routing import RoutingLayer, classify
from getalmanac.store import State, Store

SMALL, MEDIUM, LARGE = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
AGENT_TOOL = {"name": "Agent", "description": "Launch a new agent.", "input_schema": {"type": "object"}}
BASH = {"name": "Bash", "description": "run", "input_schema": {"type": "object"}}

# The classification table doubles as documentation: what each kind of prompt is sent to.
PROMPTS = [
    # small: questions, lookups, summaries, formatting, one-step commands
    ("where is the config loaded?", "small"),
    ("summarize the last commit", "small"),
    ("list the files in src", "small"),
    ("git status", "small"),
    ("rename the variable foo to bar", "small"),
    ("format this JSON for me", "small"),
    ("convert 5 miles to km", "small"),
    ("read README.md and tell me the first line", "small"),
    # medium: write, change, fix or test code
    ("fix the failing test in tests/test_auth.py", "medium"),
    ("add a --verbose flag to cli.py", "medium"),
    ("implement pagination for the /users endpoint", "medium"),
    ("refactor parse_config to use dataclasses", "medium"),
    ("write unit tests for the cache module", "medium"),
    ("update the README with the new install steps", "medium"),
    ("create a new file hello.py that prints hi", "medium"),
    ("src/app.py", "medium"),
    ("How do I fix this bug in login.py?", "medium"),
    ("debug why the build fails on windows", "medium"),
    ("remove the unused imports", "medium"),
    ("optimize the slow query in report.py", "medium"),
    ("```py\nprint(1)\n``` why does this print 1?", "medium"),
    ("1. read the config\n2. parse it\n3. print the keys", "medium"),
    ("please look at this " + "long description of what I want. " * 50, "medium"),
    # large: design, audits, hard bugs, many files, earlier attempts failed
    ("design a plugin system for the proxy", "large"),
    ("audit the repo for security problems", "large"),
    ("find the root cause of the intermittent timeout", "large"),
    ("this still fails after your fix", "large"),
    ("that's wrong, try again", "large"),
    ("review the auth flow for vulnerabilities", "large"),
    ("update every file under src to use pathlib", "large"),
    ("refactor a.py b.py c.py d.py to share a base class", "large"),
    ("write a migration plan from sqlite to postgres", "large"),
    ("what are the trade-offs between these two approaches?", "large"),
    # inherit: no positive sign of an easy or a hard job, so the model stays (questions can be hard)
    ("What does the retry function in utils do?", "inherit"),
    ("explain this error: KeyError 'name'", "inherit"),
    ("how many tests are there?", "inherit"),
    ("hello", "inherit"),
    ("why is this test flaky?", "inherit"),
    ("Why do requests hang when two users save the same record at once?", "inherit"),
    ("How should we structure the permissions model for teams and orgs?", "inherit"),
    ("Is this retry approach safe for the payment webhook?", "inherit"),
    ("review the changes on this branch", "inherit"),
    ("review my PR", "inherit"),
    ("ultrathink about why the cache misses after deploy", "inherit"),
    ("Refactoriza el m\u00f3dulo de pagos para usar async y a\u00f1ade pruebas", "inherit"),
    ("Refaktoriere das Zahlungsmodul und f\u00fcge Tests hinzu", "inherit"),
    ("\u6c7a\u6e08\u30e2\u30b8\u30e5\u30fc\u30eb\u3092async\u306b\u66f8\u304d\u63db\u3048\u3066\u30c6\u30b9\u30c8\u3092\u8ffd\u52a0\u3057\u3066", "inherit"),
    ("next", "inherit"),
    ("do the rest", "inherit"),
    ("same for the other two endpoints", "inherit"),
    ("[Request interrupted by user]", "inherit"),
    ("<command-name>/review</command-name>\n<command-message>review</command-message>", "inherit"),
    ("<local-command-stdout>ok</local-command-stdout>", "inherit"),
    # incidental words must not promote a turn to large
    ("run all the tests", "inherit"),
    ("run the whole test suite", "inherit"),
    ("add an end-to-end test for login", "medium"),
    ("add audit logging to the save endpoint", "medium"),
    ("set concurrency to 4 in the CI config", "inherit"),
    ("mark the flaky test as skipped", "inherit"),
    ("why is this not working?", "inherit"),
    ("explain why npm install doesn't work behind the proxy", "inherit"),
    ('Traceback (most recent call last):\n  File "a.py", line 1\n  File "b.py", line 2\n  File "c.py", line 3\n'
     '  File "d.py", line 4\nKeyError: x\nwhat does this mean?', "inherit"),
    ("2024-01-01 12:00:00 ERROR deadlock detected in pool\nwhy?", "inherit"),
]
AGENT_PROMPTS = [
    ("Difficulty: simple\nRead README.md and report its first line.", "small"),
    ("Difficulty: standard\nAdd a retry to fetch() in net.py and run the tests.", "medium"),
    ("Difficulty: hard\nFind why the cache returns stale rows.", "large"),
    ("Find all usages of load_config and list them.", "inherit"),
    ("Fix the off-by-one in pager.py.", "medium"),
    ("Look into the situation described below and tell me what you think.", "inherit"),
    ("Look through the payment module and work out why refunds are sometimes applied twice. Report back", "inherit"),
    ("Search the codebase for how auth tokens are validated and whether expiry is checked everywhere", "inherit"),
    ("List every place auth tokens are validated and whether expiry is checked", "inherit"),
    ("1. read the config\n2. parse it\n3. print the keys", "inherit"),
]


def human(text):
    return {"role": "user", "content": [{"type": "text", "text": "<system-reminder>ctx</system-reminder>"}, {"type": "text", "text": text}]}


def convo(turns):
    """Messages for finished turns, each a dict(text, errors=0, pad=0), ending with the last turn's human message."""
    out = []
    for n, spec in enumerate(turns):
        out.append(human(spec["text"]))
        if n == len(turns) - 1:
            break
        for i in range(spec.get("errors", 0)):
            ident = f"t{n}_{i}"
            out.append({"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                                         {"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": f"try {i}"}}]})
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident, "content": "boom", "is_error": True}]})
        if spec.get("pad"):
            ident = f"p{n}"
            out.append({"role": "assistant", "content": [{"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": "cat big"}}]})
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident, "content": "x" * spec["pad"]}]})
        out.append({"role": "assistant", "content": [{"type": "text", "text": f"answer {n}"}]})
    return out


def body(messages, model=MEDIUM, tools=None, system="You are Claude Code"):
    return {"model": model, "max_tokens": 1000, "system": system, "tools": [AGENT_TOOL, BASH] if tools is None else tools,
            "thinking": {"type": "adaptive", "display": "omitted"}, "output_config": {"effort": "high"}, "messages": messages}


MAIN = {"x-claude-code-request-class": "main"}


def sub_headers(agent="agent-1", kind="custom"):
    return {"x-claude-code-request-class": "subagent", "x-claude-code-agent-id": agent, "x-claude-code-agent-type": kind}


class SetProject:
    """Stands in for the project layer, which routing reads the pin through."""

    def transform(self, body, ctx):
        ctx.project = "proj1"


class Case(unittest.TestCase):
    mode = "all"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-route-")
        self.store = Store(os.path.join(self.tmp, "a.db"))
        self.state = State(self.store.path.with_name("state.db"))
        self.addCleanup(self.store.close)
        self.start()

    def start(self, respond=None, **overrides):
        if getattr(self, "rig", None):
            self.rig.close()
        overrides = {"routing.mode": self.mode, "routing.route_inherited": True, **overrides}  # the older tests below predate the floor
        self.rig = Rig(respond=respond, layers=[SetProject(), *getattr(self, "before", []), RoutingLayer()], store=self.store, state=self.state, overrides=overrides)
        self.addCleanup(self.rig.close)

    def send(self, messages, model=MEDIUM, extra=None, **kw):
        self.rig.messages(body(messages, model, **kw), extra=extra or MAIN)
        return self.rig.sent()

    def decisions(self):
        return self.state.get("route:log", [])


class Tag:
    """Stands in for another layer whose change an upstream might refuse."""

    def transform(self, body, ctx):
        body["metadata"] = {"tag": "1"}


class ClassifyTest(unittest.TestCase):
    def test_every_prompt_in_the_table_goes_where_the_table_says(self):
        wrong = [(text[:50], want, classify(text)[0]) for text, want in PROMPTS if classify(text)[0] != want]
        self.assertEqual(wrong, [])
        self.assertGreaterEqual(len(PROMPTS), 30)

    def test_subagent_task_prompts(self):
        wrong = [(text[:40], want, classify(text, agent=True)[0]) for text, want in AGENT_PROMPTS if classify(text, agent=True)[0] != want]
        self.assertEqual(wrong, [])

    def test_a_short_follow_up_keeps_the_model(self):
        for text in ("yes", "ok, continue", "go ahead", "Thanks!"):
            self.assertEqual(classify(text)[0], "inherit", text)

    def test_only_unsure_turns_are_ambiguous(self):
        self.assertTrue(classify("How do I fix this bug in login.py?")[2])
        self.assertTrue(classify("src/app.py")[2])
        self.assertFalse(classify("design a plugin system")[2])
        self.assertFalse(classify("what is 2+2")[2])


class DecisionTest(Case):
    def test_the_model_and_effort_follow_the_tier(self):
        sent = self.send(convo([{"text": "list the files in src"}]))
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (SMALL, "medium"))
        sent = self.send(convo([{"text": "fix the bug in cli.py"}]), model=SMALL)
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (MEDIUM, "medium"))
        sent = self.send(convo([{"text": "design a plugin system"}]))
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (LARGE, "low"))
        self.assertEqual(sent["thinking"], {"type": "adaptive", "display": "omitted"})  # untouched

    def test_the_decision_is_frozen_across_a_tool_loop_and_identical_on_replay(self):
        first = [human("list the files in src")]
        loop = first + [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "design " * 50}]}]
        self.rig.messages(body(first), extra=MAIN)
        self.rig.messages(body(loop), extra=MAIN)
        self.rig.messages(body(first), extra=MAIN)
        sent = [r["body"] for r in self.rig.upstream.requests]
        self.assertEqual([json.loads(b)["model"] for b in sent], [SMALL, SMALL, SMALL])
        self.assertEqual(sent[0], sent[2])
        self.assertEqual(len(self.decisions()), 1)

    def test_a_lane_first_seen_mid_conversation_is_decided_from_its_latest_turn(self):
        messages = convo([{"text": "list the files in src"}, {"text": "design a plugin system"}])
        self.assertEqual(self.send(messages, model=SMALL)["model"], LARGE)

    def test_the_main_thread_keeps_one_state_key_per_session_with_the_model_it_sent(self):
        self.send(convo([{"text": "list the files in src"}]), model=SMALL)
        keys = [r["key"] for r in self.state.run("select key from state where key like '%main%'")]
        self.assertEqual(keys, [f"main:{SESSION}"])
        self.assertEqual(self.state.get(f"main:{SESSION}"), {"sent": SMALL})

    def test_a_model_outside_the_table_is_left_alone_and_named_in_status(self):
        sent = self.send(convo([{"text": "list the files in src"}]), model="claude-other-9")
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), ("claude-other-9", "high"))
        self.assertEqual((self.state.get("route:outside"), self.decisions()), ("claude-other-9", []))

    def test_a_hard_question_on_the_first_turn_keeps_the_model(self):
        sent = self.send(convo([{"text": "Why do requests hang when two users save at once?"}]), model=LARGE)
        self.assertEqual(sent["model"], LARGE)

    def test_a_system_message_is_not_a_turn_and_keeps_the_previous_decision(self):
        first = convo([{"text": "design a plugin system"}])
        self.send(first)
        interrupt = {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}
        sent = self.send(first + [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, interrupt])
        self.assertEqual(sent["model"], LARGE)
        self.assertEqual(len(self.decisions()), 1)

    def test_incidental_words_do_not_promote_a_session_to_large(self):
        self.assertEqual(self.send(convo([{"text": "run all the tests"}]))["model"], MEDIUM)

    def test_the_top_tier_keeps_the_effort_the_client_asked_for_when_its_effort_is_blank(self):
        self.start(**{"routing.effort": {"small": "medium", "medium": "medium", "large": ""}})
        raw = body(convo([{"text": "design a plugin system"}]))
        raw["output_config"]["effort"] = "xhigh"
        self.rig.messages(raw, extra=MAIN)
        sent = self.rig.sent()
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (LARGE, "xhigh"))

    def test_speed_fast_is_never_routed(self):
        raw = body(convo([{"text": "design a plugin system"}]))
        raw["speed"] = "fast"
        self.rig.messages(raw, extra=MAIN)
        self.assertEqual(len(self.rig.upstream.requests), 1)
        self.assertEqual(self.rig.sent()["model"], MEDIUM)
        self.assertEqual((routing.bad_tiers(self.state), self.decisions()), ({}, []))

    def test_a_skipped_decision_stays_on_the_same_model_and_effort(self):
        import time
        self.send(convo([{"text": "write a parser in utils.py"}]))
        self.state.put("route:bad:small", {"why": "x", "ts": time.time(), "lane": "l"})
        sent = self.send(convo([{"text": "write a parser in utils.py"}, {"text": "list the files in src"}]))
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (MEDIUM, "medium"))

    def test_an_explicit_model_is_left_alone(self):
        sent = self.send(convo([{"text": "design a plugin system"}]), model="claude-fable-5-2")
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), ("claude-fable-5-2", "high"))
        self.assertEqual(self.decisions(), [])

    def test_requests_that_are_not_a_thread_are_left_alone(self):
        for extra in ({"x-claude-code-request-class": "background"}, sub_headers(kind="fork")):
            self.assertEqual(self.send(convo([{"text": "design a plugin system"}]), model=SMALL, extra=extra)["model"], SMALL)
        self.assertEqual(self.send(convo([{"text": "design it"}]), model=SMALL, tools=[])["model"], SMALL)

    def test_the_ceiling_holds_the_tier_down(self):
        self.start(**{"routing.ceiling": "medium"})
        self.assertEqual(self.send(convo([{"text": "design a plugin system"}]))["model"], MEDIUM)

    def test_a_pin_wins_over_the_text(self):
        self.state.put(routing.pin_key("proj1"), "small")
        self.assertEqual(self.send(convo([{"text": "design a plugin system"}]), model=LARGE)["model"], SMALL)

    def test_a_request_that_does_not_fit_a_window_moves_up(self):
        self.start(**{"routing.windows": {SMALL: 1000, MEDIUM: 1000000, LARGE: 1000000}})
        sent = self.send(convo([{"text": "list the files in src"}]), system="You are Claude Code. " + "pad " * 5000)
        self.assertEqual(sent["model"], MEDIUM)

    def test_nothing_fits_so_nothing_changes(self):
        self.start(**{"routing.windows": {SMALL: 10, MEDIUM: 10, LARGE: 10}})
        sent = self.send(convo([{"text": "list the files in src"}]), model=MEDIUM)
        self.assertEqual(sent["model"], MEDIUM)
        self.assertEqual(self.decisions()[-1]["move"], "held")
        self.assertIn("window", self.decisions()[-1]["why"])


def tool(i, err, command=None):
    """One tool call and its result."""
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": command or f"try {i}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x", **({"is_error": True} if err else {})}]}]


def said(sent):
    """The effort a trailing system message asks for, or None."""
    last = sent["messages"][-1]
    return last["output_config"]["effort"] if last["role"] == "system" else None


class OneModelPerLaneTest(Case):
    def step(self, messages, **kw):
        sent = self.send(messages, **kw)
        return sent["model"], sent["output_config"]["effort"], said(sent)

    def test_the_first_request_picks_the_model_and_only_effort_answers_a_failure(self):
        turns = [{"text": "list the files in src"}]
        self.assertEqual(self.step(convo(turns)), (SMALL, "medium", None))  # the lane's first request picks Haiku
        turns.append({"text": "design a plugin system for the proxy"})
        self.assertEqual(self.step(convo(turns)), (SMALL, "medium", None))  # a hard turn later does not move it
        turns.append({"text": "that's wrong, try again"})
        self.assertEqual(self.step(convo(turns)), (SMALL, "medium", "high"))  # a failure raises effort, not the model
        loop = convo(turns[:2] + [{"text": "fix cli.py"}])
        for i in range(3):
            loop += tool(i, True)
        self.assertEqual(self.step(loop), (SMALL, "medium", "high"))  # three tool errors: the same
        loop += tool(3, False)
        self.assertEqual(self.step(loop), (SMALL, "medium", "high"))  # one clean request is not enough
        loop += tool(4, False)
        self.assertEqual(self.step(loop), (SMALL, "medium", None))  # calm_after (2) clean requests: back to the start
        self.assertEqual(len(self.decisions()), 1)

    def test_a_first_request_that_says_it_failed_keeps_the_model(self):
        self.assertEqual(self.step(convo([{"text": "list the files in src"}, {"text": "that's not right, explain again"}]), model=SMALL),
                         (SMALL, "medium", "high"))
        self.assertIn("effort rises instead", self.decisions()[-1]["why"])

    def test_two_errors_are_not_enough(self):
        turns = [{"text": "fix the bug in cli.py", "errors": 2}, {"text": "ok, try the other file b.py"}]
        self.assertEqual(self.step(convo(turns)), (MEDIUM, "medium", None))

    def test_the_same_call_failing_over_and_over_counts(self):
        messages = convo([{"text": "fix the bug in cli.py"}])
        for i in range(3):
            messages += tool(f"r{i}", True, "pytest") + tool(f"o{i}", False, "ls")
        messages += [{"role": "assistant", "content": [{"type": "text", "text": "I could not fix it."}]}, human("try once more on b.py")]
        self.assertEqual(self.step(messages), (MEDIUM, "medium", "high"))

    def test_a_large_main_thread_gets_the_effort_message_under_the_beta(self):
        raw = body(convo([{"text": "fix the bug in cli.py", "pad": 120000}, {"text": "that's wrong, try again"}]), MEDIUM)
        raw["output_config"]["effort"] = "low"
        self.rig.messages(raw, extra=MAIN)
        sent = self.rig.sent()
        self.assertEqual((sent["model"], said(sent)), (MEDIUM, "high"))
        self.assertTrue(self.rig.upstream.requests[-1]["headers"]["anthropic-beta"].endswith(routing.EFFORT_BETA))


class SubagentTest(Case):
    def test_a_subagent_is_routed_by_its_task_and_keeps_its_tier(self):
        task = [human("Difficulty: simple\nRead README.md and report its first line.")]
        loop = task + [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "design " * 40}]}]
        self.rig.messages(body(task, MEDIUM), extra=sub_headers())
        self.rig.messages(body(loop, MEDIUM), extra=sub_headers())
        sent = [json.loads(r["body"]) for r in self.rig.upstream.requests]
        self.assertEqual([s["model"] for s in sent], [SMALL, SMALL])
        self.assertEqual(sent[0]["output_config"]["effort"], "medium")

    def test_a_hard_task_goes_to_the_large_model(self):
        self.rig.messages(body([human("Difficulty: hard\nWhy does the cache return stale rows?")], SMALL), extra=sub_headers())
        self.assertEqual(self.rig.sent()["model"], LARGE)

    def test_an_unmarked_task_keeps_the_model_claude_code_chose_whatever_the_agent_type(self):
        for kind in ("Plan", "Explore"):
            self.rig.messages(body([human("Look into how sessions expire.")], SMALL), extra=sub_headers(kind, kind))
            self.assertEqual(self.rig.sent()["model"], SMALL, kind)

    def test_two_subagents_keep_their_own_tiers(self):
        self.rig.messages(body([human("Difficulty: simple\nlist the files")], MEDIUM), extra=sub_headers("a"))
        self.rig.messages(body([human("Difficulty: hard\nfind the race")], MEDIUM), extra=sub_headers("b"))
        self.assertEqual([json.loads(r["body"])["model"] for r in self.rig.upstream.requests], [SMALL, LARGE])


    def test_a_failing_subagent_keeps_its_model_and_its_raised_effort_drops_back(self):
        loop = [human("Difficulty: standard\nfix the failing test")]
        for i in range(3):
            loop += tool(i, True)
        got = []
        for clean in range(3):
            self.rig.messages(body(loop, MEDIUM), extra=sub_headers("c"))
            sent = self.rig.sent()
            got.append((sent["model"], said(sent)))
            loop += tool(3 + clean, False)
        self.assertEqual(got, [(MEDIUM, "high"), (MEDIUM, "high"), (MEDIUM, None)])

    def test_a_large_cached_subagent_keeps_its_model_and_only_raises_effort(self):
        loop = [human("Difficulty: standard\nfix the failing test " + "x" * 120000)]
        for i in range(3):
            loop += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "pytest"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x", "is_error": True}]}]
        raw = body(loop, MEDIUM)
        raw["output_config"]["effort"] = "medium"
        self.rig.messages(raw, extra=sub_headers("big"))
        sent = self.rig.sent()
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (MEDIUM, "medium"))  # the cached prefix is untouched
        last = sent["messages"][-1]
        self.assertEqual((last["role"], last["content"], last["output_config"]["effort"]), ("system", [], "high"))
        self.assertTrue(self.rig.upstream.requests[-1]["headers"]["anthropic-beta"].endswith(routing.EFFORT_BETA))  # merged after the client's own value

class SubagentsModeTest(Case):
    mode = "subagents"

    def test_the_main_thread_is_untouched_but_subagents_are_routed(self):
        main = body(convo([{"text": "design a plugin system"}]), model=SMALL)
        self.rig.messages(main, extra=MAIN)
        self.assertEqual(self.rig.sent()["model"], SMALL)
        self.rig.messages(body([human("Difficulty: hard\nfind the race")], SMALL), extra=sub_headers())
        self.assertEqual(self.rig.sent()["model"], LARGE)


class OffTest(Case):
    mode = "off"

    def test_off_changes_no_byte_of_any_request(self):
        plain = Rig()
        self.addCleanup(plain.close)
        for model, extra in ((MEDIUM, MAIN), (SMALL, sub_headers()), ("claude-fable-5-2", MAIN)):
            raw = json.dumps(body(convo([{"text": "design a plugin system"}]), model)).encode()
            self.rig.messages(raw, extra=extra)
            plain.messages(raw, extra=extra)
            self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)
            self.assertEqual(self.rig.upstream.requests[-1]["body"], plain.upstream.requests[-1]["body"])
        self.assertEqual(self.decisions(), [])


class GuidanceTest(Case):
    def test_the_agent_tool_is_sent_as_the_client_wrote_it(self):
        self.send(convo([{"text": "fix cli.py"}]))
        tool = next(t for t in json.loads(self.rig.upstream.requests[-1]["body"])["tools"] if t["name"] == "Agent")
        self.assertEqual(tool["description"], AGENT_TOOL["description"])


class RefusedTest(Case):
    def refuse_large(self, status=404):
        def respond(req):
            if json.loads(req["body"])["model"] == LARGE:
                return status, {"content-type": "application/json"}, b'{"type":"error","error":{"type":"not_found_error","message":"model: claude-opus-5-5"}}'
            return 200, {"content-type": "text/event-stream"}, sse()
        self.start(respond=respond)

    def test_a_refused_model_falls_back_to_the_original_body_and_the_tier_is_marked(self):
        self.refuse_large()
        raw = json.dumps(body(convo([{"text": "design a plugin system"}]), MEDIUM)).encode()
        status, _, _ = self.rig.messages(raw, extra=MAIN)
        self.assertEqual(status, 200)
        sent = [json.loads(r["body"]) for r in self.rig.upstream.requests]
        self.assertEqual([s["model"] for s in sent], [LARGE, MEDIUM])
        self.assertEqual(sent[1], json.loads(raw))  # the second request is the original
        self.assertIn("large", routing.bad_tiers(self.state))
        self.assertIn("404", self.decisions()[-1]["skip"])
        self.assertEqual(self.rig.proxy.fused, {})  # nothing else about the conversation is switched off

    def test_the_next_turn_does_not_try_the_marked_tier_again(self):
        self.refuse_large(400)
        self.rig.messages(body(convo([{"text": "design a plugin system"}]), MEDIUM), extra=MAIN)
        before = len(self.rig.upstream.requests)
        self.rig.messages(body(convo([{"text": "design a plugin system"}, {"text": "now design a cache"}]), MEDIUM), extra=MAIN)
        self.assertEqual(len(self.rig.upstream.requests), before + 1)
        self.assertEqual(self.rig.sent()["model"], MEDIUM)

    def test_the_rest_of_that_turn_goes_out_unrouted_too(self):
        self.refuse_large()
        first = [human("design a plugin system")]
        self.rig.messages(body(first, MEDIUM), extra=MAIN)
        n = len(self.rig.upstream.requests)
        loop = first + [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}]
        self.rig.messages(body(loop, MEDIUM), extra=MAIN)
        self.assertEqual(len(self.rig.upstream.requests), n + 1)
        self.assertEqual(self.rig.sent()["model"], MEDIUM)

    def test_another_layers_fault_does_not_mark_the_tier(self):
        def respond(req):
            data = json.loads(req["body"])
            if data["model"] == LARGE:
                return 404, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"model"}}'
            if "metadata" in data:
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"metadata"}}'
            return 200, {"content-type": "text/event-stream"}, sse()
        self.before = [Tag()]
        self.start(respond=respond)
        status, _, _ = self.rig.messages(body(convo([{"text": "design a plugin system"}]), MEDIUM), extra=MAIN)
        self.assertEqual((status, len(self.rig.upstream.requests)), (200, 3))
        self.assertEqual(routing.bad_tiers(self.state), {})

    def test_an_overloaded_retry_keeps_the_turn_unrouted_and_marks_nothing(self):
        def respond(req):
            if json.loads(req["body"])["model"] == LARGE:
                return 404, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"model"}}'
            return 529, {"content-type": "application/json"}, b'{"type":"error","error":{"type":"overloaded_error","message":"busy"}}'
        self.start(respond=respond)
        status, _, _ = self.rig.messages(body(convo([{"text": "design a plugin system"}]), MEDIUM), extra=MAIN)
        self.assertEqual(status, 529)
        self.assertEqual(routing.bad_tiers(self.state), {})
        self.assertIn("404", self.decisions()[-1]["skip"])

    def test_a_failing_layer_hook_does_not_drop_the_request(self):
        self.refuse_large()
        with mock.patch.object(RoutingLayer, "blame", side_effect=RuntimeError("db")):
            status, _, _ = self.rig.messages(body(convo([{"text": "design a plugin system"}]), MEDIUM), extra=MAIN)
        self.assertEqual(status, 200)

    def test_if_the_original_fails_too_the_model_is_not_blamed(self):
        self.start(respond=lambda req: (400, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"bad request"}}'))
        status, _, _ = self.rig.messages(body(convo([{"text": "design a plugin system"}]), MEDIUM), extra=MAIN)
        self.assertEqual(status, 400)
        self.assertEqual(routing.bad_tiers(self.state), {})
        self.assertEqual(self.decisions()[-1]["skip"], "")


class ModelClassifierTest(Case):
    def start_with(self, answer):
        self.calls = []

        def respond(req):
            data = json.loads(req["body"])
            if "sort coding requests" in json.dumps(data.get("system")):
                self.calls.append(data)
                return 200, {"content-type": "application/json"}, json.dumps(
                    {"type": "message", "content": [{"type": "text", "text": answer}], "usage": {"input_tokens": 90, "output_tokens": 12}}).encode()
            return 200, {"content-type": "text/event-stream"}, sse()
        self.start(respond=respond, **{"routing.classifier": "model"})

    def test_an_unsure_turn_is_settled_by_one_small_model_call_and_not_asked_again(self):
        self.start_with('{"tier": "large", "why": "hard to say from the text"}')
        messages = convo([{"text": "How do I fix this bug in login.py?"}])
        self.assertEqual(self.send(messages)["model"], LARGE)
        self.send(messages)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.calls[0]["model"], self.calls[0]["max_tokens"]), (SMALL, 120))
        self.assertIn("a small model judged", self.decisions()[-1]["why"])
        self.assertEqual(self.state.get("summary:day")["calls"], 1)  # shares the daily count with summaries

    def test_terminal_escapes_in_the_models_reason_are_dropped(self):
        self.start_with('{"tier": "large", "why": "\\u001b[31mhard"}')
        self.send(convo([{"text": "How do I fix this bug in login.py?"}]))
        self.assertIn("hard", self.decisions()[-1]["why"])
        self.assertNotIn("\x1b", self.decisions()[-1]["why"])

    def test_a_sure_turn_is_never_sent_to_a_model(self):
        self.start_with('{"tier": "large", "why": "x"}')
        self.assertEqual(self.send(convo([{"text": "design a plugin system"}]))["model"], LARGE)
        self.assertEqual(self.send(convo([{"text": "list the files in src"}]))["model"], SMALL)
        self.assertEqual(self.calls, [])

    def test_a_bad_answer_falls_back_to_the_rules(self):
        self.start_with("I think it is hard")
        self.assertEqual(self.send(convo([{"text": "How do I fix this bug in login.py?"}]))["model"], MEDIUM)
        self.assertEqual(len(self.calls), 1)

    def test_a_paused_login_skips_the_call(self):
        self.start_with('{"tier": "large", "why": "x"}')
        import time
        self.state.put("summary:pause", time.time() + 600)
        self.assertEqual(self.send(convo([{"text": "How do I fix this bug in login.py?"}]))["model"], MEDIUM)
        self.assertEqual(self.calls, [])


class CommandTest(unittest.TestCase):
    def test_pin_and_status(self):
        tmp = tempfile.mkdtemp(prefix="almanac-pin-")
        store = Store(os.path.join(tmp, "a.db"))
        self.addCleanup(store.close)
        repo = make_repo(tmp, "alpha", "https://github.com/Someone/Alpha.git")
        memory = Memory(store)
        self.addCleanup(memory.state.close)
        key = memory.projects.see(repo)
        old = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, old)
        with mock.patch.object(cli, "_memory_handle", return_value=memory), mock.patch("builtins.print") as shown:
            self.assertEqual(cli._route(["pin", "large"]), 0)
            self.assertEqual(memory.state.get(routing.pin_key(key)), "large")
            memory.state.put("route:log", [{"ts": 1.0, "key": "k", "kind": "main", "tier": "large", "from": "medium", "why": "w", "move": "none", "skip": ""}])
            self.assertEqual(cli._route([]), 0)
            text = "\n".join(str(c.args[0]) for c in shown.call_args_list)
            self.assertIn("pin        large", text)
            self.assertIn(f"project    {key}", text)
            self.assertIn("large  none     applied: w", text)
            self.assertEqual(cli._route(["pin", "off"]), 0)
            self.assertIsNone(memory.state.get(routing.pin_key(key)))
            self.assertEqual(cli._route(["pin", "huge"]), 2)


class ReportTest(unittest.TestCase):
    def test_the_report_counts_tiers_moves_and_tokens_per_model(self):
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 70, "output_tokens": 5}
        records = [
            {"kind": "messages", "model": MEDIUM, "answered": SMALL, "usage": usage,
             "route": {"tier": "small", "model": SMALL, "move": "down", "first": True}},
            {"kind": "messages", "model": MEDIUM, "answered": SMALL, "usage": usage, "route": {"tier": "small", "model": SMALL, "move": "down"}},
            {"kind": "messages", "model": MEDIUM, "answered": MEDIUM, "usage": usage,
             "route_kept": {"tier": "medium", "model": MEDIUM, "move": "held", "first": True}},
            {"kind": "messages", "model": MEDIUM, "answered": LARGE, "usage": usage,
             "route": {"tier": "large", "model": LARGE, "move": "up", "first": True}},
            {"kind": "messages", "model": "x", "usage": usage},
        ]
        got = report.routing(records)
        self.assertEqual(got["tiers"], {"small": 2, "medium": 1, "large": 1})
        self.assertEqual(got["moves"], {"down": 1, "held": 1, "up": 1})
        self.assertEqual(got["unrouted"], 1)
        self.assertEqual(got["models"][SMALL], 210)
        self.assertIn("moves      down 1, held 1, up 1", report.render_routing(records))
        self.assertEqual(report.render_routing([]), "")

    def test_the_log_carries_the_route_and_the_model_that_answered(self):
        rig = Rig(respond=lambda req: (200, {"content-type": "text/event-stream"}, sse(model=SMALL)),
                  layers=[SetProject(), RoutingLayer()], state=State(os.path.join(tempfile.mkdtemp(), "a.db")), overrides={"routing.mode": "all"})
        self.addCleanup(rig.close)
        rig.messages(body(convo([{"text": "list the files in src"}]), MEDIUM), extra=MAIN)
        record = rig.records()[-1]
        self.assertEqual((record["answered"], record["route"]["tier"], record["route"]["first"]), (SMALL, "small", True))
        self.assertEqual(record["model"], MEDIUM)  # what Claude Code asked for


def thread(task, errors=0):
    """A sub-agent's own messages: its task, then `errors` failed tool calls."""
    out = [human(task)]
    for i in range(errors):
        out += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"e{i}", "name": "Bash", "input": {"command": f"try {i}"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"e{i}", "content": "boom", "is_error": True}]}]
    return out


class SubagentFailureTest(Case):
    def sub(self, task, model, errors, agent="agent-1", kind="custom"):
        self.rig.messages(body(thread(task, errors), model), extra=sub_headers(agent, kind))
        sent = self.rig.sent()
        return sent["model"], sent["output_config"]["effort"], said(sent)

    def test_failures_never_move_a_subagent_off_its_first_model(self):
        for n, (task, model) in enumerate((("Difficulty: simple\nRead README.md.", SMALL), ("Difficulty: standard\nAdd a retry in net.py.", MEDIUM),
                            ("Difficulty: hard\nFind why the cache returns stale rows.", LARGE))):
            effort = self.rig.proxy.cfg["routing"]["effort"][routing._tier_of(self.rig.proxy.cfg["routing"], model)]
            self.assertEqual([self.sub(task, MEDIUM, e, agent=f"s{n}") for e in (0, 3, 6)],
                             [(model, effort, None), (model, effort, "high"), (model, effort, "high")])

    def test_an_agent_files_effort_is_its_start(self):
        routing._agent_efforts["deep-reviewer"] = ("medium", "")
        try:
            self.assertEqual(self.sub("Difficulty: standard\nReview net.py.", MEDIUM, 0, kind="deep-reviewer"), (MEDIUM, "medium", None))
            self.start(**{"routing.effort": {"small": "low", "medium": "low", "large": "low"}})
            self.assertEqual(self.sub("Difficulty: standard\nReview net.py.", MEDIUM, 3, agent="b", kind="deep-reviewer"), (MEDIUM, "medium", "high"))
        finally:
            routing._agent_efforts.pop("deep-reviewer", None)

    def test_a_task_that_says_it_failed_keeps_its_model_and_raises_effort(self):
        self.assertEqual(self.sub("that didn't work, try again\nread README.md and report its first line.", SMALL, 0), (SMALL, "medium", "high"))

    def test_a_fork_never_moves(self):
        self.assertEqual(self.sub("Add a retry to fetch() in net.py.", SMALL, 6, kind="fork")[:2], (SMALL, "high"))


def routing_default_route_inherited():
    from getalmanac import config
    return config.DEFAULTS["routing"]["route_inherited"]


class AgentFloorTest(Case):
    def sub(self, task, model, **kw):
        self.rig.messages(body([human(task)], model), extra=sub_headers(**kw))
        return self.rig.sent()["model"]

    def test_an_agents_own_model_is_a_floor(self):
        self.send(convo([{"text": "hello"}]), model=MEDIUM)  # the parent runs on Sonnet; this agent asked for Opus
        self.assertEqual(self.sub("Add a retry to fetch() in net.py.", LARGE), LARGE)
        self.assertEqual(self.sub("Difficulty: simple\nlist the files", LARGE, agent="b"), LARGE)

    def test_a_task_that_needs_more_still_moves_an_inheriting_agent_up(self):
        self.send(convo([{"text": "hello"}]), model=SMALL)
        self.assertEqual(self.sub("Difficulty: hard\nfind the race", SMALL), LARGE)

    def test_an_agents_own_model_is_never_moved_up(self):
        self.send(convo([{"text": "hello"}]), model=LARGE)  # the parent runs on Opus; this agent's file names Sonnet
        task = "Review a.py, b.py, c.py, d.py and e.py for bugs."
        self.assertEqual(routing.classify(task, agent=True)[0], "large")  # the task alone would go to Opus
        self.assertEqual(self.sub(task, MEDIUM), MEDIUM)
        self.assertEqual(self.sub("Difficulty: hard\nfind the race", MEDIUM, agent="b"), MEDIUM)

    def test_an_agent_that_only_inherited_the_parents_model_keeps_it_unless_route_inherited_is_on(self):
        self.send(convo([{"text": "hello"}]), model=LARGE)
        self.assertEqual(self.sub("Difficulty: simple\nlist the files", LARGE), SMALL)  # on in these tests: routed down
        self.start(**{"routing.route_inherited": False})  # the default: the plain floor
        self.assertFalse(routing_default_route_inherited())
        self.send(convo([{"text": "hello"}]), model=LARGE)
        self.assertEqual(self.sub("Difficulty: simple\nlist the files", LARGE, agent="b"), LARGE)
        self.assertEqual(self.sub("Difficulty: simple\nlist the files", LARGE, agent="c", kind="Explore"), LARGE)

    def test_an_inheriting_agent_with_no_difficulty_line_starts_on_medium(self):
        self.start(**{"routing.route_inherited": False})  # the default; the floor must not lift the default back to Opus
        self.send(convo([{"text": "hello"}]), model=LARGE)
        self.assertEqual(self.sub("Look around the repo and tell me what it does.", LARGE), MEDIUM)
        self.assertEqual(self.sub("Difficulty: simple\nlist the files", LARGE, agent="b"), LARGE)  # a marked task keeps the floor
        self.assertEqual(self.sub("Difficulty: hard\nfind the race", LARGE, agent="c"), LARGE)

    def test_an_agent_with_its_own_model_keeps_it_without_a_difficulty_line(self):
        self.start(**{"routing.route_inherited": False})
        self.send(convo([{"text": "hello"}]), model=MEDIUM)  # the parent runs on Sonnet; this agent asked for Opus
        self.assertEqual(self.sub("Look around the repo and tell me what it does.", LARGE), LARGE)

    def test_a_pin_overrides_the_floor(self):
        self.send(convo([{"text": "hello"}]), model=MEDIUM)
        self.state.put(routing.pin_key("proj1"), "small")
        self.assertEqual(self.sub("Add a retry to fetch() in net.py.", LARGE), SMALL)


class MainEffortOnlyTest(Case):
    mode = "subagents"

    def ask(self, turns, effort="low", model=LARGE):
        raw = body(convo(turns), model)
        raw["output_config"]["effort"] = effort
        self.rig.messages(raw, extra=MAIN)
        return self.rig.sent()

    def said(self, sent):
        last = sent["messages"][-1]
        return last["output_config"]["effort"] if last["role"] == "system" else None

    def test_effort_is_raised_by_message_after_a_failure_until_two_clean_requests(self):
        loop = [human("design a plugin system")]
        for i in range(4):
            loop += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"try {i}"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x", "is_error": i < 3}]}]
        said = []
        for n in (7, 9):  # three errors then clean results, as the loop grows
            sent = self.ask_raw(loop[:n])
            said.append(self.said(sent))
        self.assertEqual(said[0], "high")  # the third error
        self.assertEqual((sent["model"], sent["output_config"]["effort"]), (LARGE, "low"))
        self.assertTrue(self.rig.upstream.requests[-1]["headers"]["anthropic-beta"].endswith(routing.EFFORT_BETA))
        self.assertEqual(self.said(self.ask_raw(loop[:9])), said[1])  # a replay is answered the same way
        self.assertEqual(said[1], "high")  # one clean result is not enough
        loop += [{"role": "assistant", "content": [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "ok"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "x"}]}]
        self.assertIsNone(self.said(self.ask_raw(loop)))  # the second clean one turns it off

    def ask_raw(self, messages):
        raw = body(messages, LARGE)
        raw["output_config"]["effort"] = "low"
        self.rig.messages(raw, extra=MAIN)
        return self.rig.sent()

    def test_the_raise_drops_back_after_calm_after_clean_requests(self):
        self.start(**{"routing.calm_after": 3})
        loop = [human("design a plugin system")]
        for i in range(6):
            loop += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": f"try {i}"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x", "is_error": i < 3}]}]
        said = [self.said(self.ask_raw(loop[:n])) for n in (7, 9, 11, 13)]
        self.assertEqual(said, ["high", "high", "high", None])  # three errors, then off only at the third clean request

    def test_nothing_is_added_without_a_failure_or_when_the_client_asks_for_high(self):
        self.assertIsNone(self.said(self.ask([{"text": "design a plugin system"}])))
        turns = [{"text": "design a plugin system", "errors": 3}, {"text": "a"}]
        self.assertIsNone(self.said(self.ask(turns, "high")))


class BetaRefusedTest(Case):
    """An upstream that refuses the effort message: the effort goes in output_config, after compacting a large history."""
    mode = "subagents"

    def run_case(self, turns, refuse_all=False):
        from getalmanac.compact import CompactLayer

        def respond(req):
            data = json.loads(req["body"])
            if data["messages"][-1]["role"] == "system" or refuse_all and data["output_config"]["effort"] != "low":
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"unsupported"}}'
            return 200, {"content-type": "text/event-stream"}, sse()
        self.before = [CompactLayer(Memory(self.store, self.state))]
        self.start(respond=respond, **{"compaction.trigger_tokens": 10 ** 6, "compaction.target_tokens": 1500,
                                       "memory.recall": False, "memory.summary_model": ""})
        raw = body(convo(turns), LARGE)
        raw["output_config"]["effort"] = "low"
        status, _, _ = self.rig.messages(raw, extra=MAIN)
        self.assertEqual(status, 200)
        return raw

    def last(self):
        return self.rig.sent(), self.rig.upstream.requests[-1]["headers"]["anthropic-beta"]

    def test_when_every_rewrite_is_refused_the_original_goes_out_with_its_own_header(self):
        raw = self.run_case([{"text": "design a plugin system"}, {"text": "that's wrong, try again"}], refuse_all=True)
        self.assertEqual(len(self.rig.upstream.requests), 3)  # the message, then output_config, then the original
        self.assertEqual(self.last(), (raw, "a,b,c"))

    def test_a_small_history_gets_the_effort_in_output_config(self):
        raw = self.run_case([{"text": "design a plugin system"}, {"text": "that's wrong, try again"}])
        self.assertEqual(len(self.rig.upstream.requests), 2)
        raw["output_config"]["effort"] = "high"
        self.assertEqual(self.last(), (raw, "a,b,c"))

    def test_a_large_history_is_compacted_first_and_later_requests_skip_the_message(self):
        turns = [{"text": f"step {i}", "pad": 3000} for i in range(6)] + [{"text": "that's wrong, try again"}]
        raw = self.run_case(turns)
        sent, beta = self.last()
        self.assertEqual(len(self.rig.upstream.requests), 2)
        self.assertIn("Almanac compacted this conversation", json.dumps(sent["messages"][0]))
        self.assertEqual((sent["output_config"]["effort"], sent["messages"][-1]["role"], beta), ("high", "user", "a,b,c"))
        self.rig.messages(raw, extra=MAIN)  # the same lane again: straight to the fallback, in one request
        self.assertEqual(len(self.rig.upstream.requests), 3)
        self.assertEqual(self.rig.sent(), sent)

    def test_a_large_subagent_whose_effort_message_is_refused_gets_it_in_output_config(self):
        def respond(req):
            if json.loads(req["body"])["messages"][-1]["role"] == "system":
                return 400, {"content-type": "application/json"}, b'{"type":"error","error":{"message":"unsupported"}}'
            return 200, {"content-type": "text/event-stream"}, sse()
        self.start(respond=respond)
        loop = [human("Difficulty: standard" + chr(10) + "fix the failing test " + "x" * 200000)]
        for i in range(3):
            loop += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "pytest"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x", "is_error": True}]}]
        raw = body(loop, MEDIUM)
        raw["output_config"]["effort"] = "medium"
        self.assertEqual(self.rig.messages(raw, extra=sub_headers("big"))[0], 200)
        sent = self.rig.sent()
        self.assertEqual(len(self.rig.upstream.requests), 2)  # the message, then output_config
        self.assertEqual((sent["model"], sent["output_config"]["effort"], sent["messages"][-1]["role"]), (MEDIUM, "high", "user"))
        self.rig.messages(raw, extra=sub_headers("big"))  # the same lane again: straight to output_config
        self.assertEqual(len(self.rig.upstream.requests), 3)
        self.assertEqual(self.rig.sent()["output_config"]["effort"], "high")


class ConfigCheckTest(unittest.TestCase):
    def load(self, **routing):
        from getalmanac import config
        with mock.patch.object(config, "_file", return_value={"routing": routing}):
            return config.load()["routing"], config.DEFAULTS["routing"]

    def test_removed_keys_left_in_a_config_load_and_are_ignored(self):
        got, default = self.load(ladders={"small": [["medium", "low"]]}, margin=0.5, prices={"small": {}}, work={}, effort_cost={"low": 1})
        self.assertEqual(got["effort"], default["effort"])

    def test_a_malformed_effort_falls_back_to_the_default(self):
        for bad in ({"large": "ultra"}, ["low"]):
            got, default = self.load(effort=bad)
            self.assertEqual(got["effort"], default["effort"])
        got, _ = self.load(effort={"large": ""})
        self.assertEqual(got["effort"]["large"], "")


if __name__ == "__main__":
    unittest.main()
