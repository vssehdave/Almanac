import contextlib
import io
import json
import os
import subprocess
import unittest
from datetime import datetime
from unittest import mock

import test_summarize as ts
from test_memory import Case, human, reply

from getalmanac import cli, codemem, mcp, project
from getalmanac.memory import Memory
from getalmanac.store import State, Store


def use(call, name, **data):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": call, "name": name, "input": data}]}


def out(call, text="ok", error=False):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": text, **({"is_error": True} if error else {})}]}


class CodeMemoryTest(Case):
    def setUp(self):
        super().setUp()
        self.file = self.alpha + "/src/app.py"
        self.messages = [human("Fix the app"),
                         use("e1", "Edit", file_path=self.file, old_string="a = 1", new_string="a = 2\nb = 3"), out("e1"),
                         use("w1", "Write", file_path=self.alpha + "/src/new.py", content="x = 1\n"), out("w1"),
                         use("e2", "Edit", file_path=self.file, old_string="zzz", new_string="y", ), out("e2", "String not found", True)]

    def index(self, messages=None, lane="lane-1"):
        self.memory.index(lane, "s", self.memory.projects.see(self.alpha), messages or self.messages, reply("Done."))

    def rows(self, path=None):
        return codemem.history(self.store, path or self.file)

    def test_edit_and_write_are_recorded_and_the_failed_edit_is_not(self):
        self.index()
        (edit,) = self.rows()
        self.assertEqual((edit["kind"], edit["status"], edit["tool_use_id"]), ("edit", "ok", "e1"))
        self.assertIn("-1/+2 lines", codemem.render(self.rows()))
        (write,) = self.rows(self.alpha + "/src/new.py")
        self.assertEqual((write["kind"], write["content"], write["size"]), ("write", "x = 1", 6))
        node = self.store.run("select id, convo from node where kind = 'leaf'")[0]
        self.assertEqual((edit["node"], edit["convo"]), (node["id"], node["convo"]))
        self.assertTrue(edit["ts"].endswith("+00:00"))

    def test_recording_again_writes_nothing_new(self):
        self.index()
        self.index(self.messages + [use("e3", "Edit", file_path=self.file, old_string="b", new_string="c"), out("e3")] )
        self.index()
        self.assertEqual([r["tool_use_id"] for r in self.rows()], ["e3", "e1"])
        self.assertEqual(len(self.store.run("select 1 from codeedit")), 3)

    def test_a_large_write_keeps_only_its_hash(self):
        big = "x" * (codemem.KEEP + 1)
        self.index([human("Make it"), use("w", "Write", file_path=self.file, content=big), out("w")])
        (row,) = self.rows()
        self.assertIsNone(row["content"])
        self.assertEqual((len(row["chash"]), row["size"]), (40, len(big)))
        self.assertIn("content not kept", codemem.render(self.rows()))

    def test_a_call_without_a_result_is_unknown_and_an_error_result_removes_it(self):
        codemem.capture(self.store, 1, "p", 1, [use("u", "Edit", file_path=self.file, old_string="a", new_string="b")], 0)
        self.assertEqual(self.rows()[0]["status"], "unknown")
        self.assertIn("result not seen", codemem.render(self.rows()))
        codemem.capture(self.store, 1, "p", 1, [use("u", "Edit", file_path=self.file, old_string="a", new_string="b"), out("u", "no", True)], 0)
        self.assertEqual(self.rows(), [])

    def test_multiedit_and_notebook_and_odd_input(self):
        self.index([human("Go"), use("m", "MultiEdit", file_path=self.file, edits=[
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]), out("m"),
            use("n", "NotebookEdit", notebook_path=self.alpha + "/n.ipynb", new_source="print(1)"), out("n"),
            use("bad", "Edit", file_path=self.file, old_string=None), out("bad")])
        self.assertIn("2 edit(s)", codemem.render(self.rows()))
        self.assertEqual(self.rows(self.alpha + "/n.ipynb")[0]["kind"], "edit")
        self.assertEqual(len(self.rows()), 1)

    def test_paths_are_spelled_one_way(self):
        self.index([human("Go"), use("a", "Edit", file_path=self.file.replace("/", "\\"), old_string="a", new_string="b"), out("a")])
        self.assertEqual(self.rows()[0]["path"], project.normalize(self.file))
        self.assertEqual(len(self.rows(self.file.replace("/", "\\"))), 1)

    def test_mcp_tool_and_cli_print_the_same_history(self):
        self.index()
        text = mcp.call(self.memory, "code_history", {"path": "src/app.py", "limit": 5}, self.alpha)
        self.assertIn(f"exchange {self.rows()[0]['node']}", text)
        self.assertEqual(text, codemem.render(self.rows(), self.file))
        self.assertIn("No recorded changes", mcp.call(self.memory, "code_history", {"path": "nope.py"}, self.alpha))
        self.assertIn("needs a path", mcp.call(self.memory, "code_history", {}, self.alpha))
        self.assertIn("code_history", [t["name"] for t in mcp.TOOLS])

    def test_recall_names_the_last_edit_of_a_file_the_prompt_mentions(self):
        self.index()
        key = self.memory.projects.see(self.alpha)
        node = self.rows()[0]["node"]
        note = self.memory.recall(key, "why did src/app.py change?", "elsewhere", 2400)
        self.assertIn(f"{self.file}: last edit ", note)
        self.assertIn(f"exchange {node}", note)
        self.assertEqual(self.memory.recall(key, "why did src/app.py change?", "elsewhere", 2400), note)
        self.assertNotIn("last edit", self.memory.recall(key, "why did src/other.py change?", "elsewhere", 2400))
        self.assertNotIn("last edit", self.memory.recall(key, "explain the app", "elsewhere", 2400))

    def test_start_sync_follows_the_setting(self):
        with mock.patch.object(codemem, "sync") as sync:
            self.assertIsNone(codemem.start_sync({"code": {"sync_on_start": False}}, self.store))
            sync.assert_not_called()
            codemem.start_sync({"code": {"sync_on_start": True}}, self.store).join()
            self.assertTrue(sync.called)

    def test_related_tool_and_cli_print_the_same_text(self):
        self.index()
        text = mcp.call(self.memory, "code_related", {"path": "src/app.py"}, self.alpha)
        self.assertEqual(text, codemem.related(self.store, self.file))
        self.assertIn("Last edit: 20", text)
        self.assertIn("needs a path", mcp.call(self.memory, "code_related", {}, self.alpha))
        self.store.close()
        old = os.environ.get("ALMANAC_HOME")
        os.environ["ALMANAC_HOME"] = self.tmp
        self.addCleanup(lambda: os.environ.pop("ALMANAC_HOME") if old is None else os.environ.update(ALMANAC_HOME=old))
        os.replace(os.path.join(self.tmp, "a.db"), os.path.join(self.tmp, "almanac.db"))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli.main(["code", "related", self.file]), 0)
        self.assertEqual(buffer.getvalue().strip(), text.strip())

    def test_edits_are_recorded_when_the_proxy_stores_a_finished_exchange(self):
        from helpers import Rig, sse
        from getalmanac.memory import MemoryLayer, ProjectLayer
        rig = Rig(layers=[ProjectLayer(self.memory), MemoryLayer(self.memory)], store=self.store, state=self.state)
        self.addCleanup(rig.close)
        rig.upstream.respond = lambda req: (200, {"content-type": "text/event-stream"}, sse("Done."))
        env = {"role": "system", "content": [{"type": "text", "text": f"# Environment\n - Primary working directory: {self.alpha}\n"}]}
        tools = [{"name": "Edit", "description": "x", "input_schema": {"type": "object"}}]
        history = self.messages[:3] + [{"role": "assistant", "content": [{"type": "text", "text": "Fixed."}]}]
        rig.messages({"model": "m", "tools": tools, "messages": [*history, human("Thanks, now run it"), env]})
        rig.records()
        self.assertEqual([r["tool_use_id"] for r in self.rows()], ["e1"])

    def test_cli(self):
        self.index()
        self.store.close()
        old = os.environ.get("ALMANAC_HOME")
        os.environ["ALMANAC_HOME"] = self.tmp
        self.addCleanup(lambda: os.environ.pop("ALMANAC_HOME") if old is None else os.environ.update(ALMANAC_HOME=old))
        os.replace(os.path.join(self.tmp, "a.db"), os.path.join(self.tmp, "almanac.db"))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli.main(["code", "history", self.file, "-n", "1"]), 0)
        self.assertIn("exchange", buffer.getvalue())
        self.assertIn("1 edit(s)", buffer.getvalue())

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


