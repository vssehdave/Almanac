import json
import os
import subprocess
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "src", "getalmanac", "bundle", "guard", "scripts")

NO_GIT = "No git commands: do not commit, stage or reset."
REPORT = "Your last message must be the report."
TEST = "Run: PYTHONPATH=src python -m unittest discover -s tests -p test_x.py"
TEST_QUOTED = 'Run: PYTHONPATH=src python -m unittest discover -s tests -p "test_x.py"'
TEST_PYTEST = "Run: pytest tests/test_x.py"
PARTS = {"no git": NO_GIT, "last message": REPORT, "test command": TEST}


def run(script, tool, **tool_input):
    return run_raw(script, json.dumps({"tool_name": tool, "tool_input": tool_input}))


def run_raw(script, text):
    done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], input=text, capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


class BriefGuard(unittest.TestCase):
    def brief(self, *keep):
        return " ".join(PARTS[name] for name in PARTS if name in keep)

    def blocked(self, tool, subagent_type, prompt, named):
        code, _, err = run("brief_guard.py", tool, subagent_type=subagent_type, prompt=prompt)
        self.assertEqual(code, 2, prompt)
        self.assertTrue(err.startswith("Blocked: this builder brief is missing:"), err)
        for name in named:
            self.assertIn(name, err)
        for name in set(PARTS) - set(named):
            self.assertNotIn(name, err)

    def allowed(self, tool, subagent_type, prompt):
        self.assertEqual(run("brief_guard.py", tool, subagent_type=subagent_type, prompt=prompt)[0], 0, prompt)

    def allowed_without_ask(self, prompt):
        code, out, _ = run("brief_guard.py", "Agent", subagent_type="almanac:builder", prompt=prompt)
        self.assertEqual(code, 0, prompt)
        self.assertNotIn('"permissionDecision": "ask"', out)

    def asked(self, prompt):
        code, out, _ = run("brief_guard.py", "Agent", subagent_type="almanac:builder", prompt=prompt)
        self.assertEqual(code, 0, prompt)
        self.assertIn('"permissionDecision": "ask"', out)

    def test_full_brief_is_allowed(self):
        self.allowed("Agent", "almanac:builder", self.brief("no git", "last message", "test command"))

    def test_unittest_module_form_is_allowed(self):
        prompt = " ".join([NO_GIT, REPORT, "Run: python -m unittest tests.test_x"])
        self.allowed("Agent", "almanac:builder", prompt)

    def test_quoted_pattern_form_is_allowed(self):
        self.allowed_without_ask(" ".join([NO_GIT, REPORT, TEST_QUOTED]))

    def test_pytest_path_form_is_allowed(self):
        self.allowed_without_ask(" ".join([NO_GIT, REPORT, TEST_PYTEST]))

    def test_missing_test_command_asks(self):
        self.asked(self.brief("no git", "last message"))

    def test_each_missing_part_is_blocked_and_named(self):
        for missing in ["no git", "last message"]:
            with self.subTest(missing=missing):
                keep = [name for name in PARTS if name != missing]
                self.blocked("Agent", "almanac:builder", self.brief(*keep), [missing])

    def test_empty_prompt_names_all_three(self):
        self.blocked("Agent", "almanac:builder", "", list(PARTS))

    def test_other_subagent_types_are_not_checked(self):
        self.allowed("Agent", "almanac:surveyor", "")

    def test_task_tool_is_checked_the_same(self):
        self.blocked("Task", "almanac:builder", "", list(PARTS))
        self.allowed("Task", "almanac:surveyor", "")

    def test_bad_json_is_allowed(self):
        self.assertEqual(run_raw("brief_guard.py", "not json")[0], 0)


if __name__ == "__main__":
    unittest.main()
