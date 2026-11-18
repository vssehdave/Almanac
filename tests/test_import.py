import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import zlib
from unittest import mock
from datetime import datetime, timedelta, timezone

import helpers  # noqa: F401  (sets ALMANAC_HOME before getalmanac loads)
from test_memory import make_repo

from getalmanac import cli, importer, memory as memory_mod, project, request as rq
from getalmanac.memory import Memory
from getalmanac.store import Store

SID = "aaaaaaaa-0000-4000-8000-000000000001"


def stamp(days_ago, minutes=0):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class Transcript:
    """Builds a Claude Code session file the way the real ones look: one line per content block."""

    def __init__(self, folder, cwd, session=SID):
        self.lines, self.cwd, self.session, self.n = [], cwd, session, 0
        self.path = os.path.join(folder, session + ".jsonl")

    def _add(self, kind, role, content, ts, **extra):
        self.n += 1
        line = {"type": kind, "message": {"role": role, "content": content}, "timestamp": ts, "cwd": self.cwd,
                "sessionId": self.session, "uuid": f"u{self.n}", "isSidechain": False}
        line.update(extra)
        if role == "assistant":
            line["message"]["id"] = f"msg_{self.n}"
        self.lines.append(line)

    def turn(self, ts, typed, answer, tool=None, result="ok", **extra):
        """One exchange: what was typed, an optional tool call and its result, then the final answer."""
        self._add("user", "user", typed, ts, **extra)
        if tool:
            self._add("assistant", "assistant", [{"type": "tool_use", "id": f"t{self.n}", "name": tool[0], "input": tool[1]}], ts)
            self._add("user", "user", [{"type": "tool_result", "tool_use_id": f"t{self.n - 1}", "content": result}], ts)
        self._add("assistant", "assistant", [{"type": "text", "text": answer}], ts)
        return self

    def write(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "mode", "mode": "normal"}) + "\n")
            for line in self.lines:
                handle.write(json.dumps(line) + "\n")
        old = time.time() - 7200  # a finished session, not one still being written
        os.utime(self.path, (old, old))
        return self.path


class ImportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-import-")
        self.addCleanup(self._close)
        self.repos = os.path.join(self.tmp, "GitHub")
        os.makedirs(self.repos)
        self.a = make_repo(self.repos, "Alpha", "https://github.com/me/alpha.git")
        self.b = make_repo(self.repos, "Beta", "https://github.com/me/beta.git")
        self.folder = os.path.join(self.tmp, "projects", "alpha-folder")
        os.makedirs(self.folder)
        self.store = Store(os.path.join(self.tmp, "almanac.db"))
        self.memory = Memory(self.store)
        self.scratch = os.path.join(self.tmp, "scratch")  # the test repos sit in the temp folder, so it is not the ignored one here
        patch = mock.patch.object(importer, "scratch_dirs", lambda: [project.normalize(self.scratch)])
        patch.start()
        self.addCleanup(patch.stop)

    def _close(self):
        try:
            self.store.db.close()
        except Exception:
            pass

    def key(self, root):
        return self.memory.projects.see(root, touch=False)

    def session(self, **kw):
        return Transcript(self.folder, kw.pop("cwd", self.a), **kw)

    def run_import(self, *paths, **kw):
        return importer.run(self.memory, list(paths) or [self.folder], idle=0, **kw)

    def convos(self):
        return [dict(r) for r in self.store.run("select lane, project, started, updated from convo order by lane")]

    def leaves(self):
        return [dict(r) for r in self.store.run(
            "select c.lane, n.seq, n.project, n.status, n.ts from node n join convo c on c.id = n.convo "
            "where n.kind = 'leaf' order by c.lane, n.seq")]

    def everything_stored(self):
        parts = [r["text"] for r in self.store.run("select text from node")]
        parts += [r["text"] for r in self.store.run("select text from detail")]
        parts += [zlib.decompress(r["data"]).decode() for r in self.store.run("select data from raw")]
        return "\n".join(parts)


