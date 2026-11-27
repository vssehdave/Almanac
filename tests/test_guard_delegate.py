import json
import os
import subprocess
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "src", "getalmanac", "bundle", "guard", "scripts")


def run(tool, agent_type=None, **tool_input):
    payload = {"tool_name": tool, "tool_input": tool_input}
    if agent_type:
        payload["agent_type"] = agent_type
    return run_raw(json.dumps(payload))


def run_raw(text):
    done = subprocess.run([sys.executable, os.path.join(SCRIPTS, "delegate_guard.py")], input=text, capture_output=True, text=True)
    return done.returncode, done.stderr


class DelegateGuard(unittest.TestCase):
    def test_main_thread_write_and_notebook_edit_on_code_are_blocked(self):
        for tool, args in (("Write", {"file_path": "app.py", "content": "x = 1\n"}),
                           ("NotebookEdit", {"notebook_path": "nb.ipynb", "new_source": "x = 1"})):
            code, err = run(tool, **args)
            self.assertEqual(code, 2, tool)
            if tool == "Write":
                self.assertIn("almanac:builder", err)

    def test_main_thread_edit_spanning_lines_is_blocked(self):
        code, err = run("Edit", file_path="app.ts", old_string="a\nb", new_string="c")
        self.assertEqual(code, 2)
        self.assertIn("almanac:builder", err)
        self.assertEqual(run("Edit", file_path="app.ts", old_string="\na\n", new_string="\nb\nc\n")[0], 2)

    def test_main_thread_one_line_edit_is_allowed(self):
        self.assertEqual(run("Edit", file_path="app.py", old_string="x = 1", new_string="x = 2")[0], 0)

    def test_multi_edit_with_two_edits_is_blocked_and_one_is_allowed(self):
        one = [{"old_string": "x = 1", "new_string": "x = 2"}]
        two = one + [{"old_string": "y = 1", "new_string": "y = 2"}]
        self.assertEqual(run("MultiEdit", file_path="app.py", edits=one)[0], 0)
        self.assertEqual(run("MultiEdit", file_path="app.py", edits=two)[0], 2)

    def test_builder_and_non_code_files_are_allowed(self):
        self.assertEqual(run("Write", "almanac:builder", file_path="app.py", content="x = 1\n")[0], 0)
        self.assertEqual(run("Write", file_path="README.md", content="# hi\n")[0], 0)
        self.assertEqual(run("Edit", file_path="notes.txt", old_string="a\nb", new_string="c")[0], 0)

    def test_bad_input_is_allowed(self):
        self.assertEqual(run_raw("not json")[0], 0)


if __name__ == "__main__":
    unittest.main()
