import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "egm" / "scripts" / "plan_review.py"


def run(stdin, data):
    env = dict(os.environ, CLAUDE_PLUGIN_DATA=data)
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True, env=env)


def payload(plan):
    return json.dumps({"tool_name": "ExitPlanMode", "tool_input": {"plan": plan}})


class PlanReviewHook(unittest.TestCase):
    def test_blocks_once_per_plan_then_allows(self):
        with tempfile.TemporaryDirectory() as data:
            first = run(payload("plan A"), data)
            self.assertEqual(first.returncode, 2)
            self.assertIn("egm-review", first.stderr)
            self.assertEqual(run(payload("plan A"), data).returncode, 0)
            self.assertEqual(run(payload("plan B"), data).returncode, 2)

    def test_fails_open(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(run("not json", data).returncode, 0)
            self.assertEqual(run(json.dumps({"tool_input": {}}), data).returncode, 0)
            blocker = os.path.join(data, "file")
            Path(blocker).write_text("")  # the data dir is a file, so the mark cannot be written
            self.assertEqual(run(payload("plan C"), blocker).returncode, 0)

    def test_hooks_json_points_at_the_script(self):
        hooks = json.loads((SCRIPT.parent.parent / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(hooks[0]["matcher"], "ExitPlanMode")
        self.assertIn("scripts/plan_review.py", hooks[0]["hooks"][0]["command"])