class ProjectPerExchange(ImportBase):
    def test_file_path_decides_the_project(self):
        t = self.session().turn(stamp(2), "look at it", "done", tool=("Read", {"file_path": self.b + "/src/x.py"}))
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_cd_in_a_bash_command(self):
        t = self.session().turn(stamp(2), "build it", "done", tool=("Bash", {"command": f"cd {self.b} && make"}))
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_relative_cd_uses_the_lines_cwd(self):
        t = self.session().turn(stamp(2), "build it", "done", tool=("Bash", {"command": "cd ../Beta && make"}))
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_cd_into_a_home_relative_folder(self):
        home, name = os.path.split(self.b)
        t = self.session().turn(stamp(2), "build it", "done", tool=("Bash", {"command": f"cd ~/{name} && make"}))
        with mock.patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}):
            self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_a_task_notification_is_not_a_typed_prompt(self):
        t = self.session().turn(stamp(2), "build it", "started")
        t.turn(stamp(2, 1), "<task-notification><task-id>bgtmc8yha</task-id></task-notification>", "it finished",
               origin={"kind": "task-notification"})
        self.run_import(t.write())
        self.assertEqual([r["title"] for r in self.store.run("select title from node where kind = 'leaf'")], ["build it"])

    def test_git_dash_c(self):
        t = self.session().turn(stamp(2), "status", "done", tool=("Bash", {"command": f"git -C {self.b} status"}))
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_worktree_counts_as_its_main_repo(self):
        tree = os.path.join(self.repos, "Beta-wt")
        os.makedirs(tree)
        with open(os.path.join(tree, ".git"), "w") as handle:
            handle.write(f"gitdir: {self.b}/.git/worktrees/wt\n")
        t = self.session().turn(stamp(2), "edit", "done", tool=("Edit", {"file_path": tree + "/f.py"}))
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_a_deleted_worktree_is_matched_by_its_name(self):
        gone = project.normalize(os.path.join(self.repos, "Beta-sec2", "src", "f.py"))  # never existed on disk
        t = self.session().turn(stamp(2), "edit", "done", tool=("Edit", {"file_path": gone}))
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})

    def test_no_signal_inherits_and_the_first_takes_the_first_real_project(self):
        t = (self.session()
             .turn(stamp(2, 0), "hello there", "hi")
             .turn(stamp(2, 1), "look at beta", "ok", tool=("Read", {"file_path": self.b + "/a.py"}))
             .turn(stamp(2, 2), "and then explain it", "sure"))
        self.run_import(t.write())
        self.assertEqual({(l["lane"], l["seq"]): l["project"] for l in self.leaves()},
                         {("import:%s:0" % SID, n): self.key(self.b) for n in range(3)})  # one run, all Beta

    def test_a_session_with_no_signal_at_all_takes_its_start_folder(self):
        t = self.session().turn(stamp(2, 0), "hello there", "hi").turn(stamp(2, 1), "and again", "hi")
        self.run_import(t.write())
        self.assertEqual({l["project"] for l in self.leaves()}, {self.key(self.a)})

    def test_a_scratch_folder_gets_no_vote_and_inherits(self):
        clone = make_repo(self.scratch, "clone", "https://github.com/me/clone.git")
        t = (self.session()
             .turn(stamp(2, 0), "fix beta", "ok", tool=("Edit", {"file_path": self.b + "/b.py"}))
             .turn(stamp(2, 1), "try it in a clone", "ok", tool=("Edit", {"file_path": clone + "/c.py"})))
        self.run_import(t.write())
        self.assertEqual([c["project"] for c in self.convos()], [self.key(self.b)])

    def _bash(self, command):
        return {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]}

    def test_a_relative_cd_follows_an_earlier_cd_in_the_same_command(self):
        elsewhere = project.normalize(os.path.join(self.tmp, "elsewhere"))
        self.assertEqual(importer._folders([self._bash(f"cd {elsewhere} && cd src && ls")], [self.a]), [])  # not Alpha/src
        self.assertEqual(importer._folders([self._bash(f"cd {self.b} && cd src")], [self.a]), [self.b, self.b + "/src"])

    def test_a_folder_that_does_not_exist_gets_no_vote_unless_it_is_a_deleted_worktree(self):
        self.assertEqual(importer._folders([self._bash("cd nope")], [self.a]), [])
        gone = project.normalize(os.path.join(self.repos, "Beta-sec2"))
        self.assertEqual(importer._folders([self._bash(f"cd {gone}")], [self.a]), [gone])

    def test_a_session_that_switches_project_is_two_conversations(self):
        t = (self.session()
             .turn(stamp(2, 0), "fix alpha", "ok", tool=("Edit", {"file_path": self.a + "/a.py"}))
             .turn(stamp(2, 1), "now beta", "ok", tool=("Edit", {"file_path": self.b + "/b.py"})))
        self.run_import(t.write())
        found = {c["lane"]: c["project"] for c in self.convos()}
        self.assertEqual(found, {f"import:{SID}:0": self.key(self.a), f"import:{SID}:1": self.key(self.b)})

    def test_the_session_cwd_alone_is_no_signal_once_work_moved(self):
        t = (self.session()
             .turn(stamp(2, 0), "beta first", "ok", tool=("Edit", {"file_path": self.b + "/b.py"}))
             .turn(stamp(2, 1), "carry on", "ok"))  # cwd still says Alpha: that is where Claude Code started
        self.run_import(t.write())
        self.assertEqual({c["project"] for c in self.convos()}, {self.key(self.b)})


