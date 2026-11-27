import json
import os
import subprocess
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "src", "getalmanac", "bundle", "guard", "scripts")


def run_raw(script, text):
    done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], input=text, capture_output=True, text=True)
    return done.returncode, done.stderr


def run(command):
    return run_raw("suite_guard.py", json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))


class SuiteGuard(unittest.TestCase):
    def test_whole_suite_runs_are_blocked(self):
        for command in ("python -m unittest discover -s tests", "cd x && python -m unittest discover",
                        "python -m unittest", "python -m unittest -v", "pytest", "pytest -q", "python -m pytest",
                        "rtk python -m unittest discover -s tests"):
            with self.subTest(command=command):
                code, err = run(command)
                self.assertEqual(code, 2, command)
                self.assertIn("full test suite runs in CI", err)

    def test_targeted_runs_are_allowed(self):
        for command in ("PYTHONPATH=src python -m unittest discover -s tests -p test_x.py",
                        "python -m unittest tests.test_x", "pytest tests/test_x.py -k foo", "git status"):
            with self.subTest(command=command):
                self.assertEqual(run(command)[0], 0, command)

    def test_bad_input_allows(self):
        self.assertEqual(run_raw("suite_guard.py", "not json")[0], 0)


if __name__ == "__main__":
    unittest.main()
