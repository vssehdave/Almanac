import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from getalmanac import config, profile

CFG = copy.deepcopy(config.DEFAULTS)
CFG["memory"]["review_gate"] = False  # these tests are about the user's own hooks
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

    def test_the_review_gate_is_added_as_a_stop_hook(self):
        cfg = copy.deepcopy(CFG)
        cfg["memory"]["review_gate"] = True
        stop = profile.curated_settings(USER, cfg)["hooks"]["Stop"]
        self.assertEqual(len(stop), 1)  # the user's Logbook Stop hook is still left out
        self.assertTrue(stop[0]["hooks"][0]["command"].endswith("-m getalmanac review-gate"))
        cfg["memory"]["enabled"] = False
        self.assertNotIn("hooks", profile.curated_settings({"model": "x"}, cfg))
        cfg["memory"].update(enabled=True, tools=False)  # the verdict is filed by a memory tool: no tool, no gate
        self.assertNotIn("hooks", profile.curated_settings({"model": "x"}, cfg))

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

    def prep(self, args, cwd=None):
        flags, notes = profile.prepare(CFG, args, cwd=cwd or self.home)
        self.dir = Path(flags[1]) if flags else None  # the folder this run was given with --add-dir
        return flags, notes

    def test_the_profile_folder_is_trusted_once_and_nothing_else_changes(self):
        state = self.user / ".claude.json"
        other = {"numStartups": 7, "projects": {"C:/elsewhere": {"hasTrustDialogAccepted": False, "x": 1}}}
        state.write_text(json.dumps(other), encoding="utf-8")
        self.prep(["-p", "hi"])
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["projects"][self.dir.resolve().as_posix()], {"hasTrustDialogAccepted": True})
        self.assertEqual(data["projects"]["C:/elsewhere"], other["projects"]["C:/elsewhere"])
        self.assertEqual(data["numStartups"], 7)
        before = state.stat().st_mtime_ns
        self.prep(["-p", "hi"])
        self.assertEqual(state.stat().st_mtime_ns, before)  # already trusted: the file is not written again

    def test_no_state_file_is_left_alone(self):
        self.prep(["-p", "hi"])
        self.assertFalse((self.user / ".claude.json").exists())

    def test_flags_and_files(self):
        flags, notes = self.prep(["-p", "hi"], self.home)
        self.assertEqual(notes, [])
        self.assertEqual(flags[0], "--add-dir")
        self.assertEqual(flags[2:4], ["--setting-sources", "project,local"])
        self.assertEqual(flags[flags.index("--settings") + 1], str(self.dir / "settings.json"))
        self.assertTrue(flags[1].startswith(str(self.home)) and flags[2].startswith("--"))  # --add-dir is not the last flag
        written = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
        self.assertFalse(written["enabledPlugins"]["claude-mem@thedotmack"])
        self.assertEqual(written["claudeMdExcludes"], [(self.user / "CLAUDE.md").as_posix()])

    def test_the_users_rules_are_a_claude_md_of_the_profile_folder_so_sub_agents_get_them_too(self):
        flags, _ = self.prep([])
        self.assertNotIn("--append-system-prompt-file", flags)  # a system prompt addition reaches the main thread only
        self.assertIn("rules", (self.dir / "CLAUDE.md").read_text(encoding="utf-8"))
        written = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(written["env"]["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")  # Claude Code reads it from an added folder
        self.assertEqual(written["claudeMdExcludes"], [(self.user / "CLAUDE.md").as_posix()])  # the unfiltered copy stays out

    def test_a_removed_claude_md_leaves_no_old_copy_behind(self):
        self.prep([])
        (self.user / "CLAUDE.md").unlink()
        self.prep([])
        self.assertFalse((self.dir / "CLAUDE.md").exists())
        written = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("claudeMdExcludes", written)
        self.assertNotIn("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", written.get("env", {}))

    def test_the_users_own_env_settings_are_kept(self):
        (self.user / "settings.json").write_text(json.dumps({**USER, "env": {"MINE": "1"}}), encoding="utf-8")
        self.prep([])
        written = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(written["env"]["MINE"], "1")

    def test_skills_and_agents_come_along_except_the_off_skills(self):
        self.prep([], self.home)
        skills = sorted(p.name for p in (self.dir / ".claude" / "skills").iterdir())
        self.assertEqual(skills, ["council", "keep"])
        self.assertTrue((self.dir / ".claude" / "skills" / "keep" / "SKILL.md").exists())
        self.assertEqual([p.name for p in (self.dir / ".claude" / "agents").iterdir()], ["editor.md"])
        (self.user / "skills" / "new").mkdir()
        (self.user / "skills" / "new" / "SKILL.md").write_text("n", encoding="utf-8")
        self.prep([], self.home)  # a second launch picks changes up and leaves the originals alone
        self.assertEqual(sorted(p.name for p in (self.dir / ".claude" / "skills").iterdir()), ["council", "keep", "new"])
        self.assertTrue((self.user / "skills" / "keep" / "SKILL.md").exists())

    def test_each_config_folder_has_its_own_profile_folder_and_a_launch_leaves_the_others_alone(self):
        self.prep([])
        mine = self.dir
        other = temp()  # a second account running at the same time
        (other / "settings.json").write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.prep([])
        self.assertNotEqual(self.dir, mine)
        self.assertEqual(sorted(p.name for p in (mine / ".claude" / "skills").iterdir()), ["council", "keep"])
        self.assertIn("rules", (mine / "CLAUDE.md").read_text(encoding="utf-8"))
        self.prep([])
        self.assertEqual(self.dir, mine)  # the same account always gets the same folder

    def test_a_second_launch_with_nothing_changed_replaces_nothing(self):
        self.prep([])
        for name in ("settings.json", "CLAUDE.md"):  # an old time shows a rewrite
            os.utime(self.dir / name, (1_000_000_000, 1_000_000_000))
        with mock.patch.object(profile, "_remove") as remove, mock.patch.object(profile, "_link") as link:
            self.prep([])
        remove.assert_not_called()
        link.assert_not_called()
        for name in ("settings.json", "CLAUDE.md"):
            self.assertEqual((self.dir / name).stat().st_mtime, 1_000_000_000)

    def test_a_changed_or_removed_item_is_brought_up_to_date(self):
        self.prep([])
        (self.user / "agents" / "editor.md").write_text("agent v2", encoding="utf-8")
        shutil.rmtree(self.user / "skills" / "keep")
        self.prep([])
        self.assertEqual((self.dir / ".claude" / "agents" / "editor.md").read_text(encoding="utf-8"), "agent v2")
        self.assertEqual([p.name for p in (self.dir / ".claude" / "skills").iterdir()], ["council"])

    def test_a_write_goes_through_a_temp_file_of_its_own(self):
        target = self.home / "x.txt"
        with mock.patch("os.getpid", return_value=4242), mock.patch("os.replace", wraps=os.replace) as replace:
            profile._put(target, "text")
        self.assertIn("4242", replace.call_args[0][0].name)  # two launches never share a temp name
        self.assertEqual(target.read_text(encoding="utf-8"), "text")

    def test_a_lost_replace_race_is_not_an_error_but_a_real_failure_is(self):
        target = self.home / "y.txt"

        def lost(src, dst):
            Path(dst).write_text("text", encoding="utf-8")  # the other launch wrote the same text first
            raise OSError("held")

        with mock.patch("os.replace", side_effect=lost):
            profile._put(target, "text")
        self.assertEqual([p.name for p in self.home.glob("y.txt*")], ["y.txt"])  # and no temp file is left
        with mock.patch("os.replace", side_effect=OSError("held")):
            with self.assertRaises(OSError):
                profile._put(target, "other")

    def test_a_link_another_launch_made_first_is_accepted(self):
        self.prep([])
        shutil.rmtree(self.dir / ".claude")
        real = profile._link

        def late(src, dst):
            real(src, dst)
            raise FileExistsError(str(dst))  # the other launch got there between our check and our link

        with mock.patch.object(profile, "_link", side_effect=late):
            self.prep([])
        self.assertTrue((self.dir / ".claude" / "skills" / "keep" / "SKILL.md").exists())

    def test_a_copied_skill_is_current_and_is_not_copied_again(self):
        # where no link can be made, the profile holds copies: an unchanged one must stay put
        def no_links(f):
            with mock.patch("os.symlink", side_effect=OSError), mock.patch("_winapi.CreateJunction", side_effect=OSError, create=True):
                return f()

        no_links(lambda: self.prep([]))
        kept = self.dir / ".claude" / "skills" / "keep"
        self.assertFalse(kept.is_symlink())
        with mock.patch.object(profile, "_remove") as remove, mock.patch("shutil.copytree") as copytree:
            no_links(lambda: self.prep([]))
        remove.assert_not_called()
        copytree.assert_not_called()
        (self.user / "skills" / "keep" / "SKILL.md").write_text("changed", encoding="utf-8")
        no_links(lambda: self.prep([]))
        self.assertEqual((kept / "SKILL.md").read_text(encoding="utf-8"), "changed")

    def test_off_for_the_run_when_the_user_sets_their_own_settings(self):
        for flag in (["--settings", "x.json"], ["--setting-sources=user"], ["--bare"]):
            flags, notes = self.prep(flag, self.home)
            self.assertEqual(flags, [])
            self.assertEqual(len(notes), 1)

    def test_turned_off_in_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["profile"]["enabled"] = False
        self.assertEqual(profile.prepare(cfg, [], cwd=self.home), ([], []))

    def test_an_unreadable_settings_file_leaves_the_profile_off_and_says_so(self):
        (self.user / "settings.json").write_text("{not json", encoding="utf-8")
        flags, notes = self.prep([], self.home)
        self.assertEqual(flags, [])
        self.assertIn("was not read", notes[0])

    def test_a_repository_hook_is_reported(self):
        repo = temp()
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text(json.dumps(USER), encoding="utf-8")
        _, notes = self.prep([], repo)
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
