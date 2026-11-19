import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

from helpers import Rig, SESSION, sse

from getalmanac import config, embed, mcp, project
from getalmanac.memory import Memory, MemoryLayer, ProjectLayer, exchange, redact
from getalmanac.store import Store


def make_repo(parent, name, remote=None):
    root = os.path.join(parent, name)
    os.makedirs(os.path.join(root, ".git"))
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, ".git", "config"), "w") as handle:
        handle.write("[core]\n\tbare = false\n")
        if remote:
            handle.write(f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    return project.normalize(root)


def human(text):
    return {"role": "user", "content": [{"type": "text", "text": "<system-reminder>rules</system-reminder>"},
                                        {"type": "text", "text": text}]}


def said(text, tool=None):
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "id": "t1", "name": tool[0], "input": tool[1]})
    return {"role": "assistant", "content": content}


def result():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}


def reply(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop, "usage": {}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-mem-")
        self.store = Store(os.path.join(self.tmp, "a.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.alpha = make_repo(self.work, "alpha", "https://github.com/Someone/Alpha.git")
        self.beta = make_repo(self.work, "beta", "git@github.com:someone/beta.git")


class ProjectTest(Case):
    def test_identity_comes_from_the_remote_in_either_url_form(self):
        self.assertEqual(project.describe(self.alpha + "/src")[:2], ("git:github.com/someone/alpha", "alpha"))
        self.assertEqual(project.describe(self.beta)[0], "git:github.com/someone/beta")

    def test_credentials_in_a_remote_url_are_dropped(self):
        root = make_repo(self.work, "gamma", "https://user:tok" + "en@example.com/team/gamma.git")
        self.assertEqual(project.describe(root)[0], "git:example.com/team/gamma")

    def test_repo_without_remote_and_plain_folder(self):
        root = make_repo(self.work, "local")
        self.assertEqual(project.describe(root)[0], "dir:" + root.lower())
        plain = project.normalize(os.path.join(self.tmp, "plain"))
        os.makedirs(plain)
        self.assertEqual(project.describe(plain)[0], "dir:" + plain.lower())

    def test_worktree_resolves_to_the_main_checkout(self):
        tree = os.path.join(self.work, "alpha-wt")
        os.makedirs(tree)
        with open(os.path.join(tree, ".git"), "w") as handle:
            handle.write(f"gitdir: {self.alpha}/.git/worktrees/wt\n")
        self.assertEqual(project.describe(tree)[0], "git:github.com/someone/alpha")

    def test_two_clones_of_one_remote_are_one_project(self):
        other = make_repo(os.path.join(self.tmp), "alpha-copy", "https://github.com/someone/alpha")
        self.assertEqual(project.describe(other)[0], project.describe(self.alpha)[0])

    def test_tool_calls_vote_for_the_repository_they_touch(self):
        group = [human("fix it"),
                 said("on it", ("Edit", {"file_path": self.beta + "/src/x.py"})), result(),
                 said("and", ("Bash", {"command": f'cd "{self.beta}" && git status && cat {self.alpha}/README.md'})), result(),
                 said("more", ("Read", {"file_path": self.beta + "/src/y.py"}))]
        self.assertEqual(project.vote(project.tool_paths(group)),
                         {"git:github.com/someone/beta": 3, "git:github.com/someone/alpha": 1})
        self.assertEqual(self.memory.projects.for_turn(group, "fallback"), "git:github.com/someone/beta")
        self.assertEqual(self.memory.projects.for_turn([human("hi"), said("hello")], "fallback"), "fallback")

    def test_start_uses_a_named_sibling_repository_over_the_folder(self):
        projects = self.memory.projects
        self.assertEqual(projects.for_start(self.alpha, "fix the failing test"), "git:github.com/someone/alpha")
        self.assertEqual(projects.for_start(self.alpha, "Use the Beta repo for this"), "git:github.com/someone/beta")
        self.assertEqual(projects.for_start(self.alpha, f"look at {self.beta}/src/x.py"), "git:github.com/someone/beta")
        self.assertEqual(projects.for_start(self.alpha, "alphabet soup"), "git:github.com/someone/alpha")

    def test_merge_moves_memory_and_future_lookups(self):
        self.memory.index("lane-a", "s", self.memory.projects.see(self.alpha), [human("alpha question about caching layers")], reply("done"))
        self.memory.projects.see(self.beta)
        self.memory.merge_projects("git:github.com/someone/alpha", "git:github.com/someone/beta")
        self.assertEqual(self.memory.projects.see(self.alpha), "git:github.com/someone/beta")
        self.assertEqual(len(self.memory.search("caching layers", project="git:github.com/someone/beta")), 1)

    @unittest.skipUnless(os.name == "nt", "drive letters only exist on Windows")
    def test_git_bash_paths_are_normalised(self):
        self.assertEqual(project.normalize("/c/Users/x/repo/"), "C:/Users/x/repo")
        self.assertEqual(project.normalize("c:\\Users\\x"), "C:/Users/x")


class EmbedTest(unittest.TestCase):
    def test_related_text_scores_above_unrelated(self):
        a = embed.embed("fix the proxy cache invalidation bug")
        self.assertGreater(embed.cosine(a, embed.embed("the cache in the proxy was invalidated wrongly")),
                           embed.cosine(a, embed.embed("bake sourdough bread with rye flour")) + 0.2)

    def test_pack_round_trips(self):
        vec = embed.embed("hello world of vectors")
        self.assertAlmostEqual(embed.cosine(vec, embed.unpack(embed.pack(vec))), 1.0, places=5)


class IndexTest(Case):
    def convo(self):
        return [human("Why does the proxy drop the cache after a tool loads?"),
                said("Looking", ("Read", {"file_path": self.alpha + "/src/proxy.py"})), result(),
                said("The proxy rewrote the tools array, which sits at the start of the cached prefix."),
                human("Add a test for the cache staying warm in the proxy"),
                said("Writing", ("Write", {"file_path": self.alpha + "/tests/test_cache.py"})), result()]

    def test_finished_exchanges_are_stored_with_answer_tools_and_files(self):
        key = self.memory.projects.see(self.alpha)
        self.assertEqual(self.memory.index("lane-1", "s1", key, self.convo(), reply("Added test_cache.py; it passes.")), 2)
        hit = self.memory.search("tools array cached prefix", project=key)[0]
        self.assertIn("Why does the proxy drop the cache", hit["title"])
        node = self.memory.read(hit["id"])
        self.assertIn("Answer: The proxy rewrote the tools array", node["text"])
        self.assertIn("Tools: Read", node["text"])
        self.assertIn("src/proxy.py", node["text"])
        self.assertNotIn("rules", node["text"])

    def test_an_unfinished_turn_waits(self):
        key = self.memory.projects.see(self.alpha)
        self.assertEqual(self.memory.index("lane-1", "s1", key, self.convo(), reply("", stop="tool_use")), 1)
        self.assertEqual(self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it.")), 1)
        self.assertEqual(self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it.")), 0)

    def test_similar_exchanges_share_a_topic_and_others_do_not(self):
        key = self.memory.projects.see(self.alpha)
        messages = self.convo() + [said("Added the cache test."), human("Now bake sourdough bread with rye flour and a long proof")]
        self.memory.index("lane-1", "s1", key, messages, reply("Mix flour, water and starter."))
        convo = self.store.run("select id from convo")[0]["id"]
        outline = self.memory.outline(convo)
        self.assertEqual([len(t["children"]) for t in outline], [2, 1])
        topic = self.memory.read(outline[0]["id"])
        self.assertIn("Why does the proxy drop the cache", topic["text"])

    def test_rewinding_drops_later_exchanges(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it."))
        self.memory.index("lane-1", "s1", key, self.convo()[:3], reply("A different answer about the prefix."))
        self.assertEqual(self.memory.counts()["exchanges"], 1)
        self.assertEqual(self.memory.search("test cache warm", project=key), [])

    def test_a_conversation_resumed_under_a_new_session_is_stored_once(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it."))
        more = self.convo() + [said("Added it."), human("Explain the cache key format in the proxy")]
        self.assertEqual(self.memory.index("lane-2", "s2", key, more, reply("It hashes the prefix.")), 1)
        self.assertEqual(self.memory.counts(), {"convos": 1, "exchanges": 3, "topics": self.memory.counts()["topics"]})
        self.assertEqual(sorted(self.memory.leaves("lane-2")), [0, 1, 2])
        # The old session going on its own way afterwards is kept whole, beside the resumed one.
        other = self.convo() + [said("Added it."), human("Rename the proxy module to gateway")]
        self.memory.index("lane-1", "s1", key, other, reply("Renamed."))
        self.assertEqual(self.memory.counts()["exchanges"], 6)
        self.assertEqual(sorted(self.memory.leaves("lane-1")), [0, 1, 2])

    def test_a_resumed_conversation_compacted_on_its_first_request_is_stored_once(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it."))
        more = self.convo() + [said("Added it."), human("Explain the cache key format in the proxy")]
        self.assertEqual(self.memory.index("lane-2", "s2", key, more[:-1], finished=True), 0)  # what compaction does
        self.assertEqual(self.memory.counts()["convos"], 1)
        self.assertEqual(sorted(self.memory.leaves("lane-2")), [0, 1])

    def test_same_first_prompt_with_a_different_answer_stays_separate(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo()[:3], reply("The tools array changed."))
        self.memory.index("lane-2", "s2", key, self.convo()[:3], reply("A hook rewrote the system prompt."))
        self.assertEqual(self.memory.counts()["convos"], 2)
        self.assertEqual(self.memory.counts()["exchanges"], 2)

    def test_a_new_conversation_with_the_same_opening_stays_separate(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo()[:3], reply("The tools array changed."))
        self.memory.index("lane-2", "s2", key, self.convo()[:3], reply("The tools array changed."))
        later = self.convo()[:3] + [said("The tools array changed."), human("Now bake sourdough bread with rye")]
        self.memory.index("lane-2", "s2", key, later, reply("Mix flour and starter."))
        self.assertEqual(self.memory.counts()["convos"], 2)
        self.assertEqual(self.memory.counts()["exchanges"], 3)

    def test_search_needs_two_words_and_respects_the_project(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it."))
        self.assertEqual(self.memory.search("proxy sourdough", project=key), [])
        self.assertEqual(self.memory.search("proxy cache", project="git:github.com/someone/beta"), [])
        self.assertEqual(len(self.memory.search("proxy cache", project=None)), 2)
        self.assertEqual(self.memory.search("proxy cache", project=key, exclude_lane="lane-1"), [])

    def test_work_done_from_another_folder_lands_in_the_right_project(self):
        start = self.memory.projects.see(self.alpha)  # the session started in alpha
        messages = [human("Tidy the retry helper"),
                    said("Editing", ("Edit", {"file_path": self.beta + "/src/retry.py"})), result()]
        self.memory.index("lane-9", "s9", start, messages, reply("Simplified the retry helper backoff."))
        self.assertEqual(self.memory.search("retry helper", project=start), [])
        self.assertEqual(len(self.memory.search("retry helper", project="git:github.com/someone/beta")), 1)
        self.assertEqual(self.store.run("select project from convo")[0]["project"], "git:github.com/someone/beta")

    def test_secrets_are_removed_before_storing(self):
        fake = "ghp_" + "a1B2" * 9
        text = redact(f"token is {fake} and password = hunter2hunter2 ok")
        self.assertNotIn(fake, text)
        self.assertNotIn("hunter2hunter2", text)
        record = exchange([human(f"my key is sk-{'x' * 30}"), said("noted")])
        self.assertNotIn("x" * 30, record["text"])

    def test_more_secret_shapes_are_removed(self):
        for line, secret in (("DB_PASS=s3cretValue99", "s3cretValue99"), ("AccountKey=abcdEFGH1234abcdEFGH1234==", "abcdEFGH1234"),
                             ("STRIPE=whsec_abcdefghijklmnop1234", "abcdefghijklmnop1234"), ("dbpassword=hunter2hunter2", "hunter2"),
                             ("PRIVATE_KEY=0x4c0883a69102937d62", "4c0883a69102937d62"),
                             (json.dumps({"command": 'export SECRET="topsecretvalue"'}), "topsecretvalue"),
                             ("https://hooks.slack.com/services/T0000/B0000/XXXXXXXXXXXX", "XXXXXXXXXXXX"),
                             ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA12345", "MIIEpAIBAAKCAQEA")):
            self.assertNotIn(secret, redact(line), line)
        for line in ("BYPASS=something12", "compass: northwest", "max_tokens: 150000", "passes: everything"):
            self.assertEqual(redact(line), line)

    def test_prune_removes_only_old_conversations(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-old", "s1", key, self.convo(), reply("Added it."))
        self.memory.index("lane-new", "s2", key, [human("a fresh question about proxies")], reply("fresh answer"))
        self.store.run("update convo set updated = updated - 400 * 86400 where lane = 'lane-old'")
        self.assertEqual(self.memory.prune(0), 0)
        self.assertEqual(self.memory.prune(365), 1)
        self.assertEqual(self.memory.counts()["convos"], 1)

    def test_forget(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, self.convo(), reply("Added it."))
        self.store.run("update convo set updated = updated - 7200")  # closed: not changed in the last hour
        self.assertEqual(self.memory.forget(project=key), 1)
        self.assertEqual(self.memory.counts(), {"convos": 0, "exchanges": 0, "topics": 0})
        self.assertEqual(self.memory.search("proxy cache"), [])


class RecallTest(Case):
    def setUp(self):
        super().setUp()
        self.key = self.memory.projects.see(self.alpha)
        old = [human("Why does the proxy drop the cache after a tool loads?"),
               said("The proxy rewrote the tools array, which sits at the start of the cached prefix.")]
        self.memory.index("old-lane", "old", self.key, old[:1], reply(old[1]["content"][0]["text"]))
        self.rig = Rig(layers=[ProjectLayer(self.memory), MemoryLayer(self.memory)], store=self.store)
        self.addCleanup(self.rig.close)

    def body(self, prompt, cwd=None, extra=()):
        env = f"# Environment\n - Primary working directory: {cwd or self.alpha}\n"
        return {"model": "m", "tools": [{"name": "Bash", "description": "x", "input_schema": {"type": "object"}}],
                "messages": [human(prompt), {"role": "system", "content": [{"type": "text", "text": env}]}, *extra]}

    def test_related_memory_is_added_before_the_prompt(self):
        self.rig.messages(self.body("the proxy cache drops again after loading a tool"))
        content = self.rig.sent()["messages"][0]["content"]
        self.assertEqual(content[-1]["text"], "the proxy cache drops again after loading a tool")
        note = content[-2]["text"]
        self.assertTrue(note.startswith("<system-reminder>\nAlmanac memory for this project (alpha)"))
        self.assertIn("rewrote the tools array", note)
        self.assertIn("Most recent conversations:", note)
        self.assertEqual(self.rig.records()[-1]["memory"]["recall_chars"], len(note))

    def test_memory_and_routing_classify_a_request_alike(self):
        odd = {"x-claude-code-request-class": "classifier"}  # neither main nor sub: no note, no stored lane
        self.rig.messages(self.body("the proxy cache drops again after loading a tool"), extra=odd)
        self.assertNotIn("memory", self.rig.records()[-1])
        self.assertIsNone(self.store.get(f"mainlane:{SESSION}"))
        self.rig.messages(self.body("the proxy cache drops again after loading a tool"))
        self.assertIn("memory", self.rig.records()[-1])

    def test_recall_is_frozen_even_as_memory_grows(self):
        request = self.body("the proxy cache drops again after loading a tool")
        self.rig.messages(request)
        first = self.rig.upstream.requests[-1]["body"]
        self.memory.index("other", "o", self.key, [human("proxy cache tool question number two")], reply("another answer"))
        self.rig.messages(request)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], first)

    def test_another_project_gets_nothing(self):
        raw = json.dumps(self.body("the proxy cache drops again", cwd=self.beta)).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_naming_the_project_from_another_folder_recalls_it(self):
        self.memory.projects.see(self.beta)
        self.rig.messages(self.body("In the alpha repo the proxy cache drops again after a tool loads", cwd=self.beta))
        self.assertIn("rewrote the tools array", self.rig.sent()["messages"][0]["content"][-2]["text"])

    def test_the_conversation_is_indexed_after_the_reply(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse("Use a sticky tool set."))
        self.rig.messages(self.body("How should deferred tools be remembered between turns?"))
        self.rig.records()
        hits = self.memory.search("deferred tools remembered", project=self.key)
        self.assertEqual(len(hits), 1)
        self.assertIn("Use a sticky tool set.", self.memory.read(hits[0]["id"])["text"])

    def test_a_tool_inner_model_call_is_not_a_conversation(self):
        body = self.body("Perform a web search for the query: csv sniffer")
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]  # no tool the client runs
        raw = json.dumps(body).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)
        self.rig.records()
        self.assertEqual(self.memory.store.run("select count(*) n from convo")[0]["n"], 1)  # only the one made in setUp

    def test_subagents_and_toolless_requests_are_left_alone(self):
        body = self.body("the proxy cache drops again after loading a tool")
        raw = json.dumps(body).encode()
        self.rig.messages(raw, extra={"x-claude-code-agent-id": "agent-7"})
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)
        del body["tools"]
        raw = json.dumps(body).encode()
        self.rig.messages(raw)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)


class SubagentTest(Case):
    """A sub-agent is told which conversation it belongs to, and its finished work is filed under that conversation."""

    tools = [{"name": "Bash", "description": "x", "input_schema": {"type": "object"}}]
    HEADERS = {"x-claude-code-agent-id": "agent-7", "x-claude-code-agent-type": "general-purpose", "x-claude-code-request-class": "subagent"}

    def setUp(self):
        super().setUp()
        self.key = self.memory.projects.see(self.alpha)
        self.rig = Rig(layers=[ProjectLayer(self.memory), MemoryLayer(self.memory)], store=self.store)
        self.addCleanup(self.rig.close)
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse("Found the retry helper in net.py."))
        # the main conversation, stored by its first reply
        self.memory.index("main-lane", SESSION, self.key, [human("Find out why requests are retried twice")], reply("Looking.", "tool_use"))
        self.parent = self.store.run("select id from convo where lane = 'main-lane'")[0]["id"]
        self.store.put(f"mainlane:{SESSION}", "main-lane")  # what the main thread's own request leaves behind

    def body(self, prompt="Search the code for the retry helper and report where it lives"):
        env = f"# Environment\n - Primary working directory: {self.alpha}\n"
        return {"model": "m", "tools": self.tools, "messages": [human(prompt), {"role": "system", "content": [{"type": "text", "text": env}]}]}

    def sub(self, body, **extra):
        self.rig.messages(body, extra=dict(self.HEADERS, **extra))
        return self.rig.sent()

    def note_of(self, sent):
        return [b["text"] for b in sent["messages"][0]["content"] if "subagent of conversation" in b.get("text", "")]

    def test_a_subagent_gets_the_note_and_the_fifth_request_is_byte_identical(self):
        first = self.sub(self.body())
        note = self.note_of(first)
        self.assertEqual(len(note), 1)
        self.assertIn(f"You are a subagent of conversation {self.parent}", note[0])
        self.assertIn(f'memory_search {{"conversation": {self.parent}}}', note[0])
        self.assertEqual(first["messages"][0]["content"][-1]["text"], "Search the code for the retry helper and report where it lives")
        self.assertEqual(self.rig.records()[-1]["memory"]["subnote_chars"], len(note[0]))
        for _ in range(3):
            self.sub(self.body())
        # a newer main conversation appears in the same session: the note this lane already has does not change
        self.memory.index("main-2", SESSION, self.key, [human("A different main task entirely")], reply("ok", "tool_use"))
        self.store.put(f"mainlane:{SESSION}", "main-2")
        self.rig.cfg["memory"]["tools"] = False  # nor does turning the tools off mid-run
        fifth = self.sub(self.body())
        self.assertEqual(self.rig.upstream.requests[-1]["body"], self.rig.upstream.requests[-5]["body"])
        self.assertEqual(fifth["messages"][0], first["messages"][0])

    def test_a_fork_gets_no_note_and_is_not_stored(self):
        raw = json.dumps(self.body()).encode()
        self.rig.messages(raw, extra={"x-claude-code-agent-id": "agent-8", "x-claude-code-agent-type": "fork"})
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)
        self.rig.records()
        self.assertEqual(self.store.run("select count(*) n from convo")[0]["n"], 1)

    def test_no_note_when_memory_is_off_or_the_parent_is_not_stored(self):
        raw = json.dumps(self.body()).encode()
        for cfg in ({"memory.enabled": False}, {"memory.recall": False}):
            rig = Rig(layers=[ProjectLayer(self.memory), MemoryLayer(self.memory)], store=self.store, overrides=cfg)
            self.addCleanup(rig.close)
            rig.messages(raw, extra=self.HEADERS)
            self.assertEqual(rig.upstream.requests[-1]["body"], raw)
        self.rig.messages(raw, session="99999999-0000-0000-0000-000000000000", extra=self.HEADERS)  # no stored conversation in this session
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)

    def test_a_finished_subagent_exchange_is_stored_under_the_parent_and_never_open(self):
        self.sub(self.body())
        self.rig.records()
        child = self.store.run("select * from convo where parent = ?", (self.parent,))
        self.assertEqual(len(child), 1)
        self.assertEqual(child[0]["session"], SESSION)
        leaves = self.store.run("select status, text from node where convo = ? and kind = 'leaf'", (child[0]["id"],))
        self.assertEqual(len(leaves), 1)
        self.assertIn("Found the retry helper in net.py.", leaves[0]["text"])
        self.assertEqual(leaves[0]["status"], "done")  # the rules would call the newest exchange open
        self.assertNotIn(child[0]["id"], [r["id"] for r in self.memory.recent(project=self.key, limit=10)])  # it is not a conversation of its own
        self.assertEqual(self.memory.status_counts()["open"], 0)

    def test_a_subagent_exchange_writes_a_library_row_that_no_search_shows(self):
        from getalmanac import library
        prompt = "Search the code for the retry helper and report where it lives"
        body = self.body(prompt)
        for n in range(5):
            body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"u{n}", "name": "Read", "input": {"file_path": f"/a/{n}.py"}}]},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{n}", "content": "ok"}]}]
        self.sub(body)
        self.rig.records()
        child = self.store.run("select id from convo where parent = ?", (self.parent,))[0]["id"]
        self.assertEqual([(r["convo"], r["calls"]) for r in self.store.run("select convo, calls from library")], [(child, 5)])
        self.assertEqual((library.search(self.store, prompt, self.key), library.listing(self.store), library.candidates(self.store)), ([], [], []))

    def test_a_model_summary_cannot_reopen_a_subagent_exchange(self):
        self.sub(self.body())
        self.rig.records()
        leaf = self.store.run("select n.id from node n join convo c on c.id = n.convo where c.parent = ? and n.kind = 'leaf'", (self.parent,))[0]["id"]
        self.memory.set_summary(leaf, "Retry helper", "It lives in net.py.", status="open")
        self.assertEqual(self.store.run("select status from node where id = ?", (leaf,))[0]["status"], "done")

    def test_a_subagent_that_is_still_working_stores_nothing(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse("Checking.", tool=("Bash", {"command": "ls"})))
        self.sub(self.body())
        self.rig.records()
        self.assertEqual(self.store.run("select count(*) n from node where kind = 'leaf'")[0]["n"], 0)

    def test_a_subagent_ending_on_subagenthandback_is_stored_with_the_report_as_its_answer(self):
        report = "The retry helper lives in net.py, line 12. Token sk-abcdefghijklmnopqrstuvwxyz123456 was in its config."
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse("Done looking.", tool=("SubagentHandback", {"message": report})))
        self.sub(self.body())
        self.rig.records()
        child = self.store.run("select * from convo where parent = ?", (self.parent,))
        self.assertEqual(len(child), 1)
        leaves = self.store.run("select status, text from node where convo = ? and kind = 'leaf'", (child[0]["id"],))
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["status"], "done")
        self.assertIn("Answer: The retry helper lives in net.py, line 12.", leaves[0]["text"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", leaves[0]["text"])  # redacted like any stored text

    def test_a_subagent_calling_other_tools_beside_the_handback_is_still_working(self):
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse("Checking.", tool=("Bash", {"command": "ls"})))
        self.sub(self.body())
        self.rig.records()
        self.assertEqual(self.store.run("select count(*) n from node where kind = 'leaf'")[0]["n"], 0)
        reply_mixed = {"content": [{"type": "tool_use", "id": "a", "name": "SubagentHandback", "input": {"message": "x"}},
                                   {"type": "tool_use", "id": "b", "name": "Bash", "input": {}}], "stop_reason": "tool_use"}
        self.assertIsNone(MemoryLayer._handback(reply_mixed))

    def test_a_subagent_that_starts_before_its_parent_is_stored_gets_the_note_later_and_is_filed_under_it(self):
        late = "99999999-aaaa-bbbb-cccc-000000000001"
        self.store.put(f"mainlane:{late}", "late-lane")  # the main thread has sent its request; its row is not written yet
        replies = [sse("Checking.", tool=("Bash", {"command": "ls"})), sse("Found the retry helper in net.py.")]
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, replies[min(len(self.rig.upstream.requests) - 1, 1)])
        raw = json.dumps(self.body()).encode()
        self.rig.messages(raw, session=late, extra=self.HEADERS)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)  # no parent yet: no note
        self.rig.records()
        self.assertEqual(self.store.run("select count(*) n from state where key like 'subparent:%'")[0]["n"], 0)  # nothing was frozen
        self.memory.index("late-lane", late, self.key, [human("Find out why requests are retried twice")], reply("Looking.", "tool_use"))
        late_id = self.store.run("select id from convo where lane = 'late-lane'")[0]["id"]
        self.rig.messages(self.body(), session=late, extra=self.HEADERS)
        self.assertEqual(len(self.note_of(self.rig.sent())), 1)
        self.assertIn(f"conversation {late_id};", self.note_of(self.rig.sent())[0])
        self.rig.records()
        self.assertEqual([r["parent"] for r in self.store.run("select parent from convo where lane not in ('main-lane', 'late-lane')")], [late_id])

    def test_a_subagent_after_clear_is_filed_under_the_new_conversation(self):
        self.rig.messages(self.body("A completely new task after clearing the chat"))  # the new main conversation, same session
        self.rig.records()
        new = self.store.run("select id from convo where parent = 0 and lane != 'main-lane'")[0]["id"]
        self.assertNotEqual(new, self.parent)
        self.assertIn(f"conversation {new};", self.note_of(self.sub(self.body()))[0])
        self.rig.records()
        self.assertEqual([r["parent"] for r in self.store.run("select parent from convo where parent != 0")], [new])

    def test_a_subagent_started_just_after_clear_never_takes_the_old_conversation_as_its_parent(self):
        self.store.put(f"mainlane:{SESSION}", "new-lane")  # cleared: the new main thread has sent its request, its row is still queued
        raw = json.dumps(self.body()).encode()
        self.rig.messages(raw, extra=self.HEADERS)
        self.assertEqual(self.rig.upstream.requests[-1]["body"], raw)
        self.rig.records()
        self.assertEqual(self.store.run("select count(*) n from convo where parent != 0")[0]["n"], 0)

    def test_a_subagents_prompt_is_not_something_the_person_typed(self):
        self.sub(self.body())
        self.rig.records()
        self.assertEqual(self.memory.user_messages(project=self.key, query="retry helper"), [])
        child = self.store.run("select id from convo where parent = ?", (self.parent,))[0]["id"]
        self.assertEqual(len(self.memory.user_messages(convo_id=child)), 1)

    def test_subagent_hits_are_tagged_and_never_recalled_as_the_persons_request(self):
        self.sub(self.body())
        self.rig.records()
        found = mcp.call(self.memory, "memory_search", {"query": "retry helper report", "project": "all"}, self.alpha)
        self.assertIn(f"(sub-agent of conversation {self.parent})", found)
        self.assertNotIn("Search the code for the retry helper", self.memory.recall(self.key, "search the code for the retry helper report", "elsewhere", 2400))

    def test_a_main_thread_exchange_is_unaffected(self):
        self.rig.messages(self.body("Why do requests retry twice?"))
        self.assertEqual(self.note_of(self.rig.sent()), [])
        self.rig.records()
        self.assertEqual(self.store.run("select count(*) n from convo where parent != 0")[0]["n"], 0)

    def test_forgetting_the_parent_forgets_its_subagents(self):
        self.sub(self.body())
        self.rig.records()
        self.memory.forget(convo_id=self.parent, force=True)
        self.assertEqual(self.store.run("select count(*) n from convo")[0]["n"], 0)
        self.assertEqual(self.store.run("select count(*) n from node")[0]["n"], 0)

    def test_memory_search_can_be_limited_to_one_conversation(self):
        self.memory.index("other-lane", "o2", self.key, [human("Why do the requests retry twice in the helper?")], reply("A second retry wrapper."))
        self.sub(self.body())
        self.rig.records()
        everywhere = mcp.call(self.memory, "memory_search", {"query": "retry helper requests"}, self.alpha)
        self.assertIn("A second retry wrapper", everywhere)
        one = mcp.call(self.memory, "memory_search", {"query": "retry helper requests", "conversation": self.parent}, self.alpha)
        self.assertIn("Found the retry helper in net.py.", one)  # the sub-agent's work counts as the parent's
        self.assertNotIn("A second retry wrapper", one)
        listing = mcp.call(self.memory, "memory_search", {"conversation": self.parent}, self.alpha)  # the call the note names: no words
        self.assertIn("Found the retry helper in net.py.", listing)
        self.assertNotIn("A second retry wrapper", listing)

    def test_memory_search_checks_its_arguments(self):
        for bad in ("abc", True, -1, 1.5, [1], 2 ** 63):
            with self.assertRaises(ValueError, msg=repr(bad)):
                mcp.call(self.memory, "memory_search", {"query": "retry", "conversation": bad}, self.alpha)
        self.assertIn("No stored conversation", mcp.call(self.memory, "memory_search", {"conversation": 9999}, self.alpha))
        self.assertIn("needs", mcp.call(self.memory, "memory_search", {}, self.alpha))


    def seed(self, lane, agent_type, prompt, answer):
        self.memory.index(lane, SESSION, self.key, [human(prompt)], reply(answer), parent=self.parent, agent_type=agent_type)
        return self.store.run("select n.id from node n join convo c on c.id = n.convo where c.lane = ?", (lane,))[0]["id"]

    def test_a_new_subagent_is_told_what_earlier_subagents_learned_and_the_note_stays_byte_stable(self):
        earlier = self.seed("old-sub", "Explore", "Where does the retry helper live in the code", "The retry helper lives in net.py.")
        first = self.sub(self.body())
        text = self.note_of(first)[0]
        self.assertIn(f"You are a subagent of conversation {self.parent}", text)
        self.assertIn("Almanac memory for this project", text)
        self.assertIn(f"[{earlier}]", text)
        own = self.store.run("select id from convo where lane = 'old-sub'")[0]["id"]
        self.assertIn(f"(unreviewed: memory_review conversation {own})", text)  # the number a reviewer must pass
        self.assertEqual(len(self.note_of(first)), 1)
        self.assertEqual(self.rig.records()[-1]["memory"]["subnote_chars"], len(text))
        self.seed("old-sub-2", "Explore", "The retry helper code also lives in the report module", "Found the retry helper there too.")
        self.sub(self.body())
        self.assertEqual(self.rig.sent()["messages"][0], first["messages"][0])  # frozen: later memory does not change it

    def test_recall_ranks_the_same_agent_type_first_without_dropping_the_rest(self):
        other = self.seed("sub-a", "general-purpose", "Find the retry helper and the retry delay in the code", "The retry helper and retry delay are in net.py.")
        mine = self.seed("sub-b", "Explore", "Find the retry helper", "It is in net.py.")
        def order(kind):
            note = self.memory.recall(self.key, "Find the retry helper and the retry delay", "new-lane", 4000, agent_type=kind)
            return [i for _, i in sorted((note.find(f"[{i}]"), i) for i in (other, mine) if f"[{i}]" in note)]

        self.assertEqual(order("Explore")[0], mine)
        self.assertEqual(order("general-purpose")[0], other)
        self.assertEqual(sorted(order("Explore")), sorted([other, mine]))  # a boost, not a filter
        main = self.memory.recall(self.key, "Find the retry helper and the retry delay", "new-lane", 4000)
        self.assertNotIn(f"[{other}]", main)  # the main thread's note still leaves sub-agent work out

    def test_a_stored_subagent_is_pending_with_its_type_and_only_an_opus_review_of_it_counts(self):
        self.sub(self.body())
        self.rig.records()
        child = dict(self.store.run("select * from convo where parent = ?", (self.parent,))[0])
        self.assertEqual((child["agent_type"], child["review"], child["review_note"]), ("general-purpose", "pending", ""))
        self.assertEqual(self.store.run("select review from convo where id = ?", (self.parent,))[0]["review"], "")
        hit = self.memory.search("retry helper net.py", project=self.key)[0]
        self.assertIn(f"(unreviewed: memory_review conversation {child['id']})", mcp.render_hits([hit]))
        self.assertIn(f"(unreviewed: memory_review conversation {child['id']})",
                      mcp.render_hits(self.memory.within(self.parent)))  # the conversation listing shows it too

        def review(model, number, verdict="holds up"):
            self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse(
                "ok", tool=("mcp__almanac__memory_review", {"conversation": number, "verdict": verdict})))
            self.rig.messages(dict(self.body("Review the stored sub-agent work"), model=model))
            self.rig.records()
            return dict(self.store.run("select review, review_note from convo where id = ?", (number,))[0])

        self.assertEqual(review("claude-sonnet-5", child["id"])["review"], "pending")  # a non-Opus caller changes nothing
        self.assertEqual(review("claude-opus-5", self.parent)["review"], "")  # a main-thread conversation is never reviewed
        self.assertEqual(review("claude-opus-5", child["id"]), {"review": "reviewed", "review_note": "holds up"})
        self.assertEqual(review("claude-opus-5", child["id"], "changed my mind")["review_note"], "holds up")  # only pending changes
        hit = self.memory.search("retry helper net.py", project=self.key)[0]
        self.assertNotIn("unreviewed", mcp.render_hits([hit]))
        self.rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse(
            "ok", tool=("mcp__almanac__memory_review", {"verdict": "all checked"})))
        ask = dict(self.body("Review all the stored sub-agent work"), model="claude-opus-5")
        self.rig.messages(ask)
        self.rig.records()
        here = self.store.run("select id from convo where lane = ?", (self.rig.records()[-1]["lane"],))[0]["id"]
        self.store.run("update convo set review = 'pending', parent = ? where id = ?", (here, child["id"]))  # a sub-agent of the reviewing conversation
        self.rig.messages(ask)
        self.rig.records()
        self.assertEqual(self.store.run("select review from convo where id = ?", (child["id"],))[0]["review"], "reviewed")  # no number: all of them
        self.assertIn("sub-agent of conversation", mcp.render_hits([hit]))

    def test_the_gate_holds_an_opus_turn_while_subagent_work_is_unreviewed(self):
        self.sub(self.body())
        self.rig.records()
        child = self.store.run("select id from convo where parent = ?", (self.parent,))[0]["id"]
        self.store.put(f"mainmodel:{SESSION}", "claude-sonnet-5")
        self.assertEqual(self.memory.gate(SESSION), "")  # a model whose review would not count is never held
        self.store.put(f"mainmodel:{SESSION}", "claude-opus-5")
        self.assertIn(f"{child}:", self.memory.gate(SESSION))
        self.assertIn("memory_review", self.memory.gate(SESSION))
        self.assertEqual(self.memory.gate(SESSION), "")  # held twice at most: never stuck
        self.store.run("delete from state where key = 'gateheld:main-lane'")
        self.store.run("update convo set review = 'reviewed' where id = ?", (child,))
        self.assertEqual(self.memory.gate(SESSION), "")
        self.assertEqual(self.memory.gate("unknown-session"), "")

    def test_the_review_tool_only_acknowledges(self):
        self.sub(self.body())
        self.rig.records()
        child = self.store.run("select id from convo where parent = ?", (self.parent,))[0]["id"]
        self.assertIn("Review noted", mcp.call(self.memory, "memory_review", {"conversation": child, "verdict": "ok"}, self.alpha))
        self.assertEqual(self.store.run("select review from convo where id = ?", (child,))[0]["review"], "pending")

    def test_a_database_from_before_reviews_gains_the_columns_and_marks_old_subagent_work_pending(self):
        path = os.path.join(self.tmp, "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            create table convo (id integer primary key, lane text unique not null, session text not null, project text not null,
                title text not null default '', started real not null, updated real not null, turns integer not null default 0,
                parent integer not null default 0);
            insert into convo (id, lane, session, project, started, updated, parent) values (1, 'main', 's', 'p', 1, 1, 0), (2, 'sub', 's', 'p', 1, 1, 1);
        """)
        old.commit()
        old.close()
        memory = Memory(Store(path))
        self.addCleanup(memory.store.close)
        rows = {r["id"]: (r["agent_type"], r["review"], r["review_note"]) for r in memory.store.run("select * from convo")}
        self.assertEqual(rows, {1: ("", "", ""), 2: ("", "pending", "")})


class McpTest(Case):
    def ask(self, method, params=None, ident=1):
        return mcp.handle(self.memory, {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}, self.alpha)

    def test_handshake_and_tool_list(self):
        self.assertEqual(self.ask("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in self.ask("tools/list")["result"]["tools"]],
                         ["memory_search", "memory_read", "memory_projects", "playbook_find", "playbook_save", "playbook_run",
                          "memory_list", "memory_handover", "memory_user_messages", "library_find", "memory_review"])
        self.assertIsNone(mcp.handle(self.memory, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.alpha))
        self.assertEqual(self.ask("nope")["error"]["code"], -32601)

    def test_the_tool_list_follows_the_memory_and_playbook_switches(self):
        def names(**sections):
            cfg = config.load()
            for section, over in sections.items():
                cfg[section].update(over)
            with mock.patch.object(config, "load", return_value=cfg):
                return [t["name"] for t in self.ask("tools/list")["result"]["tools"]]
        self.assertEqual([n for n in names(memory={"tools": False}) if not n.startswith("playbook_")], [])
        self.assertEqual(len(names(memory={"tools": False})), 3)
        self.assertEqual([n for n in names(playbooks={"enabled": False}) if n.startswith("playbook_")], [])
        self.assertIn("library_find", names(playbooks={"enabled": False}))
        self.assertNotIn("library_find", names(memory={"tools": False}))

    def test_initialize_carries_constant_instructions(self):
        first, second = (self.ask("initialize", {"protocolVersion": "2025-06-18"}, ident=n)["result"] for n in (1, 2))
        text = first["instructions"]
        self.assertEqual(text, second["instructions"])
        self.assertIn("memory_read", text)
        self.assertLessEqual(text.count(". "), 5)  # five or six plain sentences

    def test_always_load_is_an_option_and_off_by_default(self):
        entry = mcp.client_config()["mcpServers"]["almanac"]
        self.assertNotIn("alwaysLoad", entry)
        cfg = json.loads(json.dumps(config.DEFAULTS))
        self.assertFalse(cfg["memory"]["always_load"])
        cfg["memory"]["always_load"] = True
        self.assertIs(mcp.client_config(cfg)["mcpServers"]["almanac"]["alwaysLoad"], True)

    def test_search_then_read(self):
        key = self.memory.projects.see(self.alpha)
        self.memory.index("lane-1", "s1", key, [human("How is the retry backoff computed?")], reply("Exponential with jitter."))
        found = self.ask("tools/call", {"name": "memory_search", "arguments": {"query": "retry backoff"}})["result"]
        text = found["content"][0]["text"]
        self.assertFalse(found["isError"])
        self.assertIn("How is the retry backoff computed?", text)
        node_id = int(text[1:text.index("]")])
        read = self.ask("tools/call", {"name": "memory_read", "arguments": {"id": node_id}})["result"]["content"][0]["text"]
        self.assertIn("Exponential with jitter.", read)
        listing = self.ask("tools/call", {"name": "memory_projects", "arguments": {}})["result"]["content"][0]["text"]
        self.assertIn("alpha", listing)
        missing = self.ask("tools/call", {"name": "memory_search", "arguments": {"query": "x y", "project": "nope"}})
        self.assertIn("No project is called", missing["result"]["content"][0]["text"])

    def test_stdio_loop(self):
        out = io.BytesIO()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n", b"not json\n"]
        mcp.serve(stdin=iter(lines), stdout=out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32700)


class StatusTest(Case):
    """The status rules (status.py), as they run when exchanges are stored."""

    def tag(self, *exchanges, final, last="Done."):
        """Store a conversation of (typed text, assistant messages) exchanges ending on `final`; return seq -> status."""
        messages = [m for typed, answer in exchanges for m in [human(typed), *answer]] + [human(final)]
        self.memory.index("lane", "s", self.memory.projects.see(self.alpha), messages, reply(last))
        return self.memory.statuses("lane")

    def test_a_question_sets_no_rule(self):
        done = [said("It reads a stale cache key.")]
        found = self.tag(("Why does the upload always fail on the second try?", done), final="Fix it then")
        self.assertEqual(found[0], "done")

    def test_open_work_far_behind_the_newest_is_no_longer_open(self):
        asked = [said("Which of the two layouts do you want?")]
        found = self.tag(*[("Sketch the settings page layout", asked)] * 18, final="Carry on with the form")
        self.assertEqual(found[0], "done")  # more than OPEN_KEEP exchanges back
        self.assertEqual(found[17], "open")

    def test_each_rule(self):
        done = [said("Added the retry helper.")]
        found = self.tag(
            ("Add a retry helper to fetch", done),
            ("From now on keep every function under twenty lines", done),
            ("ok go ahead", done),
            ("<command-name>/clear</command-name>", done),
            ("Rename the module", [said("Which name do you want?")]),
            final="Add a cache to the proxy")
        self.assertEqual([found[n] for n in range(6)], ["done", "decision", "noise", "noise", "open", "open"])

    def test_precedence(self):
        tool = [said("Looking", ("Read", {"file_path": self.alpha + "/src/a.py"})), result(), said("Read it.")]
        found = self.tag(
            ("always use tabs", [said("Done.")]),  # decision, then the next message says it failed: still a decision
            ("that's wrong, try again", [said("Retried.")]),
            ("sounds good", [said("Fine.")]),  # an acknowledgement, then a failure: superseded beats noise
            ("it still fails", [said("Looking again.")]),
            ("go ahead", tool),  # an acknowledgement that did work is not noise
            final="ok", last="Right.")  # the newest, and an acknowledgement: noise beats open
        self.assertEqual([found[n] for n in range(6)], ["decision", "done", "superseded", "done", "done", "noise"])

    def test_the_next_message_saying_it_failed_makes_the_exchange_before_it_superseded(self):
        key = self.memory.projects.see(self.alpha)
        first = [human("Add a retry helper to fetch"), said("Added it.")]
        self.memory.index("lane", "s", key, first[:1], reply("Added it."))
        self.assertEqual(self.memory.statuses("lane"), {0: "open"})
        self.memory.index("lane", "s", key, first + [human("Now add a cache")], reply("Added the cache."))
        self.assertEqual(self.memory.statuses("lane"), {0: "done", 1: "open"})
        later = first + [human("Now add a cache"), said("Added the cache."), human("that did not work, try again")]
        self.memory.index("lane", "s", key, later, reply("Trying again."))
        self.assertEqual(self.memory.statuses("lane"), {0: "done", 1: "superseded", 2: "open"})
        self.assertEqual(self.memory.status_counts(), {"open": 1, "done": 1, "decision": 0, "superseded": 1, "noise": 0})


class CliTest(unittest.TestCase):
    def test_memory_and_projects_commands_run(self):
        from getalmanac import cli

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["memory", "search", "anything", "at", "all", "--all"]), 0)
            self.assertEqual(cli.main(["memory", "recent"]), 0)
            self.assertIn("exchanges by status: open 0, done 0", out.getvalue())
            self.assertEqual(cli.main(["projects"]), 0)
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("memory", out.getvalue())


if __name__ == "__main__":
    unittest.main()
