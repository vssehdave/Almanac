import json
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
        (self.plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "demo"}', encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"ALMANAC_SANDBOX": str(self.dir / "sb"), "ANTHROPIC_BASE_URL": "http://x",
                                                "ALMANAC_PROXY": "http://x", "ALMANAC_CLAUDE": "claude-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_parse(self):
        self.assertEqual(sandbox.parse(["--plugin", str(self.plugin), "--", "-p", "hi"]),
                         ([str(self.plugin)], False, ["-p", "hi"], {}))
        self.assertEqual(sandbox.parse(["--list"]), ([], True, [], {}))
        self.assertEqual(sandbox.parse(["--env", "A=b=c"])[3], {"A": "b=c"})
        for bad in (["--env", "NOEQ"], ["--env", "1x=y"], ["--env"]):
            with self.assertRaises(ValueError):
                sandbox.parse(bad)
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
        self.assertIn("demo", shown)
        self.assertIn("/login", shown)
        self.assertFalse((self.dir / "sb").exists())

    def test_env_is_passed_in(self):
        env = sandbox.environment({"A": "1"}, extra={"B": "2"})
        self.assertEqual((env["A"], env["B"]), ("1", "2"))

    def test_plugin_by_name_uses_the_real_installed_list(self):
        real = self.dir / "real"
        (real / "plugins").mkdir(parents=True)
        data = {"version": 2, "plugins": {
            "demo@m1": [{"scope": "project", "installPath": "/p"}, {"scope": "user", "installPath": str(self.plugin)}],
            "other@m1": [{"scope": "user", "installPath": "/o"}],
            "other@m2": [{"scope": "user", "installPath": "/o2"}]}}
        (real / "plugins" / "installed_plugins.json").write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(real)}):
            self.assertEqual(sandbox.resolve(["demo"]), [(str(self.plugin.resolve()), False)])
            self.assertEqual(sandbox.resolve(["other@m2"]), [(str(Path("/o2").resolve()), False)])
            self.assertEqual(sandbox.resolve([str(self.plugin)]), [(str(self.plugin.resolve()), True)])
            for bad in ("other", "nope"):
                with self.assertRaises(ValueError):
                    sandbox.resolve([bad])
            self.assertEqual(sandbox.run(["--plugin", "nope", "--list"]), 2)

    def test_plugin_source_root_leads_pythonpath(self):
        plugin = self.dir / "wt" / "src" / "pkg" / "plugin"
        plugin.mkdir(parents=True)
        env = sandbox.environment({"PYTHONPATH": "old"}, [str(plugin), str(self.plugin)])
        self.assertEqual(env["PYTHONPATH"], os.pathsep.join([str(self.dir / "wt" / "src"), "old"]))
        self.assertNotIn("PYTHONPATH", sandbox.environment({}, [str(self.plugin)]))

    def test_no_terminal_opens_a_window_on_windows_and_refuses_elsewhere(self):
        self.assertTrue(self._needs(False, []))
        self.assertFalse(self._needs(True, []))
        self.assertFalse(self._needs(False, ["--print", "hi"]))
        with mock.patch("getalmanac.sandbox.needs_window", return_value=True),                 mock.patch("getalmanac.sandbox.subprocess.call") as call:
            with mock.patch("getalmanac.sandbox.sys.platform", "linux"):
                self.assertEqual(sandbox.run([]), 2)
            call.assert_not_called()
            with mock.patch("getalmanac.sandbox.sys.platform", "win32"),                     mock.patch("getalmanac.sandbox.subprocess.Popen") as popen,                     mock.patch("getalmanac.sandbox.subprocess.CREATE_NEW_CONSOLE", 16, create=True):
                self.assertEqual(sandbox.run([]), 0)
            self.assertEqual(popen.call_args[1]["creationflags"], 16)

    def _needs(self, tty, args):
        with mock.patch("getalmanac.sandbox.sys.stdin.isatty", return_value=tty, create=True),                 mock.patch("getalmanac.sandbox.sys.stdout.isatty", return_value=tty, create=True):
            return sandbox.needs_window(args)

    def test_a_real_config_folder_is_refused(self):
        with mock.patch.dict(os.environ, {"ALMANAC_SANDBOX": str(Path.home() / ".claude")}), \
                mock.patch("getalmanac.sandbox.subprocess.call") as call:
            self.assertEqual(sandbox.run(["--list"]), 2)
        call.assert_not_called()

    def test_a_folder_that_is_not_a_plugin_is_refused(self):
        with mock.patch("getalmanac.sandbox.subprocess.call") as call:
            self.assertEqual(sandbox.run(["--plugin", str(self.dir)]), 2)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
