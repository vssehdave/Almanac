import io
import os
import re
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_memory import Case, human, reply, result, said

from getalmanac import cli, cloud

TASK = "Finish the retry work.\n" + "\n".join(f"Step {n}: check case {n} of the backoff table and write it down." for n in range(60))


def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")  # the machine's own hooks and signing stay out
    done = subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


@unittest.skipUnless(shutil.which("git"), "needs git")
class CloudCase(Case):
    def setUp(self):
        super().setUp()
        git(self.alpha, "init", "-q", "-b", "work")  # make_repo wrote the config; this makes it a real repository
        self.write("src/app.py", "print('one')\n")
        git(self.alpha, "add", ".")
        git(self.alpha, "commit", "-q", "-m", "first commit")
        self.key = self.memory.projects.see(self.alpha)

    def write(self, name, text):
        with open(os.path.join(self.alpha, name), "w") as handle:
            handle.write(text)

    def talk(self, extra=0, first="Add retry with backoff to the client"):
        messages = [human(first), said("Reading the client.", tool=("Read", {"file_path": self.alpha + "/src/app.py"})), result(),
                    said("Added retry with exponential backoff."), human("Now write the docs for it"), said("Docs are in README.")]
        for n in range(extra):
            messages += [human(f"Extra request number {n} about the widget"), said(f"Handled extra {n}.")]
        self.memory.index("lane-1", "s1", self.key, messages + [human(TASK)], reply("Halfway: cases 0 to 30 are checked."))
        return self.memory.recent(project=self.key)[0]["id"]

    def push(self):
        git(self.alpha, "update-ref", "refs/remotes/origin/work", "HEAD")
        git(self.alpha, "branch", "-q", "--set-upstream-to=origin/work")


class PacketTest(CloudCase):
    def test_it_holds_the_task_the_outline_the_repository_and_the_untracked_warning(self):
        convo = self.talk()
        self.memory.set_summary(self.memory.leaves("lane-1")[0], "Retry added", "The client now retries with backoff and jitter.")
        git(self.alpha, "remote", "set-url", "origin", "https://bob:hunter2" + "pw@github.com/Someone/Alpha.git")
        self.write("src/app.py", "print('two')\n")
        self.write("notes.txt", "x")
        text = cloud.packet(self.memory, convo, self.alpha)
        self.assertGreater(len(TASK), 2000)  # longer than the start the exchange text keeps
        self.assertIn(TASK, text)
        self.assertIn("\n- Add retry with backoff to the client\n- Now write the docs for it\n", text)
        self.assertIn("- Retry added -> The client now retries with backoff and jitter.\n"
                      "- Now write the docs for it -> Docs are in README.\n", text)
        self.assertIn("User: (the task above)\nAssistant: Halfway: cases 0 to 30 are checked.", text)
        self.assertEqual(text.count("Step 59:"), 1)
        self.assertNotIn("left out", text)
        places = [text.index(part) for part in ("This is a hand-off packet", "## The task", "## What was done",
                                                "## Most recent work", "## Repository")]
        self.assertEqual(places, sorted(places))
        self.assertEqual(places[0], 0)
        for line in ("- branch: work", f"- HEAD: {git(self.alpha, 'rev-parse', '--short', 'HEAD')} first commit",
                     "- remote: https://github.com/Someone/Alpha.git", "- pushed: no, HEAD is not on any remote-tracking branch (the branch has no upstream)",
                     "- git status --short:", "     M src/app.py", "    ?? notes.txt"):
            self.assertIn(line + "\n", text)
        self.assertIn("WARNING: 1 local commit(s) were not on the remote", text)
        self.assertIn("WARNING: the changes to tracked files listed above were not committed", text)
        self.assertRegex(text, r"WARNING: untracked files .* did not travel with this session: notes\.txt$")
        self.assertNotIn("hunter2", text)
        self.assertLessEqual(len(text), cloud.LIMIT)

    def test_a_pushed_clean_repository_and_a_folder_outside_git(self):
        convo = self.talk()
        self.push()
        text = cloud.packet(self.memory, convo, self.alpha)
        self.assertIn("- pushed: yes, HEAD is on origin/work\n- git status --short: clean", text)
        self.assertNotIn("WARNING", text)
        self.assertTrue(cloud.packet(self.memory, convo, self.tmp).endswith("is not a git repository."))
        self.assertEqual(cloud.packet(self.memory, 999, self.alpha), "")

    def test_a_secret_in_stored_text_is_not_in_the_packet(self):
        key, word = "ghp_" + "A1b2" * 8, "hunter2-" + "hunter2"
        convo = self.talk(first=f"Use the deploy key {key} for the client")
        leaf = self.memory.leaves("lane-1")[1]
        self.store.run("update node set title = title || ?, text = text || ? where id = ?", (" " + key, " " + key, leaf))
        self.store.run("update detail set text = text || ?", ("\npassword = " + word,))
        text = cloud.packet(self.memory, convo, self.alpha)
        self.assertNotIn(key, text)
        self.assertNotIn(word, text)
        self.assertIn("[key removed]", text)

    def test_the_cap_drops_old_outline_lines_and_keeps_the_task(self):
        convo = self.talk(extra=60)
        text = cloud.packet(self.memory, convo, self.alpha, limit_chars=9000)
        self.assertLessEqual(len(text), 9000)
        self.assertIn(TASK, text)
        outline = text.split("## What was done")[1].split("## Most recent work")[0]
        left = int(re.search(r"\((\d+) older ones left out", outline).group(1))
        self.assertEqual(left + outline.count(" -> "), 63)
        self.assertGreater(left, 0)
        self.assertNotIn("Add retry with backoff to the client ->", outline)
        self.assertIn("- Extra request number 59 about the widget -> Handled extra 59.", outline)
        self.assertIn("(42 older ones not listed)", text)  # the earlier requests keep the latest 20 of 62

    def test_the_same_state_gives_the_same_packet(self):
        convo = self.talk(extra=3)
        self.write("notes.txt", "x")
        self.assertEqual(cloud.packet(self.memory, convo, self.alpha), cloud.packet(self.memory, convo, self.alpha))


