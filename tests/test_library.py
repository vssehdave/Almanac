import json
import types
import unittest

import test_memory as tm

from getalmanac import library
from getalmanac.memory import MemoryLayer


def use(n, name, **args):
    return {"role": "assistant", "content": [{"type": "text", "text": f"step {n}"},
                                             {"type": "tool_use", "id": f"u{n}", "name": name, "input": args}]}


def out(n, text="ok", error=False):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{n}", "content": text, "is_error": error}]}


class LibraryCase(tm.Case):
    def setUp(self):
        super().setUp()
        self.key = self.memory.projects.see(self.alpha)
        self.layer = MemoryLayer(self.memory)
        self.n = 0

    def finish(self, prompt, calls, lane="lane-1", agent="", answer="Done: it works now.", **cfg):
        """Run one finished exchange through the layer's observe, the way the worker does."""
        messages = [tm.human(prompt)]
        for i, (name, args, text, error) in enumerate(calls, 1):
            messages += [use(i, name, **args), out(i, text, error)]
        ctx = types.SimpleNamespace(cfg={"memory": {"enabled": True}, "library": cfg}, lane=lane, session="s", project=self.key,
                                    agent=agent, proxy=types.SimpleNamespace(log=types.SimpleNamespace(write=print)))
        body = {"messages": messages, "tools": [{"name": "Bash", "input_schema": {}}]}
        self.layer.observe(ctx, body, tm.reply(answer))

    def rows(self):
        return [dict(r) for r in self.store.run("select * from library order by id")]

    def calls(self, *extra):
        a = self.alpha
        return [("Bash", {"command": "git checkout -b feat/x"}, "ok", False),
                ("Read", {"file_path": a + "/src/proxy.py"}, "code", False),
                ("Bash", {"command": "python -m pytest tests/test_x.py"}, "Exit code 1\nImportError: no module named x\nFAILED", True),
                ("Edit", {"file_path": a + "/src/proxy.py", "old_string": "a", "new_string": "b"}, "edited", False),
                ("Bash", {"command": "python -m pytest tests/test_x.py"}, "2 passed", False), *extra]


class WriterTest(LibraryCase):
    def test_failing_pytest_then_edit_then_passing_pytest_gives_one_fix(self):
        self.finish("Make the x test pass", self.calls())
        (row,) = self.rows()
        fixes = json.loads(row["fixes"])
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0], "Bash: python -m pytest failed (ImportError: no module named x) -> "
                                   "Edit src/proxy.py -> Bash: python -m pytest passed")
        self.assertEqual(json.loads(row["route"]), ["Bash: git checkout", "Read", "Bash: python -m pytest", "Edit",
                                                    "Bash: python -m pytest"])
        self.assertEqual(json.loads(row["files"]), ["src/proxy.py"])
        self.assertTrue(row["outcome"].startswith("done: Done: it works"))
        self.assertEqual((row["project"], row["seq"]), (self.key, 0))

    def test_fewer_tool_calls_than_min_tools_writes_nothing(self):
        self.finish("Look at the proxy", self.calls()[:4])
        self.assertEqual(self.rows(), [])
        self.finish("Look at the proxy again", self.calls()[:4], min_tools=4, lane="lane-2")
        self.assertEqual(len(self.rows()), 1)

    def test_a_sub_agent_exchange_writes_nothing(self):
        self.finish("Make the x test pass", self.calls(), agent="agent-1")
        self.assertEqual(self.rows(), [])

    def test_a_reminder_tag_in_the_prompt_is_stripped_and_secrets_are_redacted(self):
        self.finish("fix the build </system-reminder> then use ghp_abcdefghijklmnopqrstuvwxyz0123", self.calls())
        (row,) = self.rows()
        self.assertNotIn("system-reminder", row["prompt"])
        self.assertNotIn("ghp_", row["prompt"])
        self.assertIn("fix the build", row["prompt"])


class ForgetTest(LibraryCase):
    def test_forget_and_prune_delete_the_rows(self):
        self.finish("Make the x test pass in the proxy", self.calls())
        self.finish("Rewrite the readme introduction completely", self.calls(), lane="lane-2")
        self.assertEqual(len(self.rows()), 2)
        self.store.run("update convo set updated = updated - 7200")
        self.assertEqual(self.memory.forget(project=self.key), 2)
        self.assertEqual(self.rows(), [])
        self.finish("Another task entirely about caching", self.calls(), lane="lane-3")
        self.store.run("update convo set updated = updated - 400 * 86400")
        self.memory.prune(365)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
