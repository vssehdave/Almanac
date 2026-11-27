import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "src", "getalmanac", "bundle", "guard", "scripts")


def run(script, tool, **tool_input):
    return run_raw(script, json.dumps({"tool_name": tool, "tool_input": tool_input}))


def run_raw(script, text):
    done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], input=text, capture_output=True, text=True)
    return done.returncode, done.stderr


class ConfigGuard(unittest.TestCase):
    def blocked(self, tool, **args):
        code, err = run("config_guard.py", tool, **args)
        self.assertEqual(code, 2, args)
        self.assertIn("ask the user", err)

    def allowed(self, tool, **args):
        self.assertEqual(run("config_guard.py", tool, **args)[0], 0, args)

    def test_edit_tools_on_config_files_are_blocked(self):
        for name in (".eslintrc.json", "a/eslint.config.mjs", ".prettierrc", "C:/p/tsconfig.build.json", "ruff.toml",
                     "mypy.ini", ".flake8", "tox.ini", "pytest.ini", "setup.cfg", ".coveragerc"):
            self.blocked("Edit", file_path=name, old_string="a", new_string="b")
        self.blocked("Write", file_path="tsconfig.json", content="{}")
        self.blocked("MultiEdit", file_path="x/.flake8", edits=[{"old_string": "a", "new_string": "b"}])

    def test_other_files_are_allowed(self):
        self.allowed("Edit", file_path="src/app.py", old_string="a", new_string="b")
        self.allowed("Write", file_path="package.json", content="{}")
        self.allowed("Read", file_path="ruff.toml")

    def test_pyproject_is_guarded_only_in_its_tool_sections(self):
        path = os.path.join(tempfile.mkdtemp(), "pyproject.toml")
        with open(path, "w") as handle:
            handle.write('[project]\nname = "x"\n\n[tool.ruff]\nline-length = 88\n\n[tool.other]\na = 1\n')
        self.blocked("Edit", file_path=path, old_string="line-length = 88", new_string="line-length = 200")
        self.allowed("Edit", file_path=path, old_string='name = "x"', new_string='name = "y"')
        self.allowed("Edit", file_path=path, old_string="a = 1", new_string="a = 2")
        self.blocked("Write", file_path=path, content='[project]\nname = "x"\n\n[tool.other]\na = 1\n')
        self.allowed("Write", file_path=path, content='[project]\nname = "z"\n\n[tool.ruff]\nline-length = 88\n\n[tool.other]\na = 1\n')

    def test_commands_that_write_to_configs_are_blocked_on_both_shells(self):
        for tool in ("Bash", "PowerShell"):
            self.blocked(tool, command="echo 'ignore' >> .eslintrc.json")
            self.blocked(tool, command="sed -i 's/on/off/' tox.ini")
            self.blocked(tool, command=r"Set-Content -Path .\mypy.ini -Value x")
            self.allowed(tool, command="cat .eslintrc.json")
            self.allowed(tool, command="python -m flake8 src")
            self.allowed(tool, command="sed -i 's/a/b/' pyproject.toml")
            self.blocked(tool, command="sed -i 's/a/b/' pyproject.toml  # [tool.ruff]")

    def test_bad_input_or_a_crash_allows(self):
        self.assertEqual(run_raw("config_guard.py", "not json")[0], 0)
        self.assertEqual(run_raw("config_guard.py", "{}")[0], 0)
        self.assertEqual(run_raw("config_guard.py", json.dumps({"tool_name": "Edit", "tool_input": {}}))[0], 0)

    def test_segments_and_targets(self):
        for command in ("grep -n write tox.ini", "cp tsconfig.json /tmp/b.json", "cat setup.cfg && rm -rf build"):
            self.allowed("Bash", command=command)
        for command in ("echo x >> 'C:/p q/mypy.ini'", "cp /tmp/a tsconfig.json", "echo x > .ESLINTRC.json"):
            self.blocked("Bash", command=command)

    def test_utf8_payload_and_toml_array_sections(self):
        path = os.path.join(tempfile.mkdtemp(), "pyproject.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[project]\ndescription = \"a \u2014 b\"\n\n[[tool.mypy.overrides]]\nx = 1\n")
        payload = json.dumps({"tool_name": "Edit", "tool_input": {
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2"}}, ensure_ascii=False)
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, "config_guard.py")],
                              input=payload.encode("utf-8"), capture_output=True)
        self.assertEqual(done.returncode, 2)