class Safety(ImportBase):
    def test_reimport_stores_nothing_new(self):
        t = (self.session()
             .turn(stamp(2, 0), "fix alpha", "ok", tool=("Edit", {"file_path": self.a + "/a.py"}))
             .turn(stamp(2, 1), "now beta", "ok", tool=("Edit", {"file_path": self.b + "/b.py"})))
        path = t.write()
        self.run_import(path)
        before = (self.convos(), self.leaves(), self.store.run("select count(*) n from node")[0]["n"])
        self.key(make_repo(self.repos, "Gamma"))  # the store knows another project by now
        report = self.run_import(path)
        after = (self.convos(), self.leaves(), self.store.run("select count(*) n from node")[0]["n"])
        self.assertEqual(before, after)
        self.assertEqual(report["exchanges"], 0)

    def test_a_secret_in_a_tool_result_is_not_stored(self):
        secret = "ghp_" + "a1B2c3D4" * 5
        t = self.session().turn(stamp(2), "show env", "done", tool=("Bash", {"command": "env"}),
                                result="HOME=/x\nGITHUB_TOKEN=%s\n" % secret + "filler line\n" * 300)
        self.run_import(t.write())
        stored = self.everything_stored()
        self.assertNotIn(secret, stored)
        self.assertIn("filler line", stored)

    def test_sidechain_lines_and_subagent_files_are_skipped(self):
        t = self.session().turn(stamp(2), "main work", "done")
        t.lines.append(dict(t.lines[-1], isSidechain=True, uuid="x1",
                            message={"role": "assistant", "id": "msg_side", "content": [{"type": "text", "text": "SIDECHAIN-TEXT"}]}))
        t.write()
        sub = os.path.join(self.folder, SID, "subagents")
        os.makedirs(sub)
        Transcript(sub, self.a, session="agent-1").turn(stamp(2), "SUBAGENT-ASK", "x").write()
        self.run_import()
        stored = self.everything_stored()
        self.assertNotIn("SIDECHAIN-TEXT", stored)
        self.assertNotIn("SUBAGENT-ASK", stored)
        self.assertIn("main work", stored)

    def test_dry_run_writes_nothing_and_reports(self):
        t = (self.session()
             .turn(stamp(2, 0), "fix alpha", "ok", tool=("Edit", {"file_path": self.a + "/a.py"}))
             .turn(stamp(2, 1), "now beta", "ok", tool=("Edit", {"file_path": self.b + "/b.py"})))
        path = t.write()
        report = self.run_import(path, dry_run=True)
        for table in ("convo", "node", "project", "detail", "raw"):
            self.assertEqual(self.store.run(f"select count(*) n from {table}")[0]["n"], 0, table)
        self.assertEqual(report["conversations"], 2)
        self.assertEqual(report["exchanges"], 2)
        self.assertEqual(sorted(v["exchanges"] for v in report["projects"].values()), [1, 1])
        self.assertEqual(sum(report["statuses"].values()), 2)
        real = self.run_import(path)
        self.assertEqual(real["exchanges"], 2)

    def test_a_conversation_the_proxy_stored_is_not_stored_twice(self):
        t = (self.session().turn(stamp(2, 0), "explain the cache", "it keeps bytes").turn(stamp(2, 1), "and more", "yes"))
        path = t.write()
        loaded = importer.load(path)
        self.memory.index("proxylane", SID, self.key(self.a), loaded["messages"], finished=True)
        report = self.run_import(path)
        self.assertEqual([c["lane"] for c in self.convos()], ["proxylane"])
        self.assertEqual(report["skipped"]["already stored"], 1)

    def test_sessions_sharing_a_prefix_are_both_stored_whole(self):
        one, two = self.session(session="s-one"), self.session(session="s-two")
        for t in (one, two):
            for i in range(3):
                t.turn(stamp(3, i), f"shared question {i}", f"shared answer {i}")
        for i in range(3):
            one.turn(stamp(3, 10 + i), f"first branch {i}", "a")
            two.turn(stamp(2, 10 + i), f"second branch {i}", "b")
        report = self.run_import(one.write(), two.write())
        self.assertEqual({(l["lane"], l["seq"]) for l in self.leaves()},
                         {(f"import:{s}:0", n) for s in ("s-one", "s-two") for n in range(6)})
        self.assertEqual(report["skipped"]["already stored"], 0)

    def test_a_conversation_the_proxy_stored_only_in_part_is_still_imported(self):
        t = self.session()
        for i in range(4):
            t.turn(stamp(2, i), f"question {i}", f"answer {i}")
        path = t.write()
        part = importer.load(path)["messages"][:4]  # the first two exchanges
        self.memory.index("proxylane", SID, self.key(self.a), part, finished=True)
        report = self.run_import(path)
        self.assertEqual(report["skipped"]["already stored"], 0)
        self.assertEqual(len(self.leaves()), 4)

    def test_a_session_still_being_written_is_left_alone(self):
        path = self.session().turn(stamp(0), "typing now", "ok").write()
        os.utime(path, None)
        report = importer.run(self.memory, [path])  # the default idle time
        self.assertEqual(self.convos(), [])
        self.assertEqual(report["skipped"]["still open"], 1)

    def test_days_keeps_only_recent_sessions(self):
        self.session(session="old-one").turn(stamp(30), "old question", "old").write()
        self.session(session="new-one").turn(stamp(2), "new question", "new").write()
        self.run_import(days=7)
        self.assertEqual([c["lane"] for c in self.convos()], ["import:new-one:0"])

    def test_a_long_session_is_stored_whole(self):
        old = memory_mod.BACKFILL
        memory_mod.BACKFILL = 3
        self.addCleanup(setattr, memory_mod, "BACKFILL", old)
        t = self.session()
        for i in range(8):
            t.turn(stamp(2, i), f"question number {i}", f"answer {i}")
        self.run_import(t.write())
        self.assertEqual(len(self.leaves()), 8)

    def test_cli_dry_run(self):
        path = self.session().turn(stamp(2), "fix alpha", "ok", tool=("Edit", {"file_path": self.a + "/a.py"})).write()
        os.environ["ALMANAC_HOME"], old = self.tmp, os.environ["ALMANAC_HOME"]
        self.addCleanup(os.environ.__setitem__, "ALMANAC_HOME", old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["memory", "import", path, "--dry-run", "--days", "30"]), 0)
        self.assertIn("alpha", out.getvalue())
        self.assertIn("1 conversation", out.getvalue())
        self.assertIn("pending", out.getvalue())


