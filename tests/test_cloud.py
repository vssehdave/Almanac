import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import Rig, sse
from test_memory import Case, human, reply, result, said

from getalmanac import cli, cloud, config, proxy as proxy_mod
from getalmanac.report import Log

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

    def test_tool_output_is_not_in_the_packet_stored_or_live(self):
        odd = "XYZ-unknown-secret-123"
        out = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "line one " + odd + " line two"}]}
        messages = [human("Add retry with backoff to the client"), said("Added retry."), human("Now write the docs for it"),
                    said("Reading it.", tool=("Read", {"file_path": "a.py"})), out, said("Docs are in README.")]
        self.memory.index("lane-1", "s1", self.key, messages + [human(TASK)], reply("Halfway."))
        convo = self.memory.recent(project=self.key)[0]["id"]
        self.assertIn(odd, self.memory.detail(self.memory.leaves("lane-1")[1]))  # it is stored, only the packet leaves it out
        text = cloud.packet(self.memory, convo, self.alpha)
        self.assertNotIn(odd, text)
        self.assertIn("[result of Read:", text)
        live = cloud.packet(self.memory, convo, self.alpha, live=messages[:2] + [human("Do the next thing"), said("On it.", tool=("Read", {})), out])
        self.assertNotIn(odd, live)

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


QUIET_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}  # the machine's hooks, signing and identity stay out