class NoVerifyGuard(unittest.TestCase):
    def code(self, command, tool="Bash"):
        return run("no_verify_guard.py", tool, command=command)[0]

    def test_skipping_hooks_or_signing_is_blocked_on_both_shells(self):
        for tool in ("Bash", "PowerShell"):
            for command in ("git commit --no-verify -m x", "git commit -n -m x", "git commit -an", "git push --no-verify",
                            "git commit --no-gpg-sign -m x", "git -c commit.gpgsign=false commit -m x",
                            "git -c core.hooksPath=/dev/null commit -m x", "git config core.hooksPath /tmp",
                            "npm test && git commit -m x --no-verify"):
                self.assertEqual(self.code(command, tool), 2, command)

    def test_normal_git_use_is_allowed(self):
        for command in ("git commit -m 'fix --no-verify docs'", 'git commit -am "use -n here"', "git push -n",
                        "git status", "ls --no-verify", "git commit -m x", "git log -n 5 -- src/commit.py",
                        "git commit -uno -m x", "git commit -minit", "git config --get core.hooksPath",
                        'git commit -m "set core.hooksPath"'):
            self.assertEqual(self.code(command), 0, command)
        self.assertEqual(run("no_verify_guard.py", "Edit", file_path="a", old_string="git commit -n", new_string="")[0], 0)

    def test_bad_input_allows_and_the_reason_is_printed(self):
        self.assertEqual(run_raw("no_verify_guard.py", "")[0], 0)
        self.assertEqual(run_raw("no_verify_guard.py", '{"tool_name": "Bash", "tool_input": []}')[0], 0)
        self.assertIn("skips git hooks", run("no_verify_guard.py", "Bash", command="git commit -n")[1])


class DestructiveGuard(unittest.TestCase):
    def run_full(self, command, tool="Bash"):
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, "no_verify_guard.py")],
                              input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}),
                              capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    def asks(self, command, tool="Bash"):
        code, out, _ = self.run_full(command, tool)
        self.assertEqual(code, 0, command)
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]

    def test_destructive_commands_ask(self):
        for command in ("rm -rf build", "rm -fr x", "rm -r -f x", "sudo rm -rf /tmp/x", "env git reset --hard",
                        "git reset --hard HEAD~1", "git clean -fd", "git clean -xdf", "git checkout -- .",
                        "git restore .", "git branch -D feat", "git push --force origin main", "git push -f",
                        'sqlite3 a.db "DROP TABLE t"', 'psql -c "drop database x"',
                        'sqlite3 a.db "ALTER TABLE t DROP COLUMN c"', "cd x && rm -rf y",
                        "git -C . reset --hard", "git -c x=y clean -fd", "bash -c 'rm -rf x'", "echo $(rm -rf x)",
                        "FOO=1 rm -rf x", "env FOO=1 rm -rf x", "sudo -u root rm -rf x", "(rm -rf x)",
                        "/bin/rm -rf x", "ri -r -fo x", "rm -Recurse -Force x", "git branch -Df x",
                        "git branch --delete --force x", "git restore -- .", "git stash clear",
                        "git push origin +main", 'sqlite3 db "DROP TABLE x"', "true & rm -rf x",
                        "sleep 1 & git reset --hard", "Remove-Item -rec -for x", 'eval "rm -rf x"', "exec rm -rf x",
                        '"rm" -rf x', "find . -exec rm -rf {} +", 'pwsh -c "Remove-Item -Recurse -Force x"',
                        'cmd /c "rmdir /s /q x"', "rd /s x", "cmd /c rmdir /s /q x",
                        "pwsh -c Remove-Item -Recurse -Force x", "find . -delete", "find . -name '*.o' -delete",
                        "cat <<'EOF'\nEOF\n`rm -rf x`", "echo \"<<'EOF'\"\n$(rm -rf x)\nEOF",
                        "bash <<'EOF'\necho $(rm -rf x)\nEOF", "cat <<'EOF' | sh\nx=$(rm -rf y)\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(self.asks(command), "ask")

    def test_powershell_remove_item_recurse_force_asks(self):
        self.assertEqual(self.asks("Remove-Item -Recurse -Force build", "PowerShell"), "ask")

    def test_safe_commands_are_allowed(self):
        for command in ("rm file.txt", "rm -r build", "git reset --soft HEAD~1", "git clean -n", "git push",
                        "git push origin main", "git branch -d feat", 'echo "rm -rf /"', "git restore src/a.py",
                        "git checkout main", "grep -rn 'DROP TABLE' src", 'gh pr create --body "removes DROP TABLE call"',
                        'git commit -m "fix; drop table users"', "git push --force-with-lease", "echo rm -rf",
                        "docker rm -f c", "git commit -F - <<'EOF'\nfix: avoid `git reset --hard` here\nEOF",
                        "echo a & echo b", "cmd 2>&1 | tail", "cmd &> out.log", "curl 'http://x/?a=1&b=2'",
                        "eval $(ssh-agent)", "find . -name x",
                        "cat <<'EOF'\nEOF\necho ok", "git commit -F - <<'EOF'\nnote: $(git reset --hard)\nEOF"):
            with self.subTest(command=command):
                code, out, _ = self.run_full(command)
                self.assertEqual(code, 0, command)
                self.assertEqual(out.strip(), "", command)

    def test_order_no_verify_after_destructive_returns_the_no_verify_reason(self):
        code, err = run("no_verify_guard.py", "Bash", command="rm -rf x; git commit --no-verify")
        self.assertEqual(code, 2)
        self.assertIn("skips git hooks", err)

    def test_guardlib_script_prints_ask_and_no_verify_exits_2(self):
        code, out, _ = self.run_full("rm -rf x")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(run("no_verify_guard.py", "Bash", command="git commit --no-verify -m x")[0], 2)

    def test_reason_asks_the_user(self):
        code, out, _ = self.run_full("git reset --hard")
        self.assertIn("ask the user to approve it", out)


if __name__ == "__main__":
    unittest.main()