class LocalCommands(ImportBase):
    def commands(self, stdout):
        t = self.session()
        t._add("user", "user", "<local-command-caveat>Caveat: the messages below were generated by the user</local-command-caveat>", stamp(2), isMeta=True)
        t._add("user", "user", "<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>", stamp(2))
        if stdout:
            t._add("user", "user", "<local-command-stdout></local-command-stdout>", stamp(2))
        return t.turn(stamp(2, 1), "please fix the parser", "done")

    def test_a_clear_before_a_prompt_does_not_swallow_the_prompt(self):
        for stdout in (True, False):
            loaded = importer.load(self.commands(stdout).write())
            self.assertEqual([rq.human_text(loaded["messages"][i]) for i in rq.human_turns(loaded["messages"])], ["please fix the parser"])

    def test_the_stored_exchange_is_the_typed_prompt(self):
        self.run_import(self.commands(True).write())
        self.assertEqual([r["title"] for r in self.store.run("select title from node where kind = 'leaf'")], ["please fix the parser"])

    def test_raw_results_carry_the_transcript_time(self):
        when = stamp(5)
        t = self.session().turn(when, "show it", "done", tool=("Bash", {"command": "ls"}), result="y" * (memory_mod.RESULT_CHARS + 50))
        self.run_import(t.write())
        self.assertEqual([round(r["ts"]) for r in self.store.run("select ts from raw")], [round(epoch(when))])


