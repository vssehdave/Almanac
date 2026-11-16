import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from helpers import Rig, sse

from getalmanac import embed, mcp, project
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


class McpTest(Case):
    def ask(self, method, params=None, ident=1):
        return mcp.handle(self.memory, {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}, self.alpha)

    def test_handshake_and_tool_list(self):
        self.assertEqual(self.ask("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in self.ask("tools/list")["result"]["tools"]],
                         ["memory_search", "memory_read", "memory_projects"])
        self.assertIsNone(mcp.handle(self.memory, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.alpha))
        self.assertEqual(self.ask("nope")["error"]["code"], -32601)

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


class CliTest(unittest.TestCase):
    def test_memory_and_projects_commands_run(self):
        from getalmanac import cli

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["memory", "search", "anything", "at", "all", "--all"]), 0)
            self.assertEqual(cli.main(["memory", "recent"]), 0)
            self.assertEqual(cli.main(["projects"]), 0)
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("memory", out.getvalue())


if __name__ == "__main__":
    unittest.main()