class SendTest(CloudCase):
    def run_cli(self, *argv, cwd=None, claude="claude-x", stdin=None, call=None):
        seen = {}

        def fake(args, env, cwd):
            seen.update(argv=args, env=env, cwd=cwd)
            return call(args) if call else 7

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_memory_handle", return_value=self.memory), mock.patch.object(cloud, "_call", fake), \
                mock.patch.object(cli.launcher, "find_claude", return_value=claude), \
                mock.patch.object(os, "getcwd", return_value=cwd or self.alpha), \
                mock.patch.object(cloud.sys, "stdin", stdin or io.StringIO()), \
                mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1/secret"}), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["cloud", *argv])
        self.err = err.getvalue()
        return code, seen, out.getvalue()

    def test_send_passes_the_packet_and_the_exit_code(self):
        convo = self.talk()
        code, seen, out = self.run_cli("send", "--yes")  # no id: the latest conversation of this folder's project
        text = cloud.packet(self.memory, convo, self.alpha)
        self.assertEqual((code, seen["argv"], seen["cwd"]), (7, ["claude-x", "--cloud", text], self.alpha))
        self.assertNotIn("ANTHROPIC_BASE_URL", seen["env"])
        self.assertNotIn("CCR_FORCE_BUNDLE", seen["env"])  # Claude Code chooses how the repository travels
        self.assertIn(f"conversation {convo} (Add retry with backoff to the client)", out)
        self.assertIn(text, out)  # the packet is shown before it goes
        self.assertIn(f"{len(text):,} characters", out)
        self.assertIn("claude --cloud failed (exit 7)", self.err)
        self.assertEqual(self.run_cli("send", "--yes", "--conversation", str(convo))[1]["argv"][2], text)
        code, seen, out = self.run_cli("packet")
        self.assertEqual((code, seen, out), (0, {}, text + "\n"))

    def test_send_says_what_the_cloud_will_not_see(self):
        self.talk()
        out = self.run_cli("send", "--yes")[2]
        self.assertIn("at branch work (it has no upstream", out)
        self.assertRegex(out, r"unpushed +1: [0-9a-f]+ first commit")
        self.push()
        self.write("notes.txt", "x")
        self.write("src/app.py", "print('two')\n")
        out = self.run_cli("send", "--yes")[2]
        self.assertIn("at branch work (upstream origin/work)", out)
        self.assertNotIn("unpushed", out)
        self.assertIn("uncommitted 1: src/app.py", out)
        self.assertIn("untracked   1: notes.txt", out)
        git(self.alpha, "commit", "-q", "-a", "-m", "second commit")
        self.assertRegex(self.run_cli("send", "--yes")[2], r"unpushed +1: [0-9a-f]+ second commit")

    def test_send_asks_first(self):
        self.talk()
        tty = lambda answer: mock.Mock(isatty=lambda: True, readline=lambda: answer)  # noqa: E731
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(self.run_cli("send", stdin=tty("n"))[:2], (1, {}))
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.run_cli("send", stdin=tty(""))[:2], (1, {}))
        with mock.patch("builtins.input", return_value="y"):
            self.assertEqual(self.run_cli("send", stdin=tty("y"))[0], 7)
        self.assertEqual(self.run_cli("send")[:2], (2, {}))  # not a terminal and no --yes
        self.assertIn("--yes", self.err)

    def test_a_batch_file_a_long_line_or_a_failed_start_is_refused_plainly(self):
        self.talk()
        with mock.patch.object(cloud, "WINDOWS", True):
            for name in ("claude.cmd", "C:/npm/CLAUDE.BAT"):
                self.assertEqual(self.run_cli("send", "--yes", claude=name)[:2], (2, {}))
                self.assertIn("claude.exe", self.err)
            with mock.patch.object(cloud, "CMD_LIMIT", 100):
                self.assertEqual(self.run_cli("send", "--yes")[:2], (2, {}))
                self.assertIn("too long for a Windows command line", self.err)

        def gone(_args):
            raise FileNotFoundError(2, "The system cannot find the file specified")

        self.assertEqual(self.run_cli("send", "--yes", call=gone)[0], 2)
        self.assertIn("could not start claude-x: The system cannot find the file specified", self.err)
        self.assertNotIn("Traceback", self.err)
        self.assertEqual(self.run_cli("send", "--yes", call=lambda _args: 0)[0], 0)
        self.assertNotIn("failed", self.err)

    def test_quoting_is_measured_and_the_packet_shrinks_to_fit(self):
        with mock.patch.object(cloud, "WINDOWS", True):
            self.assertTrue(cloud.too_long(["x", '"' * 20000]))  # 20,000 quotes become 40,000 characters
            self.assertFalse(cloud.too_long(["x", "a" * 20000]))
            convo = self.talk(extra=60)
            with mock.patch.object(cloud, "CMD_LIMIT", 12000):
                text = cloud.fitted(self.memory, convo, self.alpha)
                self.assertFalse(cloud.too_long(["claude.exe", "--cloud", text]))
                self.assertIn(TASK, text)

    def test_nothing_to_hand_off_or_no_claude_is_exit_code_2(self):
        self.assertEqual(self.run_cli("send")[:2], (2, {}))  # nothing stored yet
        self.assertEqual(self.run_cli("packet")[0], 2)
        convo = self.talk()
        self.assertEqual(self.run_cli("send", "--yes", cwd=self.beta)[:2], (2, {}))  # another project
        self.assertEqual(self.run_cli("send", "--yes", "--conversation", str(convo + 50))[:2], (2, {}))
        self.assertEqual(self.run_cli("send", "--yes", claude=None)[:2], (2, {}))


if __name__ == "__main__":
    unittest.main()
