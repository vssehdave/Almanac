import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from getalmanac import config, profile

CFG = copy.deepcopy(config.DEFAULTS)
USER = {
    "model": "opus",
    "enabledPlugins": {"keeper@market": True, "claude-mem@thedotmack": True, "logbook@logbook": True},
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "node sync.mjs"}]},
                         {"hooks": [{"type": "command", "command": "python -m getlogbook capture --event SessionStart"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk.exe hook claude"}]},
                       {"matcher": "Bash", "hooks": [{"type": "command", "command": "python -m getlogbook capture --event Permission"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "python -m getlogbook capture --event Stop"}]}],
    },
}


def temp() -> Path:
    return Path(tempfile.mkdtemp(prefix="almanac-profile-"))


class SettingsTest(unittest.TestCase):
    def test_off_plugins_are_false_and_the_rest_is_kept(self):
        out = profile.curated_settings(USER, CFG)
        self.assertEqual(out["enabledPlugins"], {"keeper@market": True, "claude-mem@thedotmack": False,
                                                 "logbook@logbook": False, "pocketbook@pocketbook": False})
        self.assertEqual(out["model"], "opus")
        self.assertEqual(out["skillOverrides"], {"logbook": "off"})

    def test_matching_hooks_go_and_the_others_stay(self):
        hooks = profile.curated_settings(USER, CFG)["hooks"]
        self.assertEqual(sorted(hooks), ["PreToolUse", "SessionStart"])  # Stop only had a Logbook hook
        commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        self.assertEqual(commands, ["node sync.mjs", "rtk.exe hook claude"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")

    def test_the_input_is_not_changed_and_the_user_markdown_is_excluded(self):
        before = json.dumps(USER)
        out = profile.curated_settings(USER, CFG, "C:/u/.claude/CLAUDE.md")
        self.assertEqual(json.dumps(USER), before)
        self.assertEqual(out["claudeMdExcludes"], ["C:/u/.claude/CLAUDE.md"])

    def test_a_setting_without_hooks_gets_none(self):
        self.assertNotIn("hooks", profile.curated_settings({"model": "x"}, CFG))

    def test_custom_patterns(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["off_hooks"] = ["rtk"]
        commands = [h["command"] for groups in profile.curated_settings(USER, cfg)["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertNotIn("rtk.exe hook claude", commands)
        self.assertEqual(commands, ["node sync.mjs"])  # the names in off_plugins and off_skills still match "getlogbook"


class OneLineTest(unittest.TestCase):
    def test_adding_a_plugin_to_the_one_list_covers_its_hooks_tools_and_listing(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["off_plugins"].append("newdev@mine")
        user = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "node C:/code/NewDev/hook.js"},
                                              {"type": "command", "command": "node keep.js"}]}]}}
        commands = [h["command"] for g in profile.curated_settings(user, cfg)["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(commands, ["node keep.js"])
        self.assertFalse(profile.curated_settings(user, cfg)["enabledPlugins"]["newdev@mine"])
        self.assertIn("newdev", profile.off_names(cfg))


class ClaudeMdTest(unittest.TestCase):
    def test_imports_are_inlined_and_off_sections_dropped(self):
        folder = temp()
        (folder / "RTK.md").write_text("rtk text\n", encoding="utf-8")
        (folder / "CLAUDE.md").write_text("@RTK.md\n\n# Notes\n\nkeep\n\n<!-- logbook:start -->\n## Logbook (shared memory)\n\ngone\n\n### Sub\n\ngone too\n\n# After\n\nback\n",
                                          encoding="utf-8")
        text = profile.claude_md(folder, CFG)
        self.assertIn("rtk text", text)
        self.assertIn("keep", text)
        self.assertIn("# After\n\nback", text)
        self.assertNotIn("gone", text)
        self.assertNotIn("Logbook", text)
        self.assertNotIn("<!--", text)

    def test_no_file_no_text(self):
        self.assertEqual(profile.claude_md(temp(), CFG), "")


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.user, self.home = temp(), temp()
        (self.user / "settings.json").write_text(json.dumps(USER), encoding="utf-8")
        (self.user / "CLAUDE.md").write_text("# Mine\n\nrules\n", encoding="utf-8")
        for name in ("council", "logbook", "keep"):
            (self.user / "skills" / name).mkdir(parents=True)
            (self.user / "skills" / name / "SKILL.md").write_text(name, encoding="utf-8")
        (self.user / "agents").mkdir()
        (self.user / "agents" / "editor.md").write_text("agent", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.user), "ALMANAC_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_flags_and_files(self):
        flags, notes = profile.prepare(CFG, ["-p", "hi"], cwd=self.home)
        self.assertEqual(notes, [])
        self.assertEqual(flags[0], "--add-dir")
        self.assertEqual(flags[2:4], ["--setting-sources", "project,local"])
        self.assertEqual(flags[flags.index("--settings") + 1], str(self.home / "profile" / "settings.json"))
        self.assertIn("--append-system-prompt-file", flags)
        self.assertTrue(flags[1].startswith(str(self.home)) and flags[2].startswith("--"))  # --add-dir is not the last flag
        written = json.loads((self.home / "profile" / "settings.json").read_text(encoding="utf-8"))
        self.assertFalse(written["enabledPlugins"]["claude-mem@thedotmack"])
        self.assertEqual(written["claudeMdExcludes"], [(self.user / "CLAUDE.md").as_posix()])
        self.assertIn("rules", (self.home / "profile" / "user-claude-md.md").read_text(encoding="utf-8"))

    def test_skills_and_agents_come_along_except_the_off_skills(self):
        profile.prepare(CFG, [], cwd=self.home)
        skills = sorted(p.name for p in (self.home / "profile" / ".claude" / "skills").iterdir())
        self.assertEqual(skills, ["council", "keep"])
        self.assertTrue((self.home / "profile" / ".claude" / "skills" / "keep" / "SKILL.md").exists())
        self.assertEqual([p.name for p in (self.home / "profile" / ".claude" / "agents").iterdir()], ["editor.md"])
        (self.user / "skills" / "new").mkdir()
        (self.user / "skills" / "new" / "SKILL.md").write_text("n", encoding="utf-8")
        profile.prepare(CFG, [], cwd=self.home)  # a second launch picks changes up and leaves the originals alone
        self.assertEqual(sorted(p.name for p in (self.home / "profile" / ".claude" / "skills").iterdir()), ["council", "keep", "new"])
        self.assertTrue((self.user / "skills" / "keep" / "SKILL.md").exists())

    def test_off_for_the_run_when_the_user_sets_their_own_settings(self):
        for flag in (["--settings", "x.json"], ["--setting-sources=user"], ["--bare"]):
            flags, notes = profile.prepare(CFG, flag, cwd=self.home)
            self.assertEqual(flags, [])
            self.assertEqual(len(notes), 1)

    def test_turned_off_in_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["enabled"] = False
        self.assertEqual(profile.prepare(cfg, [], cwd=self.home), ([], []))

    def test_an_unreadable_settings_file_leaves_the_profile_off_and_says_so(self):
        (self.user / "settings.json").write_text("{not json", encoding="utf-8")
        flags, notes = profile.prepare(CFG, [], cwd=self.home)
        self.assertEqual(flags, [])
        self.assertIn("was not read", notes[0])

    def test_a_repository_hook_is_reported(self):
        repo = temp()
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text(json.dumps(USER), encoding="utf-8")
        _, notes = profile.prepare(CFG, [], cwd=repo)
        self.assertEqual(len(notes), 1)
        self.assertIn("settings.local.json", notes[0])


class LauncherTest(unittest.TestCase):
    def test_the_profile_flags_come_before_the_users_arguments(self):
        from getalmanac import launcher

        with mock.patch.object(profile, "prepare", return_value=(["--setting-sources", "project,local"], ["a note"])):
            self.assertEqual(launcher.with_profile(["-p", "hi"], CFG), ["--setting-sources", "project,local", "-p", "hi"])

    def test_a_failing_profile_does_not_stop_claude(self):
        from getalmanac import launcher

        with mock.patch.object(profile, "prepare", side_effect=RuntimeError("boom")):
            self.assertEqual(launcher.with_profile(["-p", "hi"], CFG), ["-p", "hi"])


if __name__ == "__main__":
    unittest.main()
