import json
import os
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sets ALMANAC_HOME for the test run)
from getalmanac import cheatsheet, evaluate


def record(**extra):
    return json.dumps({"kind": "messages", "status": 200, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 100},
                       **extra})


class EvalTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "out.json"
        self.calls = []

    def runner(self, cmd, env, cwd):
        """Stands in for Claude Code: writes the request log a run would leave, and touches nothing outside its folders."""
        home = Path(env["ALMANAC_HOME"])
        cfg = json.loads((home / "config.json").read_text())
        sheet = env.get("ALMANAC_CHEATSHEETS")
        self.calls.append({"env": env, "lean": cfg["startup"]["lean_rules"], "mode": cfg["cheatsheet"]["mode"], "routing": cfg["routing"]["mode"], "sheet": sheet, "cwd": Path(cwd),
                           "model": cmd[cmd.index("--model") + 1], "files": sorted(p.name for p in Path(sheet).iterdir()) if sheet else None})
        (Path(cwd) / "stray.txt").write_text("x")  # must not reach the next run's copy
        (home / "logs").mkdir()
        (home / "logs" / "requests.jsonl").write_text("\n".join([record(tool_calls=2, spawns=1), record(tool_calls=1),
                                                                 record(kind="count_tokens"), json.dumps({"kind": "messages", "status": 500})]))
        return 0

    def test_plan_needs_yes(self):
        self.assertEqual(evaluate.main(["--out", str(self.out)], self.runner), 1)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.out.exists())

    def test_runs_every_arm(self):
        self.assertEqual(evaluate.main(["--yes", "--out", str(self.out), "--model", "m1"], self.runner), 0)
        self.assertEqual([(c["mode"], bool(c["sheet"])) for c in self.calls],
                         [("off", False), ("on", True), ("on", False),  # the arm order rotates per task
                          ("on", True), ("on", False), ("off", False),
                          ("on", False), ("off", False), ("on", True)])
        self.assertEqual([c["lean"] for c in self.calls[:3]], [False, True, True])
        self.assertTrue(all(c["routing"] == "off" and c["model"] == "m1" for c in self.calls))
        self.assertIn("opus.md", self.calls[1]["files"])
        self.assertEqual([c["cwd"].name for c in self.calls[::3]], ["fix_test", "find_def", "refactor"])
        data = json.loads(self.out.read_text())
        self.assertEqual(len(data["runs"]), 9)
        run = data["runs"][0]
        self.assertEqual((run["requests"], run["input_tokens"], run["output_tokens"], run["cache_read_input_tokens"],
                          run["tool_calls"], run["spawns"], run["check"], run["cache_creation_input_tokens"]),
                         (2, 20, 10, 200, 3, 1, False, 14))

    def test_outer_almanac_is_not_inherited(self):
        old = dict(os.environ)
        os.environ.update(ALMANAC_PROXY="http://127.0.0.1:9", ANTHROPIC_BASE_URL="http://127.0.0.1:9")
        try:
            evaluate.main(["--yes", "--variants", "off", "--out", str(self.out)], self.runner)
            os.environ["ANTHROPIC_BASE_URL"] = "http://elsewhere.test"
            evaluate.main(["--yes", "--variants", "off", "--out", str(self.out)], self.runner)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertNotIn("ALMANAC_PROXY", self.calls[0]["env"])
        self.assertNotIn("ANTHROPIC_BASE_URL", self.calls[0]["env"])
        self.assertEqual(self.calls[3]["env"]["ANTHROPIC_BASE_URL"], "http://elsewhere.test")

    def test_repo_needs_tasks_and_plan_writes_nothing(self):
        repo = Path(tempfile.mkdtemp()) / "r"
        self.assertEqual(evaluate.main(["--repo", str(repo), "--yes", "--out", str(self.out)], self.runner), 2)
        self.assertFalse(repo.exists())
        self.assertEqual(self.calls, [])

    def test_env_hook_points_the_sheets_at_a_folder(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "sonnet.md").write_text("mine\n")
        cheatsheet.read.cache_clear()
        os.environ["ALMANAC_CHEATSHEETS"] = str(folder)
        try:
            self.assertEqual(cheatsheet.read("sonnet"), "mine")
        finally:
            del os.environ["ALMANAC_CHEATSHEETS"]
            cheatsheet.read.cache_clear()


if __name__ == "__main__":
    unittest.main()