class CodeSyncTest(Case):
    def setUp(self):
        super().setUp()
        self.repo = project.normalize(os.path.realpath(os.path.join(self.tmp, "real")))
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.key = "dir:" + self.repo.lower()

    def git(self, *args, when=None):
        env = {**os.environ, **GIT_ENV, **({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else {})}
        subprocess.run(["git", "-C", self.repo, *args], env=env, check=True, capture_output=True)

    def commit(self, name, text, when, message="change"):
        with open(os.path.join(self.repo, name), "w") as handle:
            handle.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, when=when)

    def record(self, call, name, old, new, at, tool="Edit", convo=1):
        group = [use(call, tool, file_path=f"{self.repo}/{name}", **({"old_string": old, "new_string": new} if tool == "Edit" else {"content": new})),
                 out(call)]
        codemem.capture(self.store, convo, self.key, 1, group, datetime.fromisoformat(at).timestamp())

    def sync(self):
        return codemem.sync(self.store, self.key, self.repo)

    def rows(self, name):
        return codemem.history(self.store, f"{self.repo}/{name}")

    def test_edit_links_to_the_first_commit_after_it_and_an_unmatched_commit_is_outside(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00", "start")
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")
        self.commit("a.py", "a = 2\n", "2026-01-01T11:00:00+00:00", "use two")
        self.commit("a.py", "a = 3\n", "2026-01-01T12:00:00+00:00", "by hand")
        self.commit("b.py", "b\n", "2026-01-01T13:00:00+00:00", "new file")
        self.sync()
        by_kind = {r["kind"]: r for r in self.rows("a.py")}
        self.assertEqual(by_kind["edit"]["commit_id"], self.store.run("select commit_id from codeedit where tool_use_id = 'e1'")[0][0])
        self.assertEqual(by_kind["edit"]["branch"], "main")
        subjects = sorted(r["content"] for r in self.store.run("select content from codeedit where kind = 'outside'"))
        self.assertEqual(subjects, ["by hand", "new file"])
        self.assertIn("commit " + by_kind["edit"]["commit_id"][:7] + " on main", codemem.render(self.rows("a.py")))

    def test_edit_stamped_minutes_after_its_own_commit_still_links(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.commit("a.py", "a = 2\n", "2026-01-01T10:00:00+00:00", "same turn")
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:03:00+00:00")
        self.sync()
        self.assertIsNotNone(self.store.run("select commit_id from codeedit where tool_use_id = 'e1'")[0][0])
        self.assertEqual(self.store.run("select count(*) from codeedit where kind = 'outside'")[0][0], 0)

    def test_edit_in_a_worktree_keeps_its_branch_while_main_is_checked_out(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        work = project.normalize(os.path.realpath(os.path.join(self.tmp, "wt")))
        self.git("worktree", "add", "-q", "-b", "feat/x", work)
        self.assertEqual(codemem._head_branch(work + "/a.py"), "feat/x")
        group = [use("w1", "Edit", file_path=work + "/a.py", old_string="a = 1", new_string="a = 2"), out("w1")]
        codemem.capture(self.store, 1, self.key, 1, group, datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        with open(work + "/a.py", "w") as handle:
            handle.write("a = 2\n")
        subprocess.run(["git", "-C", work, "commit", "-q", "-am", "wt"], check=True, capture_output=True,
                       env={**os.environ, **GIT_ENV, "GIT_AUTHOR_DATE": "2026-01-01T10:01:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T10:01:00+00:00"})
        self.sync()
        row = self.store.run("select commit_id, branch from codeedit where tool_use_id = 'w1'")[0]
        self.assertIsNotNone(row["commit_id"])
        self.assertEqual(row["branch"], "feat/x")

    def commit_in(self, path, text, when):
        with open(os.path.join(path, "a.py"), "w") as handle:
            handle.write(text)
        subprocess.run(["git", "-C", path, "commit", "-q", "-am", "wt"], check=True, capture_output=True,
                       env={**os.environ, **GIT_ENV, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})

    def test_edit_in_a_removed_worktree_still_links_to_its_commit(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        work = project.normalize(os.path.realpath(os.path.join(self.tmp, "wt")))
        self.git("worktree", "add", "-q", "-b", "feat/x", work)
        codemem.capture(self.store, 1, self.key, 1, [use("w1", "Edit", file_path=work + "/a.py", old_string="a = 1", new_string="a = 2"), out("w1")],
                        datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        self.commit_in(work, "a = 2\n", "2026-01-01T10:01:00+00:00")
        self.git("worktree", "remove", "--force", work)
        codemem.capture(self.store, 1, self.key, 1, [use("w1", "Edit", file_path=work + "/a.py", old_string="a = 1", new_string="a = 2"), out("w1")],
                        datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        self.assertEqual(self.store.run("select rel from codeedit where tool_use_id = 'w1'")[0][0], "a.py")
        self.sync()
        self.assertIsNotNone(self.store.run("select commit_id from codeedit where tool_use_id = 'w1'")[0][0])

    @unittest.skipUnless(os.name == "nt", "case-insensitive file system")
    def test_history_ignores_path_case(self):
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")
        rows = codemem.history(self.store, self.repo[:3] + self.repo[3:].swapcase() + "/a.py")
        self.assertEqual([r["tool_use_id"] for r in rows], ["e1"])

    def test_history_from_main_checkout_shows_worktree_edits(self):
        self.assertEqual(project.describe(self.repo)[0], self.key)
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        work = project.normalize(os.path.realpath(os.path.join(self.tmp, "wt")))
        self.git("worktree", "add", "-q", "-b", "feat/x", work)
        codemem.capture(self.store, 1, self.key, 1, [use("w1", "Edit", file_path=work + "/a.py", old_string="a = 1", new_string="a = 2"), out("w1")],
                        datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        self.assertIn("w1", [r["tool_use_id"] for r in self.rows("a.py")])

    def test_old_row_without_rel_under_a_removed_worktree_links(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        wts = project.normalize(os.path.realpath(os.path.join(self.tmp, "wts")))
        self.git("worktree", "add", "-q", "-b", "live", wts + "/live")
        gone = wts + "/gone"
        self.git("worktree", "add", "-q", "-b", "gone", gone)
        codemem.capture(self.store, 1, self.key, 1, [use("g1", "Edit", file_path=gone + "/a.py", old_string="a = 1", new_string="a = 2"), out("g1")],
                        datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        self.commit_in(gone, "a = 2\n", "2026-01-01T10:01:00+00:00")
        self.git("worktree", "remove", "--force", gone)
        self.store.run("update codeedit set rel = null")
        self.sync()
        self.assertIsNotNone(self.store.run("select commit_id from codeedit where tool_use_id = 'g1'")[0][0])

    def test_old_row_without_rel_under_the_live_root_links(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")
        self.commit("a.py", "a = 2\n", "2026-01-01T11:00:00+00:00", "use two")
        self.store.run("update codeedit set rel = null")
        self.sync()
        row = self.store.run("select rel, commit_id from codeedit where tool_use_id = 'e1'")[0]
        self.assertEqual(row["rel"], "a.py")
        self.assertIsNotNone(row["commit_id"])

    def test_old_row_beside_a_worktree_but_not_one_does_not_link(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.git("worktree", "add", "-q", "-b", "feat/x", project.normalize(os.path.realpath(os.path.join(self.tmp, "wt"))))
        other = project.normalize(os.path.realpath(os.path.join(self.tmp, "other")))
        os.makedirs(other)
        with open(os.path.join(other, "a.py"), "w") as handle:
            handle.write("a = 2\n")
        codemem.capture(self.store, 1, self.key, 1, [use("o1", "Edit", file_path=other + "/a.py", old_string="a = 1", new_string="a = 2"), out("o1")],
                        datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        self.commit("a.py", "a = 2\n", "2026-01-01T11:00:00+00:00", "use two")
        self.store.run("update codeedit set rel = null")
        self.sync()
        row = self.store.run("select rel, commit_id from codeedit where tool_use_id = 'o1'")[0]
        self.assertIsNone(row["commit_id"])
        self.assertEqual(row["rel"], "")

    def test_edit_outside_any_repo_has_no_rel(self):
        loose = project.normalize(os.path.realpath(os.path.join(self.tmp, "loose")))
        os.makedirs(loose)
        codemem.capture(self.store, 1, self.key, 1, [use("n1", "Edit", file_path=loose + "/notes.md", old_string="a", new_string="b"), out("n1")],
                        datetime.fromisoformat("2026-01-01T10:00:00+00:00").timestamp())
        self.assertIsNone(self.store.run("select rel from codeedit where tool_use_id = 'n1'")[0][0])
        self.sync()
        self.assertIsNone(self.store.run("select commit_id from codeedit where tool_use_id = 'n1'")[0][0])

    def test_outside_commit_on_a_merged_branch_gets_the_branch_from_the_merge(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.record("e1", "a.py", "a = 1", "a = 1", "2026-01-01T09:30:00+00:00")
        self.git("checkout", "-q", "-b", "feat/y")
        self.commit("b.py", "b\n", "2026-01-01T10:00:00+00:00", "by hand")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-q", "-m", "Merge pull request #5 from me/feat/y", "feat/y", when="2026-01-01T11:00:00+00:00")
        self.git("branch", "-q", "-D", "feat/y")
        self.sync()
        rows = self.store.run("select branch, content from codeedit where kind = 'outside' and content = 'by hand'")
        self.assertEqual([r["branch"] for r in rows], ["feat/y"])

    def test_uncommitted_change_is_recorded_once_and_a_rerun_adds_nothing(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")
        self.commit("a.py", "a = 2\n", "2026-01-01T11:00:00+00:00")
        with open(os.path.join(self.repo, "a.py"), "w") as handle:
            handle.write("a = 9\n")
        self.sync()
        self.sync()
        counts = [(r["kind"], r["status"]) for r in self.store.run("select kind, status from codeedit order by rowid")]
        self.assertEqual(counts, [("edit", "ok"), ("outside", "uncommitted")])
        with open(os.path.join(self.repo, "a.py"), "w") as handle:
            handle.write("a = 10\n")
        self.sync()
        self.assertEqual(len(self.store.run("select 1 from codeedit where status = 'uncommitted'")), 2)

    def test_repeats_collapse_and_an_exact_inverse_marks_the_first_reverted(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")
        self.record("e2", "a.py", "a = 1", "a = 2", "2026-01-01T10:01:00+00:00")
        self.record("e3", "a.py", "a = 2", "a = 1", "2026-01-01T10:02:00+00:00")
        self.sync()
        status = {r["tool_use_id"]: r["status"] for r in self.store.run("select tool_use_id, status from codeedit")}
        self.assertEqual(status, {"e1": "reverted", "e2": "duplicate", "e3": "ok"})
        self.assertEqual([r["tool_use_id"] for r in self.rows("a.py")], ["e3", "e1"])
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")  # the history is sent again
        self.assertEqual(self.store.run("select status from codeedit where tool_use_id = 'e1'")[0][0], "reverted")

    def test_not_a_git_repo_or_no_git_changes_nothing(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertIn("not a git repository", codemem.sync(self.store, "dir:x", plain))
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIn("not a git repository", self.sync())
        self.assertEqual(self.store.run("select 1 from codeedit"), [])
        self.assertEqual(self.store.run("select 1 from codesync"), [])

    def test_setup_adds_the_columns_to_an_older_table(self):
        self.store.run("drop table codeedit")
        self.store.run("create table codeedit (tool_use_id text primary key, ts text not null, project text not null, convo integer not null, "
                       "node integer not null, path text not null, kind text not null, tool text not null, status text not null, "
                       "edits text not null default '[]', content text, chash text not null, size integer not null default 0)")
        codemem.setup(self.store)
        codemem.setup(self.store)
        self.assertEqual(self.store.run("select commit_id, branch, rel from codeedit"), [])


class CodeRelatedTest(CodeSyncTest):
    def put(self, name, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def links(self, name):
        return sorted(r["dst"][len(self.repo) + 1:] for r in self.store.run("select dst from coderel where src = ?", (f"{self.repo}/{name}",)))

    def test_python_relative_and_package_imports_and_js_relative_imports_are_linked(self):
        self.put("pkg/__init__.py", "")
        self.put("pkg/a.py", '"""Does the a thing.\n\nMore."""\nfrom . import b\nfrom .c import x\nimport pkg.d\nimport os\nfrom pkg import e\n')
        for name in "bcde":
            self.put(f"pkg/{name}.py", "x = 1\n")
        self.put("web/main.ts", "import { f } from './lib'\nconst g = require('./util.js')\nimport 'left-pad'\nimport('../pkg/x')\n")
        self.put("web/lib.ts", "")
        self.put("web/util.js", "")
        self.git("add", "-A")
        self.sync()
        self.assertEqual(self.links("pkg/a.py"), ["pkg/__init__.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py"])
        self.assertEqual(self.links("web/main.ts"), ["web/lib.ts", "web/util.js"])
        self.assertIn("Used by: " + f"{self.repo}/pkg/a.py", codemem.related(self.store, f"{self.repo}/pkg/b.py"))
        self.assertIn("Purpose: Does the a thing.", codemem.related(self.store, f"{self.repo}/pkg/a.py"))

    def test_a_reparsed_file_replaces_its_rows_and_an_import_outside_the_repo_is_ignored(self):
        self.put("a.py", "import b\nimport c\n")
        self.put("b.py", "")
        self.put("c.py", "")
        open(os.path.join(self.tmp, "outside.py"), "w").close()
        self.put("sub/m.py", "from ... import outside\nfrom .. import a\n")
        self.git("add", "-A")
        self.sync()
        self.assertEqual(self.links("a.py"), ["b.py", "c.py"])
        self.assertEqual(self.links("sub/m.py"), ["a.py"])
        self.put("a.py", "import c\n")
        os.utime(f"{self.repo}/a.py", (9e9, 9e9))
        self.git("add", "-A")
        self.sync()
        self.assertEqual(self.links("a.py"), ["c.py"])

    def test_a_file_that_does_not_parse_has_no_links(self):
        self.put("bad.py", "def (:\n")
        self.git("add", "-A")
        self.sync()
        self.assertEqual(self.links("bad.py"), [])



    def test_related_names_the_last_edit(self):
        self.commit("a.py", "a = 1\n", "2026-01-01T09:00:00+00:00")
        self.record("e1", "a.py", "a = 1", "a = 2", "2026-01-01T10:00:00+00:00")
        self.commit("a.py", "a = 2\n", "2026-01-01T11:00:00+00:00")
        self.sync()
        text = codemem.related(self.store, f"{self.repo}/a.py")
        self.assertIn("Purpose: unknown", text)
        self.assertIn("Last edit: 2026-01-01T10:00:00+00:00, branch main, commit ", text)


class PurposeTest(ts.Case):
    def setUp(self):
        super().setUp()
        self.file = self.alpha + "/retry.py"
        self.put("x = 1\n" * 10)

    def put(self, text):
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write(text)

    def edit(self, call, old, new, at):
        codemem.capture(self.store, 1, self.key, 1, [use(call, "Edit", file_path=self.file, old_string=old, new_string=new), out(call)],
                        datetime.fromisoformat(at).timestamp())

    def purpose_calls(self):
        return [c for c in self.calls if "one source file" in json.loads(c["body"])["system"][-1]["text"]]

    def test_a_new_file_gets_one_line_through_the_summary_call_and_a_small_edit_does_not_redo_it(self):
        self.edit("e1", "x = 1", "x = 2", "2026-01-01T10:00:00+00:00")
        self.verdict = ts.message("Retries a call.\nSecond line.")
        self.ask("Fix it")
        (call,) = self.purpose_calls()
        body = json.loads(call["body"])
        self.assertEqual((body["model"], body["messages"][0]["content"]), (ts.MODEL, "x = 1\n" * 10))
        self.assertEqual(self.store.run("select text from codepurpose")[0]["text"], "Retries a call. Second line.")
        self.assertIn("Purpose: Retries a call.", codemem.related(self.store, self.file))
        self.ask("Fix it more")
        self.assertEqual(len(self.purpose_calls()), 1)

    def test_git_tracked_files_without_a_purpose_are_included_after_the_edited_ones(self):
        self.edit("e1", "x = 1", "x = 2", "2026-01-01T10:00:00+00:00")
        other = self.alpha + "/other.py"
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("y = 1\n")
        subprocess.run(["git", "-C", self.alpha, "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.alpha, "add", "other.py"], check=True, capture_output=True)
        found = [p for p, _ in codemem.stale(self.store, self.key, 5)]
        self.assertEqual(found, [self.file, codemem.normalize(other)])
        codemem.set_purpose(self.store, self.key, codemem.normalize(other), "", "h")  # failed tries stop at MAX_TRIES
        for _ in range(codemem.MAX_TRIES):
            codemem.set_purpose(self.store, self.key, codemem.normalize(other), "", "h")
        self.assertEqual([p for p, _ in codemem.stale(self.store, self.key, 5)], [self.file])

    def test_a_large_change_since_the_purpose_asks_again(self):
        self.edit("e1", "x = 1", "x = 2", "2026-01-01T10:00:00+00:00")
        codemem.set_purpose(self.store, self.key, self.file, "Old.", "h")
        self.store.run("update codepurpose set ts = '2026-01-01T00:00:00+00:00'")
        self.assertEqual(codemem.stale(self.store, self.key, 5), [])  # 1 of 10 lines
        self.edit("e2", "x = 1\nx = 1\nx = 1\nx = 1", "y", "2026-01-02T10:00:00+00:00")  # 4 of 10 lines
        self.assertEqual([p for p, _ in codemem.stale(self.store, self.key, 5)], [self.file])

    def test_nothing_is_asked_when_summary_model_is_empty(self):
        self.start(**{"memory.summary_model": ""})
        self.edit("e1", "x = 1", "x = 2", "2026-01-01T10:00:00+00:00")
        self.ask("Fix it")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.run("select 1 from codepurpose"), [])

    def test_a_failed_call_is_tried_three_times_in_all(self):
        self.edit("e1", "x = 1", "x = 2", "2026-01-01T10:00:00+00:00")
        self.verdict = (500, ts.JSON_HEADERS, b"{}")
        for n in range(5):
            self.ask(f"Fix it {n}")
        self.assertLessEqual(len(self.purpose_calls()), codemem.MAX_TRIES)


if __name__ == "__main__":
    unittest.main()
