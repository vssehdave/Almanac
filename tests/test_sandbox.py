import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from getalmanac import sandbox


def temp() -> Path:
    return Path(tempfile.mkdtemp(prefix="almanac-sandbox-"))


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.dir = temp()
        self.plugin = temp()
        (self.plugin / ".claude-plugin").mkdir()
        (self.plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "pocketbook"}', encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"ALMANAC_SANDBOX": str(self.dir / "sb"), "ANTHROPIC_BASE_URL": "http://x",
                                                "ALMANAC_PROXY": "http://x", "ALMANAC_CLAUDE": "claude-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_parse(self):
        self.assertEqual(sandbox.parse(["--plugin", str(self.plugin), "--", "-p", "hi"]),
                         ([str(self.plugin.resolve())], False, ["-p", "hi"]))
        self.assertEqual(sandbox.parse(["--list"]), ([], True, []))
        with self.assertRaises(ValueError):
            sandbox.parse(["--nope"])
        with self.assertRaises(ValueError):
            sandbox.parse(["--plugin"])

    def test_argv_and_environment(self):
        self.assertEqual(sandbox.command("c", ["/a", "/b"], ["-p", "x"]), ["c", "--plugin-dir", "/a", "--plugin-dir", "/b", "-p", "x"])
        env = sandbox.environment({"PATH": "p", "ANTHROPIC_BASE_URL": "http://x", "ALMANAC_PROXY": "y", "CLAUDE_CONFIG_DIR": "/real"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.dir / "sb"))
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ALMANAC_PROXY", env)
        self.assertEqual(env["PATH"], "p")

    def test_run_creates_the_folder_and_launches_plain_claude(self):
        with mock.patch("getalmanac.sandbox.subprocess.call", return_value=0) as call:
            self.assertEqual(sandbox.run(["--plugin", str(self.plugin), "--", "-p", "hi"]), 0)
        argv, kwargs = call.call_args[0][0], call.call_args[1]
        self.assertEqual(argv, ["claude-test", "--plugin-dir", str(self.plugin.resolve()), "-p", "hi"])
        self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], str(self.dir / "sb"))
        self.assertNotIn("ANTHROPIC_BASE_URL", kwargs["env"])
        self.assertTrue((self.dir / "sb").is_dir())

    def test_list_starts_nothing_and_shows_the_plugins(self):
        with mock.patch("getalmanac.sandbox.subprocess.call") as call, mock.patch("builtins.print") as out:
            self.assertEqual(sandbox.run(["--list", "--plugin", str(self.plugin)]), 0)
        call.assert_not_called()
        shown = "\n".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("pocketbook", shown)
        self.assertIn("/login", shown)
        self.assertFalse((self.dir / "sb").exists())

    def test_a_real_config_folder_is_refused(self):
        with mock.patch.dict(os.environ, {"ALMANAC_SANDBOX": str(Path.home() / ".claude-work")}), \
                mock.patch("getalmanac.sandbox.subprocess.call") as call:
            self.assertEqual(sandbox.run(["--list"]), 2)
        call.assert_not_called()

    def test_a_folder_that_is_not_a_plugin_is_refused(self):
        with mock.patch("getalmanac.sandbox.subprocess.call") as call:
            self.assertEqual(sandbox.run(["--plugin", str(self.dir)]), 2)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
