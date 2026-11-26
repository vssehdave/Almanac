import contextlib
import io
import os
import subprocess
import unittest
from datetime import datetime
from unittest import mock

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
        self.assertEqual(self.store.run("select commit_id, branch from codeedit"), [])


if __name__ == "__main__":
    unittest.main()