class HandoffTest(CloudCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, dict(QUIET_GIT, ALMANAC_HOME=tempfile.mkdtemp(prefix="almanac-cfg-")))
        patcher.start()
        self.addCleanup(patcher.stop)
        git(self.alpha, "config", "user.name", "t")
        git(self.alpha, "config", "user.email", "t@example.com")
        self.bare = os.path.join(self.tmp, "origin.git")
        git(self.tmp, "init", "-q", "--bare", self.bare)
        git(self.alpha, "remote", "set-url", "origin", self.bare)
        self.log_path = os.path.join(self.tmp, "requests.jsonl")
        cloud.turn(self.memory, self.alpha, True)  # `almanac cloud on` in alpha
        cfg = config.load()
        cfg["cloud"].update(auto=True, heartbeat_minutes=10)
        self.layer = cloud.HandoffLayer(self.memory, cfg, background=False)
        self.ctx = mock.Mock(cwd=self.alpha, agent="", lane="lane-1", session="s1", proxy=mock.Mock(log=Log(self.log_path)))

    def turn(self, messages, stop):
        body = {"messages": messages, "tools": [{"name": "Bash", "input_schema": {}}]}
        self.layer.transform(body, self.ctx)
        self.layer.observe(self.ctx, body, reply("Working on it.", stop=stop))

    def pushed(self, name=None):
        ref = "cloud/handoff"
        if name is None:
            return git(self.bare, "rev-parse", "--verify", "-q", ref) if self.has(ref) else ""
        return git(self.bare, "show", f"{ref}:{name}")

    def has(self, ref):
        return subprocess.run(["git", "--git-dir", self.bare, "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0

    def state(self):
        return json.loads(self.pushed("almanac-handoff.json"))

    def test_the_snapshot_holds_the_work_and_leaves_the_checkout_alone(self):
        self.write(".gitignore", "secret.log\n")
        git(self.alpha, "add", ".gitignore")
        git(self.alpha, "commit", "-q", "-m", "ignore logs")
        self.write("src/app.py", "print('staged')\n")
        git(self.alpha, "add", "src/app.py")
        self.write("src/app.py", "print('two')\n")  # the index and the work tree now differ
        self.write("new.txt", "brand new\n")
        self.write("secret.log", "do not ship\n")
        git(self.alpha, "status")  # let git refresh the index once, so only the snapshot could change it
        index = os.path.join(self.alpha, ".git", "index")

        def look():
            with open(index, "rb") as handle:
                data = handle.read()
            with open(os.path.join(self.alpha, ".git", "HEAD"), "rb") as handle:
                head = handle.read()
            # newer git (2.56 in CI) records the pushed branch as origin's remote-tracking ref even when pushing by address
            refs = [r for r in git(self.alpha, "for-each-ref").splitlines() if not r.endswith("refs/remotes/origin/cloud/handoff")]
            return data, head, git(self.alpha, "rev-parse", "HEAD"), git(self.alpha, "branch", "--list"), \
                refs, git(self.alpha, "status"), git(self.alpha, "status", "--short")

        before = look()
        self.turn([human("Make it print two")], "tool_use")
        self.layer.beat(time.time() + 601)
        self.assertEqual(look(), before)
        head = git(self.alpha, "rev-parse", "HEAD")
        files = git(self.bare, "ls-tree", "-r", "--name-only", "cloud/handoff").splitlines()
        self.assertEqual(sorted(files), [".gitignore", "ALMANAC_HANDOFF.md", "almanac-handoff.json", "src/app.py"])  # untracked new.txt stays home
        self.assertEqual(self.pushed("src/app.py"), "print('two')")  # the work tree, not the index
        self.assertEqual(git(self.bare, "rev-parse", "cloud/handoff^"), head)
        meta = self.state()
        self.assertEqual((meta["state"], meta["base_branch"], meta["base_commit"], meta["session"]), ("working", "work", head, "s1"))
        text = self.pushed("ALMANAC_HANDOFF.md")
        self.assertIn("The last message the person typed:\n\nMake it print two", text)
        self.assertIn("This branch is a snapshot of that local work tree", text)
        self.assertNotIn("not in your clone", text)
        self.assertNotIn("did not travel", text)

    def test_a_secret_in_the_packet_is_redacted_in_the_pushed_blob(self):
        key = "ghp_" + "Z9y8" * 8
        self.memory.index("lane-1", "s1", self.key, [human(f"Deploy with {key} please")], reply("Deployed."))
        self.turn([human(f"Deploy with {key} please"), said("Done."), human("DB_PASS=s3cretValue99 now rotate it")], "tool_use")
        self.layer.beat(time.time() + 601)
        text = self.pushed("ALMANAC_HANDOFF.md")
        self.assertIn("now rotate it", text)
        self.assertIn("- Deploy with [key removed] please ->", text)
        self.assertNotIn(key, text)
        self.assertNotIn("s3cretValue99", text)

    def test_no_secret_file_tag_or_raw_secret_reaches_the_remote(self):
        git(self.alpha, "config", "push.followTags", "true")  # the user's own config must not widen the push
        git(self.alpha, "tag", "-a", "v-private", "-m", "private")
        self.write("config.py", "x = 1\n")
        self.write("deploy.pem", "placeholder\n")
        git(self.alpha, "add", ".")
        git(self.alpha, "commit", "-q", "-m", "config")
        stripe, ghp, hf = "sk_live_" + "a1B2" * 6, "ghp_" + "Q7w8" * 9, "hf_" + "abcdEFGH" * 4
        self.write("config.py", f'STRIPE = "{stripe}"\n')  # a tracked edit with a secret
        self.write("deploy.pem", "changed\n")  # a tracked credential name
        self.write("src/app.py", "print('two')\n")  # a clean tracked edit
        self.write(".env", f"GITHUB_TOKEN={ghp}\n")  # untracked and not ignored
        call = said("Setting the key.", tool=("Bash", {"command": 'export API_KEY="Zq9x8c7v6b5n4m"; cat key'}))
        out = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content":
               f"HF={hf}\nAuthorization: token {'0f' * 20}\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234567890abcdef"}]}
        self.turn([human("Wire the payment client"), call, out], "tool_use")
        self.layer.beat(time.time() + 601)
        refs = git(self.bare, "for-each-ref", "--format=%(refname)").split()
        self.assertEqual(refs, ["refs/heads/cloud/handoff"])
        files = git(self.bare, "ls-tree", "-r", "--name-only", "cloud/handoff").split()
        self.assertNotIn(".env", files)
        self.assertEqual(self.pushed("config.py"), "x = 1")  # held at its HEAD version
        self.assertEqual(self.pushed("deploy.pem"), "placeholder")
        self.assertEqual(self.pushed("src/app.py"), "print('two')")
        text = self.pushed("ALMANAC_HANDOFF.md")
        self.assertIn("Left out of this branch", text)
        self.assertIn("config.py, deploy.pem", text)
        self.assertIn("almanac hand-off: working [skip ci]", git(self.bare, "log", "-1", "--format=%s", "cloud/handoff"))
        revs = git(self.bare, "rev-list", "--all").split()
        for needle in (stripe, ghp, hf, "Zq9x8c7v6b5n4m", "0f0f0f0f0f", "MIIEpAIBAAKCAQEA"):
            found = subprocess.run(["git", "--git-dir", self.bare, "grep", "-F", needle, *revs], capture_output=True, text=True)
            self.assertEqual(found.stdout, "", needle)

    def test_ssh_cannot_prompt_and_close_does_not_hold_up_exit(self):
        with mock.patch.dict(os.environ), mock.patch.object(cloud, "_must", wraps=cloud._must) as must:
            os.environ.pop("GIT_SSH_COMMAND", None)
            os.environ.pop("GIT_SSH", None)
            self.turn([human("A long task")], "tool_use")
            self.layer.beat(time.time() + 601)
        push = [c for c in must.call_args_list if c.args[1] == "push"][0]
        self.assertEqual(push.kwargs["env"], {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"})
        first = self.pushed()
        self.layer.busy.acquire()  # a heartbeat still pushing
        try:
            started = time.time()
            self.layer.close()
            self.assertLess(time.time() - started, 8)
        finally:
            self.layer.busy.release()
        self.assertEqual(self.pushed(), first)

    def test_the_heartbeat_thread_pushes_on_its_own(self):
        layer = cloud.HandoffLayer(self.memory, config.load())
        body ={"messages": [human("A long task")], "tools": [{"name": "Bash", "input_schema": {}}]}
        layer.transform(body, self.ctx)
        layer.observe(self.ctx, body, reply("done", stop="end_turn"))  # a turn end wakes the thread
        for _ in range(100):
            if self.has("cloud/handoff"):
                break
            time.sleep(0.1)
        self.assertEqual(self.state()["state"], "idle")

    def test_only_a_repository_turned_on_takes_part(self):
        beta = self.beta  # cloud.auto on, but never turned on here
        git(beta, "init", "-q", "-b", "work")
        with open(os.path.join(beta, "f.txt"), "w") as handle:
            handle.write("x\n")
        git(beta, "add", ".")
        git(beta, "commit", "-q", "-m", "first")
        other = os.path.join(self.tmp, "other.git")
        git(self.tmp, "init", "-q", "--bare", other)
        git(beta, "remote", "set-url", "origin", other)
        ctx = mock.Mock(cwd=beta, agent="", lane="lane-b", session="s2", proxy=self.ctx.proxy)
        body = {"messages": [human("A long task")], "tools": [{"name": "Bash", "input_schema": {}}]}
        with mock.patch.object(cloud, "_must", side_effect=AssertionError("git in a repository not turned on")):
            self.layer.transform(body, ctx)
            self.layer.observe(ctx, body, reply("Working on it.", stop="tool_use"))
            self.layer.beat(time.time() + 601)
        self.assertNotIn(beta, self.layer.repos)
        self.assertEqual(git(other, "for-each-ref"), "")
        self.turn([human("A long task")], "tool_use")
        self.layer.beat(time.time() + 601)
        self.assertEqual(self.state()["state"], "working")  # alpha is on: pushed
        first = self.pushed()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli, "_memory_handle", return_value=self.memory), \
                mock.patch.object(os, "getcwd", return_value=self.alpha):
            self.assertEqual(cli.main(["cloud", "off"]), 0)
            self.assertEqual(cli.main(["cloud", "status"]), 0)
        self.assertIn("hand-off off for", out.getvalue())
        self.assertIn("this repo   off", out.getvalue())
        with mock.patch.object(cloud, "_must", side_effect=AssertionError("git after off")):
            self.turn([human("A long task")], "tool_use")
            self.layer.beat(time.time() + 5000)
            self.layer.close()
        self.assertEqual(self.pushed(), first)
        self.assertEqual(self.layer.repos, {})

    def test_idle_is_pushed_once_when_the_turn_ends_and_the_heartbeat_stops(self):
        now = time.time()
        self.turn([human("A long task")], "tool_use")
        self.layer.beat(now + 60)
        self.assertEqual(self.pushed(), "")  # the first heartbeat comes one interval in
        self.layer.beat(now + 601)
        first = self.pushed()
        self.assertEqual(self.state()["state"], "working")
        self.layer.beat(now + 700)
        self.assertEqual(self.pushed(), first)  # not due yet
        time.sleep(1.1)  # commit times have one-second resolution: the same second would give the same commit
        self.layer.beat(now + 1300)
        self.assertNotEqual(self.pushed(), first)  # a tool is still running: a new heartbeat
        self.turn([human("A long task"), said("ok", tool=("Bash", {})), result()], "end_turn")
        self.layer.beat()
        self.assertEqual(self.state()["state"], "idle")
        idle = self.pushed()
        self.layer.beat(time.time() + 5000)
        self.assertEqual(self.pushed(), idle)  # stopped until the next request
        self.assertEqual(self.memory.store.get(f"handoff:{git(self.alpha, 'rev-parse', '--show-toplevel')}")["state"], "idle")

    def test_a_subagent_or_a_side_call_changes_nothing(self):
        self.layer.transform({"messages": [human("x")], "tools": [{"name": "Bash", "input_schema": {}}]},
                             mock.Mock(cwd=self.alpha, agent="sub-1"))
        self.layer.transform({"messages": [human("x")]}, self.ctx)  # no client tools: an inner model call
        self.assertEqual(self.layer.repos, {})

    def test_a_push_failure_is_one_log_line_and_never_raises(self):
        git(self.alpha, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        self.turn([human("A long task")], "tool_use")
        self.layer.beat(time.time() + 601)
        lines = Log(self.log_path).tail()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["kind"], lines[0]["where"]), ("error", "handoff"))
        self.layer.beat(time.time() + 650)
        self.assertEqual(len(Log(self.log_path).tail()), 1)  # retries wait an interval
        outside = cloud.HandoffLayer(self.memory, {"cloud": {"heartbeat_minutes": 10}}, background=False)
        outside.observe(mock.Mock(cwd=self.tmp, agent="", lane="x", session="s", proxy=self.ctx.proxy),
                        {"messages": [human("y")], "tools": [{"input_schema": {}}]}, reply("ok"))
        outside.beat()  # not a repository turned on: ignored, nothing logged
        self.assertEqual((len(Log(self.log_path).tail()), outside.repos), (1, {}))

    def failures(self, count, status=500, layer=None):
        """`count` requests of the main conversation that end with `status`, as the proxy reports them."""
        layer = layer or self.layer
        body = {"messages": [human("A long task")], "tools": [{"name": "Bash", "input_schema": {}}]}
        for _ in range(count):
            layer.transform(body, self.ctx)
            layer.outcome(self.ctx, body, status)

    def test_five_failures_in_a_row_push_stalled_once(self):
        self.failures(5, 529)
        self.assertEqual(self.pushed(), "")  # outcome only counts: git runs on the heartbeat thread
        self.layer.beat(time.time() + 5)  # well inside the heartbeat interval
        self.assertEqual(self.state()["state"], "stalled")
        self.assertIn("task", git(self.bare, "show", "cloud/handoff:ALMANAC_HANDOFF.md"))
        first = self.pushed()
        self.failures(3, 502)
        self.layer.beat(time.time() + 5000)
        self.assertEqual(self.pushed(), first)  # one push per stall, however long it goes on
        self.assertIn(", stalled, commit ", cloud.status(self.alpha, {"cloud": {"auto": True, "heartbeat_minutes": 10}}, self.memory.store))

    def test_four_failures_or_4xx_replies_do_not_stall(self):
        self.failures(4)
        self.failures(5, 429)
        self.failures(5, 400)  # a usage limit and a bad request are not an outage, and they do not reset the count either
        self.layer.beat(time.time() + 5)
        self.assertEqual(self.pushed(), "")
        self.failures(1)
        self.layer.beat(time.time() + 5)
        self.assertEqual(self.state()["state"], "stalled")

    def test_a_success_resets_the_count_and_the_next_push_says_working(self):
        self.failures(4)
        self.failures(1, 200)
        self.failures(4)
        self.layer.beat(time.time() + 5)
        self.assertEqual(self.pushed(), "")  # 4, a success, 4: never 5 in a row
        self.failures(1)
        self.layer.beat(time.time() + 5)
        self.assertEqual(self.state()["state"], "stalled")
        self.failures(1, 200)
        self.layer.beat(time.time() + 5)  # not an interval later
        self.assertEqual(self.state()["state"], "working")
        stalled_again = self.pushed()
        self.failures(5)
        self.layer.beat(time.time() + 5)
        self.assertEqual(self.state()["state"], "stalled")  # a new stall pushes again
        self.assertNotEqual(self.pushed(), stalled_again)

    def test_a_stall_is_not_hidden_by_a_later_idle_or_working_beat(self):
        self.failures(5)
        self.layer.beat(time.time() + 5)
        self.layer.beat(time.time() + 5000)  # the client has given up: no "idle" over the "stalled"
        self.layer.close()
        self.assertEqual(self.state()["state"], "stalled")

    def test_a_repository_not_turned_on_or_stall_after_0_gets_nothing(self):
        other = cloud.HandoffLayer(self.memory, dict(config.load(), cloud=dict(config.load()["cloud"], stall_after=0)), background=False)
        self.failures(9, layer=other)
        other.beat(time.time() + 5)
        self.assertEqual(self.pushed(), "")
        cloud.turn(self.memory, self.alpha, False)
        self.layer.keys.clear()
        with mock.patch.object(cloud, "_must", side_effect=AssertionError("git in a repository not turned on")):
            self.failures(9)
            self.layer.beat(time.time() + 5)
        self.assertEqual((self.pushed(), self.layer.repos), ("", {}))

    def test_a_failing_stall_push_does_not_raise_and_is_not_repeated_at_once(self):
        git(self.alpha, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        self.failures(5)
        self.layer.beat(time.time() + 5)
        self.assertEqual(len(Log(self.log_path).tail()), 1)
        self.layer.beat(time.time() + 10)
        self.assertEqual(len(Log(self.log_path).tail()), 1)  # the retry waits an interval
        git(self.alpha, "remote", "set-url", "origin", self.bare)
        self.layer.beat(time.time() + 700)
        self.assertEqual(self.state()["state"], "stalled")

    def test_through_the_proxy_failures_stall_and_a_reply_clears_it(self):
        replies = [(529, {}, b"overloaded")] * 5 + [(429, {}, b"limit"), (200, {"content-type": "text/event-stream"}, sse("back"))]
        rig = Rig(respond=lambda req: replies.pop(0), layers=[self.layer])
        self.addCleanup(rig.close)
        body = {"model": "m", "system": f"Primary working directory: {self.alpha}", "messages": [human("Ship it")],
                "tools": [{"name": "Bash", "input_schema": {}}]}
        with mock.patch.object(cloud, "_must") as must:
            self.assertEqual([rig.messages(body)[0] for _ in range(5)], [529] * 5)
            rig.records()
        must.assert_not_called()  # the count is kept on the request path, git is not run there
        self.layer.beat()
        self.assertEqual(self.state()["state"], "stalled")
        self.assertEqual(rig.messages(body)[0], 429)
        self.layer.beat(time.time() + 5)
        self.assertEqual(self.state()["state"], "stalled")  # a 429 is no recovery
        self.assertEqual(rig.messages(body)[0], 200)
        rig.records()
        self.layer.beat()
        self.assertEqual(self.state()["state"], "idle")  # the reply ended the turn

    def test_through_the_proxy_a_request_is_not_held_up_and_the_turn_end_pushes(self):
        rig = Rig(respond=lambda req: (200, {"content-type": "text/event-stream"}, sse("done")), layers=[self.layer])
        self.addCleanup(rig.close)
        body = {"model": "m", "system": f"Primary working directory: {self.alpha}", "messages": [human("Ship it")],
                "tools": [{"name": "Bash", "input_schema": {}}]}
        with mock.patch.object(cloud, "_must") as must:
            self.assertEqual(rig.messages(body)[0], 200)
            rig.records()
        must.assert_not_called()  # git runs on the layer's thread only
        self.assertTrue(self.layer.repos[self.alpha]["ended"])
        self.layer.beat()
        self.assertEqual(self.state()["state"], "idle")

    def test_auto_off_adds_nothing_and_runs_no_git(self):
        cfg = config.load()
        self.assertFalse(cfg["cloud"]["auto"])
        with mock.patch.object(cloud, "_must", side_effect=AssertionError("git with auto off")):
            built = proxy_mod.build(cfg)
            self.addCleanup(built.store.close)
            self.assertFalse(any(isinstance(layer, cloud.HandoffLayer) for layer in built.layers))
            cfg["cloud"]["auto"] = "false"  # typed by hand into config.json
            typed = proxy_mod.build(cfg)
            self.addCleanup(typed.store.close)
            self.assertFalse(any(isinstance(layer, cloud.HandoffLayer) for layer in typed.layers))
            cfg["cloud"]["auto"] = True
            on = proxy_mod.build(cfg)
            self.addCleanup(on.store.close)
            self.assertTrue(any(isinstance(layer, cloud.HandoffLayer) for layer in on.layers))

    def test_close_marks_a_running_task_idle(self):
        self.turn([human("A long task")], "tool_use")
        self.layer.beat(time.time() + 601)
        self.layer.close()
        self.assertEqual(self.state()["state"], "idle")

    def test_status_shows_the_last_beat_and_a_takeover_branch(self):
        cfg = {"cloud": {"auto": True, "heartbeat_minutes": 10}}
        self.assertIn("none pushed from this machine", cloud.status(self.alpha, cfg, self.memory.store))
        self.turn([human("A long task")], "tool_use")
        self.layer.beat(time.time() + 601)
        sha = git(self.alpha, "rev-parse", "--short=12", self.pushed())
        text = cloud.status(self.alpha, cfg, self.memory.store)
        self.assertIn(", working, commit " + sha, text)
        self.assertIn("takeover    none on origin", text)
        git(self.bare, "branch", f"cloud/almanac-takeover-{sha}", "cloud/handoff")
        git(self.bare, "branch", "cloud/other", "cloud/handoff")
        text = cloud.status(self.alpha, cfg, self.memory.store)
        self.assertIn(f"cloud/almanac-takeover-{sha} on origin: pull the work back", text)
        self.assertNotIn("cloud/other", text)
        self.assertIn("  auto        on", text)

    def test_the_watchdog_prompt_holds_every_stop_condition(self):
        text = cloud.watchdog(10)
        for part in ("git fetch origin cloud/handoff`. If it fails (the branch does not exist), stop.",
                     'If its "state" is "idle", or its "base_branch" is', "If the number is below 1800",
                     'If its "state" is "stalled"', "skip the rest of this", "go on to step 4",
                     "`git ls-remote origin refs/heads/cloud/almanac-takeover-SHA`. If it prints anything, stop",
                     "git checkout -b cloud/almanac-takeover-SHA FETCH_HEAD", "is a record of earlier work",
                     "draft pull request", "Never delete a branch, never force-push", "never push to the base branch"):
            self.assertIn(part, text)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli, "_memory_handle", return_value=self.memory):
            self.assertEqual(cli.main(["cloud", "watchdog"]), 0)
        self.assertIn("/web-setup", out.getvalue())
        self.assertIn("almanac config set cloud.auto on", out.getvalue())
        self.assertIn(text, out.getvalue())

    def test_config_takes_on_and_off(self):
        home = tempfile.mkdtemp(prefix="almanac-cfg-")
        with mock.patch.dict(os.environ, {"ALMANAC_HOME": home}):
            self.assertIs(config.set_value("cloud.auto", "on"), True)
            self.assertIs(config.load()["cloud"]["auto"], True)
            self.assertIs(config.set_value("cloud.auto", "off"), False)
            self.assertEqual(config.set_value("cloud.heartbeat_minutes", "5"), 5)


if __name__ == "__main__":
    unittest.main()
