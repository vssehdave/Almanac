import json
import os
import subprocess
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "src", "getalmanac", "bundle", "guard", "scripts")


def run_raw(script, text):
    done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], input=text, capture_output=True, text=True)
    return done.returncode, done.stderr


class BuilderGitGuard(unittest.TestCase):
    def run_bash(self, cmd, at):
        text = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "agent_type": at})
        return run_raw("builder_git_guard.py", text)

    def blocked(self, cmd, at="almanac:builder"):
        code, err = self.run_bash(cmd, at)
        self.assertEqual(code, 2, cmd)
        self.assertIn("builders do not use git", err, cmd)

    def allowed(self, cmd, at):
        self.assertEqual(self.run_bash(cmd, at)[0], 0, cmd)

    def test_builder_git_commands_are_blocked(self):
        for cmd in ("git status", "git commit -m x", "cd a && git add .", "rtk git diff", "x; git.exe log"):
            self.blocked(cmd)

    def test_builder_non_git_commands_are_allowed(self):
        for cmd in ("python -m unittest discover -s tests -p test_x.py", "echo digit", "cat x/git_notes.txt"):
            self.allowed(cmd, "almanac:builder")

    def test_git_commit_is_allowed_for_other_agents_and_without_agent(self):
        self.allowed("git commit -m x", "almanac:handyman")
        self.allowed("git commit -m x", None)

    def test_bad_json_is_allowed(self):
        self.assertEqual(run_raw("builder_git_guard.py", "not json")[0], 0)


if __name__ == "__main__":
    unittest.main()
