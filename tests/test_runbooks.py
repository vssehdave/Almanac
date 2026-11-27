import os
import tempfile
import unittest

from getalmanac import mcp, project
from getalmanac.memory import Memory
from getalmanac.store import State, Store


def make_repo(parent, name, remote=None):
    root = os.path.join(parent, name)
    os.makedirs(os.path.join(root, ".git"))
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, ".git", "config"), "w") as handle:
        handle.write("[core]\n\tbare = false\n")
        if remote:
            handle.write(f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    return project.normalize(root)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="almanac-runbook-")
        self.store = Store(os.path.join(self.tmp, "a.db"))
        self.state = State(self.store.path.with_name("state.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, self.state)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.alpha = make_repo(self.work, "alpha", "https://github.com/Someone/Alpha.git")
        self.beta = make_repo(self.work, "beta", "git@github.com:someone/beta.git")
        self.key = self.memory.projects.see(self.alpha)


class RunbookStoreTests(Case):
    def test_add_and_match(self):
        n = self.memory.runbook_add(self.key, "ModuleNotFoundError: no module named yaml", "import fails",
                                    "yaml is not installed", "pip install pyyaml")
        rows = self.memory.runbooks_for(self.key, "I get ModuleNotFoundError: no module named yaml when I run it")
        self.assertEqual([r["id"] for r in rows], [n])
        self.assertEqual(set(rows[0]), {"id", "error", "symptom", "cause", "fix", "created"})
        self.assertEqual(rows[0]["fix"], "pip install pyyaml")

    def test_digit_normalised(self):
        self.memory.runbook_add(self.key, "timeout on port 8080 line 12", "hangs", "port busy", "free the port")
        self.assertEqual(len(self.memory.runbooks_for(self.key, "timeout on  port\t9191 line 77")), 1)

    def test_short_error_ignored(self):
        self.memory.runbook_add(self.key, "error", "something", "unknown", "retry")
        self.assertEqual(self.memory.runbooks_for(self.key, "an error happened"), [])

    def test_other_project(self):
        self.memory.runbook_add(self.key, "timeout on port 8080 line 12", "hangs", "port busy", "free the port")
        other = self.memory.projects.see(self.beta)
        self.assertEqual(self.memory.runbooks_for(other, "timeout on port 8080 line 12"), [])

    def test_empty_field_raises(self):
        fields = ["error", "symptom", "cause", "fix"]
        good = {"error": "timeout on port 8080", "symptom": "hangs", "cause": "port busy", "fix": "free it"}
        for field in fields:
            with self.subTest(field=field):
                args = dict(good, **{field: "   "})
                with self.assertRaises(ValueError):
                    self.memory.runbook_add(self.key, args["error"], args["symptom"], args["cause"], args["fix"])
        self.assertEqual(self.store.run("select count(*) n from runbook")[0]["n"], 0)

    def test_error_cut(self):
        long_line = "x" * 300
        self.memory.runbook_add(self.key, long_line + "\nsecond line", "hangs", "port busy", "free it")
        self.assertEqual(self.store.run("select error from runbook")[0]["error"], "x" * 200)

    def test_limit_newest_first(self):
        ids = [self.memory.runbook_add(self.key, "build failed on step seven", "stops", "bad flag", f"fix {n}")
               for n in range(4)]
        rows = self.memory.runbooks_for(self.key, "build failed on step seven again", limit=3)
        self.assertEqual([r["id"] for r in rows], [ids[3], ids[2], ids[1]])


class RunbookRecallTests(Case):
    def test_recall_runbook_before_pin(self):
        self.memory.runbook_add(self.key, "timeout on port 8080 line 12", "hangs", "port busy", "free the port")
        self.memory.add_pin(self.key, "Plan", ["step one"], 1)
        note = self.memory.recall(self.key, "timeout on port 9191 line 77", "elsewhere", 2400)
        self.assertIn("Runbooks matching this request:", note)
        self.assertLess(note.index("Runbooks matching this request:"), note.index("step one"))

    def test_recall_subagent(self):
        self.memory.runbook_add(self.key, "timeout on port 8080 line 12", "hangs", "port busy", "free the port")
        note = self.memory.recall(self.key, "timeout on port 9191 line 77", "elsewhere", 2400, agent_type="builder")
        self.assertIn("Runbooks matching this request:", note)
        self.assertIn("free the port", note)

    def test_mcp_round_trip(self):
        fields = {"error": "timeout on port 8080 line 12", "symptom": "hangs", "cause": "port busy", "fix": "free the port"}
        self.assertEqual(mcp.call(self.memory, "memory_runbook_add", fields, self.alpha), "Runbook 1 saved.")
        rows = self.memory.runbooks_for(self.key, "timeout on port 9191 line 77")
        self.assertEqual([r["fix"] for r in rows], ["free the port"])
        missing = {k: v for k, v in fields.items() if k != "cause"}
        self.assertEqual(mcp.call(self.memory, "memory_runbook_add", missing, self.alpha),
                         "memory_runbook_add needs error, symptom, cause and fix.")
        blank = dict(fields, symptom="   ")
        self.assertEqual(mcp.call(self.memory, "memory_runbook_add", blank, self.alpha),
                         "memory_runbook_add needs error, symptom, cause and fix.")
        self.assertEqual(len(self.memory.runbooks_for(self.key, "timeout on port 9191 line 77")), 1)


if __name__ == "__main__":
    unittest.main()