class Status(ImportBase):
    def test_old_exchange_ending_on_a_question_is_closed(self):
        t = self.session().turn(stamp(21), "set up the thing", "Done. Shall I also remove the old one?")
        self.run_import(t.write())
        self.assertEqual([l["status"] for l in self.leaves()], ["done"])

    def test_recent_exchange_ending_on_a_question_stays_open(self):
        t = self.session().turn(stamp(2), "set up the thing", "Done. Shall I also remove the old one?")
        self.run_import(t.write())
        self.assertEqual([l["status"] for l in self.leaves()], ["open"])

    def test_nothing_old_is_ever_open(self):
        t = (self.session().turn(stamp(30, 0), "a first thing", "Which do you want?")
             .turn(stamp(30, 1), "b second thing", "Done."))
        self.run_import(t.write())
        self.assertNotIn("open", {l["status"] for l in self.leaves()})

    def test_a_decision_in_old_typed_words_stays_a_decision(self):
        t = self.session().turn(stamp(40), "from now on keep every commit small", "Understood.")
        self.run_import(t.write())
        self.assertEqual([l["status"] for l in self.leaves()], ["decision"])

    def test_a_failure_in_the_next_message_supersedes_across_a_project_switch(self):
        t = (self.session()
             .turn(stamp(2, 0), "fix alpha", "ok", tool=("Edit", {"file_path": self.a + "/a.py"}))
             .turn(stamp(2, 1), "that didn't work, it still fails", "sorry", tool=("Edit", {"file_path": self.b + "/b.py"})))
        self.run_import(t.write())
        first = [l for l in self.leaves() if l["lane"].endswith(":0")]
        self.assertEqual([l["status"] for l in first], ["superseded"])

    def test_stored_dates_are_the_transcript_dates(self):
        first, second = stamp(5, 0), stamp(5, 90)
        t = self.session().turn(first, "first ask", "ok").turn(second, "second ask", "ok")
        self.run_import(t.write())
        self.assertEqual([round(l["ts"]) for l in self.leaves()], [round(epoch(first)), round(epoch(second))])
        convo = self.convos()[0]
        self.assertEqual(round(convo["started"]), round(epoch(first)))
        self.assertEqual(round(convo["updated"]), round(epoch(second)))
        topics = self.store.run("select ts from node where kind = 'topic'")
        self.assertTrue(all(r["ts"] <= epoch(second) + 1 for r in topics))

    def test_only_the_last_exchange_of_an_imported_conversation_may_be_open(self):
        t = (self.session().turn(stamp(2, 0), "a first thing", "Which do you want?")
             .turn(stamp(2, 1), "b second thing", "Done. Shall I also remove the old one?"))
        self.run_import(t.write())
        self.assertEqual([l["status"] for l in self.leaves()], ["done", "open"])
        first, last = [r["id"] for r in self.store.run("select id from node where kind = 'leaf' order by seq")]
        self.memory.set_summary(first, "First", "Asked which.", status="open")  # the model calls it unfinished
        self.memory.set_summary(last, "Second", "Done.", status="open")
        self.assertEqual([l["status"] for l in self.leaves()], ["done", "open"])

    def test_a_model_summary_cannot_reopen_old_work(self):
        t = self.session().turn(stamp(21), "set up the thing", "Done.")
        self.run_import(t.write())
        node = self.store.run("select id from node where kind = 'leaf'")[0]["id"]
        self.memory.set_summary(node, "Set up", "It was set up.", status="open")
        self.assertEqual([l["status"] for l in self.leaves()], ["done"])


if __name__ == "__main__":
    unittest.main()
