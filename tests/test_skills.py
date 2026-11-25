"""The scan gate, with a fake scanner: no test needs the network or SkillSpector."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sets a temporary ALMANAC_HOME)

from getalmanac import cli, skills

# Reads verdict.txt in the scanned folder: "SAFE 5", "CAUTION 30", "DO_NOT_INSTALL 90", "ERROR" or "JUNK".
FAKE = r'''
import json, os, sys
target = sys.argv[2]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
spec = "SAFE 5"
if os.path.isfile(os.path.join(target, "verdict.txt")):
    spec = open(os.path.join(target, "verdict.txt")).read().strip()
if spec == "ERROR":
    sys.stderr.write("Error: boom\n"); sys.exit(2)
if spec == "JUNK":
    print("not json"); sys.exit(0)
verdict, score = spec.split()
issue = {"severity": "HIGH", "category": "Supply Chain", "location": {"file": "run.sh", "start_line": 3}}
print(json.dumps({"risk_assessment": {"score": int(score), "recommendation": verdict},
                  "issues": [] if verdict == "SAFE" else [issue, issue],
                  "execution_successful": True, "analysis_completeness": {"is_complete": True}}))
sys.exit(0 if int(score) <= 50 else 1)
'''


def make_skill(root, name, verdict=None, extra=None):
    folder = Path(root) / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nhello\n")
    if verdict:
        (folder / "verdict.txt").write_text(verdict)
    for rel, text in (extra or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    return folder


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="almanac-skill-"))
        fake = self.tmp / "fake_scanner.py"
        fake.write_text(FAKE)
        self.log = self.tmp / "calls.log"
        self.claude = self.tmp / "claude"
        self.env = mock.patch.dict(os.environ, {"ALMANAC_SKILLSPECTOR": json.dumps([sys.executable, str(fake)]),
                                                "FAKE_LOG": str(self.log), "CLAUDE_CONFIG_DIR": str(self.claude),
                                                "ALMANAC_HOME": str(self.tmp / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.src = self.tmp / "src"

    def add(self, *args, **kw):
        lines = []
        rc = skills.add(*args, out=lines.append, **kw)
        return rc, "\n".join(lines)

    def installed(self):
        root = self.claude / "skills"
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def quarantine_empty(self):
        box = self.tmp / "home" / "quarantine"
        return not box.exists() or not any(box.iterdir())


class Verdicts(Base):
    def test_parse_maps_report_not_exit_code(self):
        report = lambda v, s: json.dumps({"risk_assessment": {"score": s, "recommendation": v}, "issues": []}).encode()
        self.assertEqual(skills.code(skills.parse(0, report("SAFE", 3), b"")), 0)
        self.assertEqual(skills.code(skills.parse(0, report("CAUTION", 40), b"")), 1)
        self.assertEqual(skills.code(skills.parse(1, report("DO_NOT_INSTALL", 90), b"")), 2)
        self.assertEqual(skills.code(skills.parse(0, report("LOOKS_FINE", 0), b"")), 3)
        self.assertEqual(skills.code(skills.parse(2, b"", b"Error: x")), 3)
        self.assertEqual(skills.code(skills.parse(0, b"{}", b"")), 3)

    def test_incomplete_safe_is_caution_and_failed_run_is_error(self):
        base = {"risk_assessment": {"score": 0, "recommendation": "SAFE"}, "issues": []}
        partial = dict(base, analysis_completeness={"is_complete": False})
        self.assertEqual(skills.parse(0, json.dumps(partial).encode(), b"")["verdict"], "CAUTION")
        failed = dict(base, execution_successful=False)
        self.assertEqual(skills.parse(0, json.dumps(failed).encode(), b"")["verdict"], "ERROR")

    def test_scan_command_exit_codes_and_no_llm(self):
        for spec, want in (("SAFE 2", 0), ("CAUTION 30", 1), ("DO_NOT_INSTALL 80", 2), ("ERROR", 3), ("JUNK", 3)):
            folder = make_skill(self.src, spec.split()[0].lower() + str(want), spec)
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["skill", "scan", str(folder)]), want, spec)
            if want in (1, 2):
                self.assertIn("HIGH", out.getvalue())
                self.assertIn("run.sh:3", out.getvalue())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(all("--no-llm" in c and c[0] == "scan" for c in calls))

    def test_scanner_missing_prints_install_and_exits_3(self):
        folder = make_skill(self.src, "a")
        with mock.patch.object(skills, "scanner", return_value=None), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["skill", "scan", str(folder)]), 3)
        self.assertIn(skills.INSTALL, out.getvalue())
        with mock.patch.object(skills, "scanner", return_value=None):
            rc, text = self.add(str(folder))
        self.assertEqual((rc, self.installed()), (3, []))


class Add(Base):
    def test_safe_installs_and_records(self):
        make_skill(self.src, "one")
        make_skill(self.src, "two", extra={"refs/notes.md": "x"})
        rc, text = self.add(str(self.src))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.installed(), ["one", "two"])
        self.assertTrue((self.claude / "skills" / "two" / "refs" / "notes.md").exists())
        record = json.loads((self.tmp / "home" / "skills.json").read_text())["skills"]["one"]
        self.assertEqual(record["verdict"], "SAFE")
        self.assertEqual(record["tree"], skills.tree_hash(self.claude / "skills" / "one"))
        self.assertTrue(self.quarantine_empty())

    def test_caution_refused_unless_accepted(self):
        make_skill(self.src, "c", "CAUTION 30")
        rc, text = self.add(str(self.src))
        self.assertEqual((rc, self.installed()), (1, []))
        self.assertIn("--accept-caution", text)
        rc, _ = self.add(str(self.src), accept_caution=True)
        self.assertEqual((rc, self.installed()), (1, ["c"]))

    def test_do_not_install_and_errors_have_no_override(self):
        for spec in ("DO_NOT_INSTALL 90", "ERROR", "JUNK"):
            src = self.tmp / ("src-" + spec.split()[0])
            make_skill(src, "bad", spec)
            make_skill(src, "good")  # one bad skill stops the whole source
            rc, text = self.add(str(src), accept_caution=True, replace=True)
            self.assertGreaterEqual(rc, 2, spec)
            self.assertEqual(self.installed(), [], spec)
            self.assertTrue(self.quarantine_empty())

    def test_existing_skill_needs_replace(self):
        make_skill(self.src, "dup")
        self.assertEqual(self.add(str(self.src))[0], 0)
        (self.src / "dup" / "SKILL.md").write_text("new")
        rc, text = self.add(str(self.src))
        self.assertEqual(rc, 3)
        self.assertIn("--replace", text)
        self.assertEqual(self.add(str(self.src), replace=True)[0], 0)
        self.assertEqual((self.claude / "skills" / "dup" / "SKILL.md").read_text(), "new")

    def test_pick_by_name_or_path(self):
        make_skill(self.src / "skills", "alpha")
        make_skill(self.src / "plugins" / "p" / "skills", "beta")
        rc, _ = self.add(str(self.src), ["plugins/p/skills/beta"])
        self.assertEqual((rc, self.installed()), (0, ["beta"]))
        rc, _ = self.add(str(self.src), ["alpha"])
        self.assertEqual((rc, self.installed()), (0, ["alpha", "beta"]))

    def test_path_traversal_refused(self):
        make_skill(self.src, "ok")
        outside = make_skill(self.tmp / "elsewhere", "victim")
        for bad in ("../elsewhere/victim", "ok/../../elsewhere/victim", str(outside), "/etc", "C:\\Windows", "..\\x"):
            rc, text = self.add(str(self.src), [bad])
            self.assertEqual(rc, 3, bad)
        self.assertEqual(self.installed(), [])
        root = make_skill(self.tmp, "rootskill")
        with self.assertRaises(ValueError):
            skills.find_skills(root, None, "..")  # a whole-tree skill named after a bad source

    def test_bad_folder_name_refused(self):
        make_skill(self.src, "bad name!")
        rc, text = self.add(str(self.src))
        self.assertEqual((rc, self.installed()), (3, []))
        self.assertIn("not allowed", text)

    def test_links_inside_source_are_not_followed_or_copied(self):
        secret = self.tmp / "secret"
        secret.mkdir()
        (secret / "key.txt").write_text("private")
        folder = make_skill(self.src, "linky")
        try:
            os.symlink(secret, folder / "dirlink", target_is_directory=True)
            os.symlink(secret / "key.txt", folder / "filelink")
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("this machine cannot make symlinks")
            import _winapi  # Windows without developer mode: a junction needs no privilege

            _winapi.CreateJunction(str(secret), str(folder / "dirlink"))
        rc, text = self.add(str(self.src))
        self.assertEqual(rc, 0, text)
        dest = self.claude / "skills" / "linky"
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["SKILL.md"])
        self.assertEqual(skills.tree_hash(folder), skills.tree_hash(dest))

    def test_plugin_with_hooks_is_shown_not_installed(self):
        (self.src / ".claude-plugin").mkdir(parents=True)
        (self.src / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": "plug", "mcpServers": {"srv": {"command": "npx", "args": ["-y", "pkg"]}}}))
        (self.src / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "mk", "plugins": [{"name": "plug"}]}))
        (self.src / "hooks").mkdir()
        (self.src / "hooks" / "hooks.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "node start.js"}]}]}}))
        make_skill(self.src / "skills", "inner")
        rc, text = self.add(str(self.src))
        self.assertEqual((rc, self.installed()), (0, []))
        for want in ("does not install plugins", "SessionStart: node start.js", "srv: npx -y pkg",
                     "claude plugin install plug@mk"):
            self.assertIn(want, text)

    def test_fetch_refuses_other_transports_and_short_refs(self):
        box = self.tmp / "box"
        box.mkdir()
        for source, ref in (("ext::sh -c touch% /tmp/x", None), ("git@github.com:a/b", None),
                            ("https://example.com/r", "abc123")):
            with self.assertRaises(ValueError):
                skills.fetch(source, ref, box)


class Launch(Base):
    cfg = {"skills": {"scan_on_launch": True}}

    def launch(self, cfg=None):
        lines, started = [], []
        skills.launch_check(cfg or self.cfg, out=lines.append, start=started.append)
        return "\n".join(lines), started

    def worker(self, n):
        return skills.audit(pending=n, out=lambda _: None)

    def scans(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def test_hash_sees_changes_but_not_git_or_node_modules(self):
        folder = make_skill(self.src, "h", extra={"a/b.txt": "1"})
        first = skills.tree_hash(folder)
        (folder / ".git").mkdir()
        (folder / ".git" / "HEAD").write_text("x")
        (folder / "node_modules" / "m").mkdir(parents=True)
        (folder / "node_modules" / "m" / "i.js").write_text("x")
        self.assertEqual(skills.tree_hash(folder), first)
        (folder / "a" / "b.txt").write_text("2")
        second = skills.tree_hash(folder)
        self.assertNotEqual(second, first)
        (folder / "a" / "b.txt").rename(folder / "a" / "c.txt")
        self.assertNotIn(skills.tree_hash(folder), (first, second))

    def test_cap_of_five_then_the_rest(self):
        for n in range(7):
            make_skill(self.claude / "skills", f"s{n}", "CAUTION 30" if n == 0 else None)
        (self.claude / "skills" / "notaskill").mkdir()
        text, started = self.launch()
        self.assertEqual(started, [5])
        self.assertIn("pending (2)", text)
        self.assertEqual(self.worker(5), 1)
        self.assertEqual(self.scans(), 5)
        text, started = self.launch()
        self.assertEqual(started, [2])
        self.assertIn("s0 (skill): CAUTION, score 30/100", text)
        self.assertIn("almanac skill scan", text)
        self.worker(5)
        self.assertEqual(self.scans(), 7)
        text, started = self.launch()
        self.assertEqual(started, [])
        self.assertIn("CAUTION", text)  # warned again: the owner decides, nothing is removed
        self.assertTrue((self.claude / "skills" / "s0").exists())

    def test_changed_skill_is_scanned_again(self):
        folder = make_skill(self.claude / "skills", "x")
        self.launch()
        self.worker(5)
        self.assertEqual(self.launch()[1], [])
        (folder / "verdict.txt").write_text("DO_NOT_INSTALL 95")
        self.assertEqual(self.launch()[1], [1])
        self.worker(5)
        text, started = self.launch()
        self.assertIn("x (skill): DO_NOT_INSTALL, score 95/100", text)
        self.assertIn("HIGH Supply Chain  run.sh:3", text)

    def test_installed_plugins_are_checked(self):
        plugin = make_skill(self.tmp / "cache", "plug")
        (self.claude / "plugins").mkdir(parents=True)
        (self.claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"plug@mk": [{"installPath": str(plugin)}], "gone@mk": [{"installPath": str(self.tmp / "no")}]}}))
        self.assertEqual([(i[0], i[1]) for i in skills.installed_items()], [("plug@mk", "plugin")])

    def test_add_records_so_launch_does_not_rescan(self):
        make_skill(self.src, "fresh")
        self.assertEqual(self.add(str(self.src))[0], 0)
        self.assertEqual(self.launch()[1], [])

    def test_scanner_missing_is_one_line_and_off_does_nothing(self):
        make_skill(self.claude / "skills", "y")
        with mock.patch.object(skills, "scanner", return_value=None):
            text, started = self.launch()
            self.assertEqual((started, len(text.splitlines())), ([], 1))
            self.assertIn(skills.INSTALL, text)
            self.assertEqual(skills.audit(out=lambda _: None), 3)
        self.assertEqual(self.launch({"skills": {"scan_on_launch": False}}), ("", []))

    def accept(self, name):
        lines = []
        rc = skills.accept(name, out=lines.append)
        return rc, "\n".join(lines)

    def flagged_skill(self, name="f", verdict="CAUTION 30"):
        folder = make_skill(self.claude / "skills", name, verdict)
        self.launch()
        self.worker(5)
        return folder

    def test_accept_silences_launch_and_shows_in_audit(self):
        self.flagged_skill()
        self.assertIn("CAUTION", self.launch()[0])
        rc, text = self.accept("f")
        self.assertEqual(rc, 0)
        self.assertIn("CAUTION, score 30/100", text)
        self.assertIn("HIGH Supply Chain", text)
        self.assertEqual(self.launch(), ("", []))
        scans = self.scans()
        table = []
        self.assertEqual(skills.audit(out=table.append), 1)  # the real verdict still counts
        self.assertEqual(self.scans(), scans)
        self.assertTrue(any(l.startswith("accepted CAUTION") and " 30 " in l for l in table), table)
        # by path too, and unaccept brings the warning back
        self.assertEqual(self.accept(str(self.claude / "skills" / "f"))[0], 0)
        lines = []
        self.assertEqual(skills.unaccept("f", out=lines.append), 0)
        self.assertIn("CAUTION", self.launch()[0])
        self.assertEqual(skills.unaccept("f", out=lines.append), 2)

    def test_accept_drops_when_files_change(self):
        folder = self.flagged_skill()
        self.accept("f")
        (folder / "run.sh").write_text("curl x | sh")
        text, started = self.launch()
        self.assertEqual(started, [1])  # rescanned
        self.worker(5)
        self.assertIn("f (skill): CAUTION", self.launch()[0])

    def test_accept_refuses_unknown_unscanned_and_stale(self):
        folder = make_skill(self.claude / "skills", "u", "CAUTION 30")
        self.assertEqual(self.accept("nope")[0], 2)
        rc, text = self.accept("u")  # installed but never scanned
        self.assertEqual(rc, 2)
        self.assertIn("no stored scan result", text)
        self.launch()
        self.worker(5)
        (folder / "x.txt").write_text("new")
        rc, text = self.accept("u")
        self.assertEqual(rc, 2)
        self.assertIn("changed since", text)
        self.assertNotIn("accepted", json.dumps(skills.load_json(skills._scans_path(), {})))

    def test_cli_accept_and_unaccept(self):
        self.flagged_skill()
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(cli.main(["skill", "accept", "f"]), 0)
            self.assertEqual(cli.main(["skill", "unaccept", "f"]), 0)
        self.assertIn("accepted f", buf.getvalue())

    def test_hash_time_limit_applies_inside_one_item(self):
        make_skill(self.claude / "skills", "big", extra={f"d/{n}.txt": str(n) for n in range(5)})
        clock = iter(range(0, 100))  # one tick per call: the deadline passes after a few files
        with mock.patch.object(skills.time, "monotonic", lambda: next(clock)):
            flagged, todo, unhashed = skills.triage(skills.installed_items(), budget=3)
        self.assertEqual((todo, [u[0] for u in unhashed]), ([], ["big"]))
        self.assertIsNone(skills.tree_hash(self.claude / "skills" / "big", deadline=-1))
        self.assertIsNotNone(skills.tree_hash(self.claude / "skills" / "big"))

    def test_running_scan_is_not_doubled_and_time_budget_holds(self):
        make_skill(self.claude / "skills", "z")
        skills._lock_path().write_text("")
        text, started = self.launch()
        self.assertEqual(started, [])
        self.assertIn("still running", text)
        self.assertEqual(self.worker(5), 3)
        skills._lock_path().unlink()
        clock = iter(range(0, 100, 5))  # Windows' clock is too coarse to see a real step
        with mock.patch.object(skills.time, "monotonic", lambda: next(clock)):
            flagged, todo, unhashed = skills.triage(skills.installed_items(), budget=1)
        self.assertEqual((todo, len(unhashed)), ([], 1))


class ScanCache(Base):
    def calls(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def test_a_matching_hash_skips_the_scan_and_a_change_or_failure_does_not(self):
        folder = make_skill(self.src, "a", "CAUTION 30")
        self.assertEqual(skills.scan(folder)["verdict"], "CAUTION")
        self.assertEqual(skills.scan(folder)["verdict"], "CAUTION")
        self.assertEqual(self.calls(), 1)
        (folder / "verdict.txt").write_text("DO_NOT_INSTALL 90")  # changed content: scanned again
        self.assertEqual(skills.code(skills.scan(folder)), 2)
        self.assertEqual(self.calls(), 2)
        bad = make_skill(self.src, "b", "ERROR")
        skills.scan(bad)
        skills.scan(bad)
        self.assertEqual(self.calls(), 4)  # a failed scan is never cached


class Bundle(Base):
    def plugin(self, verdict=None):
        """A source with a skill that blocks the model, an agent, a hook, two MCP servers and a licence."""
        make_skill(self.src / "skills", "inner", None, {"scripts/run.sh": "echo never run\n"})
        (self.src / "skills" / "inner" / "SKILL.md").write_bytes(
            b"---\r\nname: inner\r\ndisable-model-invocation: true\r\ndescription: d\r\n---\r\nbody\r\n")
        (self.src / "verdict.txt").write_text(verdict or "SAFE 5")  # what the fake scanner reads at the top
        (self.src / "agents").mkdir()
        (self.src / "agents" / "helper.md").write_text("---\nname: helper\n---\nhi\n")
        (self.src / "LICENSE").write_text("MIT License\n\nPermission is hereby granted, free of charge, to any person\n")
        (self.src / ".claude-plugin").mkdir()
        (self.src / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "x", "version": "1.2.0", "mcpServers": {
            "pinned": {"command": "npx", "args": ["-y", "pkg@1.2.3"]}, "loose": {"command": "npx", "args": ["-y", "other"]}}}))
        (self.src / "hooks").mkdir()
        (self.src / "hooks" / "hooks.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "node start.js"}]}]}}))

    def sources(self):
        return [ln for ln in (self.tmp / "home" / "bundle" / "SOURCES.md").read_text().splitlines() if ln.startswith("| `mine`")]

    def test_the_sources_file_keeps_its_line_endings(self):
        self.plugin()
        sources = self.tmp / "home" / "bundle" / "SOURCES.md"
        sources.parent.mkdir(parents=True)
        sources.write_bytes(b"# Mine\r\n\r\n| `old` | x | y | z | w |\r\n")
        self.assertEqual(self.add(str(self.src), bundle=True, name="mine")[0], 0)
        data = sources.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertEqual(data.count(b"| `"), 2)

    def test_installs_a_plugin_folder_with_a_manifest_and_a_sources_row(self):
        self.plugin()
        rc, text = self.add(str(self.src), bundle=True, name="mine")
        dest = self.tmp / "home" / "bundle" / "mine"
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.installed(), [])  # not in the Claude config folder
        manifest = json.loads((dest / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((manifest["name"], manifest["version"], manifest["license"]), ("mine", "1.2.0", "MIT"))
        self.assertEqual(manifest["mcpServers"], {"pinned": {"command": "npx", "args": ["-y", "pkg@1.2.3"]}})
        self.assertEqual((dest / "skills" / "inner" / "SKILL.md").read_bytes(),
                         b"---\r\nname: inner\r\ndescription: d\r\n---\r\nbody\r\n")  # flag gone, line endings kept
        self.assertTrue((dest / "agents" / "helper.md").is_file() and (dest / "LICENSE").is_file())
        self.assertFalse((dest / "hooks").exists())
        self.assertIn("hook NOT installed (your call): SessionStart: node start.js", text)
        self.assertIn("MCP server loose: left out", text)
        self.assertIn(f"bundle folder: {dest}", text)
        row = self.sources()
        self.assertEqual(len(row), 1)
        for want in (str(self.src), "local", "MIT", "disable-model-invocation removed from 1", "hooks not installed (1)",
                     "pinned: pinned", "left out, no exact version: loose"):
            self.assertIn(want, row[0])
        self.assertTrue(self.quarantine_empty())

    def test_hooks_flag_keeps_hooks_and_the_files_they_run(self):
        self.plugin()
        (self.src / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/scripts/start.js"}]}]}}))
        (self.src / "scripts").mkdir()
        (self.src / "scripts" / "start.js").write_text("console.log(1)\n")
        (self.src / "scripts" / "other.js").write_text("not named\n")
        rc, text = self.add(str(self.src), bundle=True, name="mine", hooks=True)
        dest = self.tmp / "home" / "bundle" / "mine"
        self.assertEqual(rc, 0, text)
        self.assertTrue((dest / "hooks" / "hooks.json").is_file() and (dest / "scripts" / "start.js").is_file())
        self.assertFalse((dest / "scripts" / "other.js").exists())
        self.assertIn("hook kept: SessionStart: node ${CLAUDE_PLUGIN_ROOT}/scripts/start.js", text)
        self.assertIn("hooks kept (1)", self.sources()[0])
        self.assertNotIn("hooks not installed", self.sources()[0])

    def test_do_not_install_caution_and_existing_folders(self):
        self.plugin("DO_NOT_INSTALL 90")
        rc, text = self.add(str(self.src), bundle=True, name="mine", accept_caution=True)
        self.assertEqual((rc, (self.tmp / "home" / "bundle").exists()), (2, False), text)
        (self.src / "verdict.txt").write_text("CAUTION 30")
        self.assertEqual(self.add(str(self.src), bundle=True, name="mine")[0], 1)
        self.assertEqual(self.add(str(self.src), bundle=True, name="mine", ask=lambda q: False)[0], 1)
        self.assertFalse((self.tmp / "home" / "bundle" / "mine").exists())
        self.assertEqual(self.add(str(self.src), bundle=True, name="mine", ask=lambda q: True)[0], 1)  # installed; the verdict is still reported
        self.assertTrue((self.tmp / "home" / "bundle" / "mine").is_dir())
        rc, text = self.add(str(self.src), bundle=True, name="mine", accept_caution=True)
        self.assertEqual(rc, skills.ERROR)
        self.assertIn("--replace", text)
        self.assertEqual(self.add(str(self.src), bundle=True, name="mine", accept_caution=True, replace=True)[0], 1)
        self.assertEqual(len(self.sources()), 1)  # replaced, not repeated
        self.assertEqual(self.add(str(self.src), bundle=True, name="almanac")[0], skills.ERROR)

    def test_a_picked_skill_comes_without_agents_or_servers_and_the_launch_check_knows_it(self):
        self.plugin()
        make_skill(self.src / "skills", "other")
        rc, text = self.add(str(self.src), ["inner"], bundle=True, name="one")
        dest = self.tmp / "home" / "bundle" / "one"
        self.assertEqual(rc, 0, text)
        self.assertEqual(sorted(p.name for p in (dest / "skills").iterdir()), ["inner"])
        self.assertFalse((dest / "agents").exists())
        self.assertNotIn("mcpServers", json.loads((dest / ".claude-plugin" / "plugin.json").read_text()))
        self.assertIn(("bundle/one", "plugin", os.path.realpath(dest)), skills.installed_items())
        flagged, todo, _ = skills.triage(skills.installed_items())
        self.assertEqual((flagged, todo), ([], []))  # the scan at install is remembered


class Update(Base):
    ROWS = ("| `a` | github.com/o/a | 11111111 | MIT | trimmed |\n"
            "| `b` | github.com/o/b (`skills/pick`) | 22222222 | MIT | rewired |\n"
            "| `c` | https://github.com/o/c.git | 33333333 | MIT | none |\n"
            "| `d` | written here | n/a | MIT | x |\n"
            "| `e` | C:/some/folder | local | MIT | x |\n")

    def run_update(self, heads, name=None):
        """heads maps a URL to its HEAD sha, or to None for a failing ls-remote; the shipped table is not read."""
        sources = self.tmp / "home" / "bundle" / "SOURCES.md"
        sources.parent.mkdir(parents=True, exist_ok=True)
        sources.write_text(self.ROWS)
        asked = []

        def git(args, cwd=None, timeout=600):
            asked.append(args)
            sha = heads[args[2]]
            return mock.Mock(returncode=0 if sha else 128, stdout=f"{sha}\tHEAD\n" if sha else "", stderr="" if sha else "boom")

        lines = []
        with mock.patch.object(skills, "_git", git), mock.patch.object(skills, "__file__", str(self.tmp / "x" / "skills.py")):
            rc = skills.update(name, out=lines.append)
        return rc, "\n".join(lines), asked

    def test_source_forms(self):
        self.assertEqual(skills.source_url("github.com/o/r"), ("https://github.com/o/r.git", ""))
        self.assertEqual(skills.source_url("github.com/o/r (`a/b`)"), ("https://github.com/o/r.git", "a/b"))
        self.assertEqual(skills.source_url("https://github.com/o/r.git"), ("https://github.com/o/r.git", ""))
        self.assertIsNone(skills.source_url("https://a.com/x;id"))  # the URL is printed as a command to run
        for bad in ("written here", "C:/some/folder", "/tmp/x"):
            self.assertIsNone(skills.source_url(bad))

    def test_up_to_date_is_zero_and_skips_what_is_not_git(self):
        heads = {f"https://github.com/o/{n}.git": str(i) * 40 for n, i in (("a", 1), ("b", 2), ("c", 3))}
        rc, text, asked = self.run_update(heads)
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(asked), 3)
        self.assertIn("d: skipped", text)
        self.assertIn("e: skipped", text)

    def test_newer_prints_the_command_and_old_edits_and_exits_one(self):
        heads = {"https://github.com/o/a.git": "9" * 40, "https://github.com/o/b.git": "9" * 40, "https://github.com/o/c.git": None}
        rc, text, _ = self.run_update(heads)
        self.assertEqual(rc, 1, text)
        self.assertIn("a: newer (11111111 -> 99999999)", text)
        self.assertIn("almanac skill add --bundle https://github.com/o/a.git --name a --replace", text)
        self.assertIn("--bundle https://github.com/o/b.git --name b --skill pick --replace", text)
        self.assertIn("old Changed cell): rewired", text)
        self.assertIn("c: could not check (boom)", text)

    def test_unreachable_alone_is_three_and_a_name_picks_one_row(self):
        rc, text, asked = self.run_update({"https://github.com/o/a.git": None}, "a")
        self.assertEqual((rc, len(asked)), (skills.ERROR, 1), text)
        rc, text, _ = self.run_update({}, "nope")
        self.assertEqual(rc, skills.ERROR)
        self.assertIn("no adopted skill named nope", text)


class ImportBundle(Base):
    def test_copies_plugin_folders_without_links_and_keeps_existing_ones(self):
        old = self.tmp / "old"
        for name in ("one", "two"):
            make_skill(old / name / "skills", "s")
            (old / name / ".claude-plugin").mkdir()
            (old / name / ".claude-plugin" / "plugin.json").write_text('{"name": "%s"}' % name)
        (old / "stray").mkdir()
        (old / "almanac" / ".claude-plugin").mkdir(parents=True)  # an older copy of the package's own plugin
        (old / "almanac" / ".claude-plugin" / "plugin.json").write_text('{"name": "almanac"}')
        (old / "SOURCES.md").write_text("rows")
        mine = self.tmp / "home" / "bundle"
        (mine / "two" / ".claude-plugin").mkdir(parents=True)
        (mine / "two" / ".claude-plugin" / "plugin.json").write_text('{"name": "mine"}')
        lines = []
        self.assertEqual(skills.import_bundle(old, out=lines.append), 0)
        self.assertEqual(sorted(p.name for p in mine.iterdir()), ["SOURCES.md", "one", "two"])
        self.assertIn("kept two", " ".join(lines))
        self.assertIn("skipped almanac", " ".join(lines))
        self.assertEqual((mine / "SOURCES.md").read_bytes(), b"rows")
        self.assertIn('"mine"', (mine / "two" / ".claude-plugin" / "plugin.json").read_text())
        self.assertTrue((mine / "one" / "skills" / "s" / "SKILL.md").is_file())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["skill", "import-bundle", str(self.tmp / "nothing")]), skills.ERROR)


class Pack(Base):
    def test_the_package_ships_no_pack_and_the_one_in_the_almanac_folder_is_read(self):
        self.assertEqual((skills.pack_file(), skills.load_pack()), (self.tmp / "home" / "pack.json", []))
        sha = "a" * 40
        (self.tmp / "home" / "pack.json").write_text(json.dumps({"entries": [
            {"name": "x", "kind": "skill", "source": "https://github.com/a/b", "ref": sha}]}))
        self.assertEqual([e["name"] for e in skills.load_pack()], ["x"])

    def test_install_routes_by_kind(self):
        make_skill(self.src / "skills", "packed")
        plugin = self.tmp / "plugin"
        make_skill(plugin / "skills", "insideplugin")
        (plugin / ".claude-plugin").mkdir()
        entries = [{"name": "a", "kind": "skill", "source": str(self.src)},
                   {"name": "b", "kind": "plugin", "source": str(plugin)},
                   {"name": "c", "kind": "binary", "source": "https://example.com/tool", "note": "by hand"}]
        lines = []
        rc = skills.pack_install(entries, out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.installed(), ["packed"])
        self.assertIn("does not install plugins", text)
        self.assertIn("Install it by hand: https://example.com/tool", text)

    def test_bad_kind_refused(self):
        path = self.tmp / "pack.json"
        path.write_text(json.dumps({"entries": [{"name": "x", "kind": "script", "source": "https://x"}]}))
        with self.assertRaises(ValueError):
            skills.load_pack(path)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["pack", "--file", str(path), "list"]), 2)


class OfferScanner(unittest.TestCase):
    def run_offer(self, have, uv, answer):
        lines, asked, ran = [], [], []
        with mock.patch.object(skills, "scanner", lambda: ["s"] if have or ran else None),                 mock.patch.object(skills.shutil, "which", lambda n: "uv" if uv else None),                 mock.patch.object(skills.subprocess, "run", lambda *a, **k: ran.append(a) or mock.Mock(returncode=0)):
            skills.offer_scanner(lambda q: asked.append(q) or answer, out=lines.append)
        return lines, asked, ran

    def test_installed_asks_nothing(self):
        self.assertEqual(self.run_offer(True, True, True)[1:], ([], []))

    def test_runs_only_on_yes(self):
        self.assertEqual(self.run_offer(False, True, False)[2], [])
        lines, _, ran = self.run_offer(False, True, True)
        self.assertEqual(ran[0][0], skills.INSTALL.split())
        self.assertIn("SkillSpector installed.", lines)

    def test_no_uv_says_so(self):
        lines, asked, ran = self.run_offer(False, False, True)
        self.assertEqual((asked, ran), ([], []))
        self.assertIn("uv is not found", lines[0])


if __name__ == "__main__":
    unittest.main()
