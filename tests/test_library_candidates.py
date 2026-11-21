import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import mine, playbook

from getalmanac import config, library
from getalmanac.memory import Memory
from getalmanac.project import Projects
from getalmanac.store import Store

PULL = ["Bash: git pull", "Bash: python build.py"]
FORMS = ["Bash: git status", "Bash: python -m pytest", "Bash: npm run build"]  # the step form: rtk unwrapped, flags and cd dropped



NOW = time.time()


SECRET = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2"


CWD = "C:\\Users\\someone\\code\\billing-app"


BUILD = [("Bash", {"command": "git pull --rebase"}), ("Bash", {"command": "python build.py --all"}),
         ("Skill", {"skill": "interfaces:better-layout"}), ("Agent", {"subagent_type": "crew:code-reviewer"})]


REPORT = ["please build the weekly sales report and email it to finance",
          "build weekly sales report then email finance please",
          "build the weekly sales report, email finance"]


def stamp(ts=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts or NOW))


def line(**kw) -> str:
    return json.dumps(kw)


def write_session(base, folder, sid, turns, cwd=CWD, extra=(), when=None):
    """turns: [(typed text, [(tool name, input)])]. Each tool call is one request written as two lines, plus a tool result."""
    lines = []
    for n, (text, tools) in enumerate(turns):
        lines.append(line(type="user", isSidechain=False, sessionId=sid, cwd=cwd, timestamp=stamp(when),
                          origin={"kind": "human"}, message={"role": "user", "content": text}))
        for i, (name, args) in enumerate(tools):
            req = f"req-{sid}-{n}-{i}"
            usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 50}
            block = {"type": "tool_use", "id": f"t{n}{i}", "name": name, "input": args}
            for content in ([{"type": "text", "text": "ok"}], [block]):
                lines.append(line(type="assistant", isSidechain=False, requestId=req, sessionId=sid, timestamp=stamp(when),
                                  message={"role": "assistant", "content": content, "usage": usage}))
            lines.append(line(type="user", isSidechain=False, sessionId=sid, timestamp=stamp(when),
                              message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}{i}",
                                                                    "content": "SECRET-RESULT " + SECRET}]}))
    lines += list(extra)
    path = Path(base) / "projects" / folder / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Home(unittest.TestCase):
    """A fresh ALMANAC_HOME, home folder and Claude config folder for each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="almanac-mine-"))
        self.cfg_dir, self.user_home, self.alm = self.tmp / "cfg", self.tmp / "home", self.tmp / "almanac"
        for p in (self.cfg_dir, self.user_home, self.alm):
            p.mkdir()
        patches = [mock.patch.dict(os.environ, {"ALMANAC_HOME": str(self.alm), "CLAUDE_CONFIG_DIR": str(self.cfg_dir)}),
                   mock.patch("pathlib.Path.home", return_value=self.user_home)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = copy.deepcopy(config.DEFAULTS)

    def three_sessions(self, texts=REPORT, tools=BUILD, folder="C--code-billing-app"):
        for sid in (0, 1, 2):  # Playbook counts a repeat once per session, so three requests need three sessions
            write_session(self.cfg_dir, folder, f"sess-{sid}", [(t, tools) for n, t in enumerate(texts) if n % 3 == sid])
        return texts


class LibraryCandidateTest(Home):
    def entries(self, *rows, project="p", names=()):
        """rows: (prompt, route, uses, superseded_by); written to the store `mine` opens. names: {project key: name} to know."""
        store = Store(self.alm / "almanac.db")
        Memory(store)  # the tables of the conversations and the library
        Projects(store)
        for key, name in dict(names).items():
            store.run("insert into project (key, name, first_seen, last_seen) values (?,?,1,1)", (key, name))
        for n, (prompt, route, uses, newer) in enumerate(rows, 1):
            store.run("insert into library (convo, seq, project, prompt, route, uses, created, updated, superseded_by, calls) "
                      "values (?,?,?,?,?,?,?,?,?,5)", (n, 0, project if isinstance(project, str) else project[n - 1], prompt, json.dumps(route),
                                                      uses, 1.0, 1.0, newer))
        store.close()

    def mined(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return mine.mine(30, 3, self.cfg)

    def test_a_used_mechanical_entry_becomes_a_candidate(self):
        self.entries(("refresh the build output", PULL, 2, 0))
        (c,) = self.mined()
        self.assertEqual((c["tools"], c["examples"], c["count"], c["sessions"]), (PULL, ["refresh the build output"], 2, 1))
        self.assertNotIn("median_requests", c)
        self.assertEqual(json.loads(mine.candidates_path().read_text(encoding="utf-8"))["candidates"], [c])
        self.assertIn("refresh the build output", mine.build_prompt(c))  # the unchanged compile path takes it

    def test_one_use_does_not(self):
        self.entries(("refresh the build output", PULL, 1, 0))
        self.assertEqual(self.mined(), [])

    def test_an_edit_step_does_not(self):
        self.entries(("refresh the build output", PULL + ["Edit"], 3, 0))
        self.assertEqual(self.mined(), [])

    def test_a_skill_step_or_a_single_step_does_not(self):
        self.entries(("refresh the build output", PULL + ["Skill: x:y"], 3, 0), ("tidy the release notes now", ["Bash: git pull"], 3, 0))
        self.assertEqual(self.mined(), [])

    def test_a_superseded_entry_does_not(self):
        self.entries(("refresh the build output", PULL, 5, 2), ("refresh the build output", PULL, 0, 0))
        self.assertEqual(self.mined(), [])

    def test_the_same_route_in_the_transcripts_gives_one_candidate(self):
        tools = [("Bash", {"command": "git pull --rebase"}), ("Bash", {"command": "python build.py --all"}), ("Bash", {"command": "git push"})]
        self.three_sessions(tools=tools)
        self.entries(("refresh the build output", ["Bash: git pull", "Bash: python build.py", "Bash: git push"], 4, 0))
        (c,) = self.mined()
        self.assertEqual(c["count"], 3)  # the one the transcripts found

    def test_the_step_form_of_a_found_candidate_is_compared_with_the_step_form(self):
        store = Store(self.alm / "almanac.db")
        Memory(store)  # the tables of the conversations and the library
        store.run("insert into library (convo, seq, project, prompt, route, uses, created, updated, calls) values (1,0,'p','ship it',?,2,1,1,5)",
                  (json.dumps(FORMS),))
        found = [{"tools": ["Bash: rtk git", "Bash: python -m", "Bash: npm run"], "steps": FORMS + ["Bash: git push"], "examples": ["something else"]}]
        self.assertEqual(library.candidates(store, found), [])
        self.assertEqual(len(library.candidates(store, [dict(found[0], steps=["Bash: make deploy", "Bash: git tag"])])), 1)
        store.close()

    def test_the_same_commands_in_raw_form_and_a_like_request_give_one_candidate(self):
        tools = [("Bash", {"command": "rtk git status"}), ("Bash", {"command": "python -m pytest -q"}),
                 ("Bash", {"command": "cd x && npm run build"})]
        self.three_sessions(tools=tools)
        self.entries((REPORT[0], FORMS, 4, 0))
        (c,) = self.mined()
        self.assertEqual(c["count"], 3)  # the one the transcripts found

    def test_projects_are_names_and_at_most_five(self):
        keys = [f"git:github.com/someone/p{n}" for n in range(7)]
        self.entries(*[(f"refresh build {n}", PULL, 2, 0) for n in range(7)], project=keys, names={k: f"p{n}" for n, k in enumerate(keys)})
        (c,) = self.mined()
        self.assertEqual(c["projects"], ["p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(c["sessions"], 7)  # one conversation for each entry

    def test_a_locked_database_gives_no_library_candidates(self):
        self.three_sessions()
        self.entries(("refresh the build output", PULL, 2, 0))
        with mock.patch("getalmanac.library.candidates", side_effect=sqlite3.OperationalError("database is locked")):
            (c,) = self.mined()
        self.assertEqual(c["count"], 3)

    def test_two_entries_of_one_route_give_one_candidate(self):
        self.entries(("refresh the build output", PULL, 2, 0), ("rebuild the output again", PULL, 2, 0))
        (c,) = self.mined()
        self.assertEqual((c["count"], len(c["examples"])), (4, 2))

    def test_a_playbook_that_matches_hides_it(self):
        self.entries(("refresh the build output", PULL, 2, 0))
        playbook.save({"name": "refresh", "title": "t", "when": "w", "triggers": ["refresh the build output"], "steps": ["x"]})
        self.assertEqual(self.mined(), [])

    def test_no_database_is_fine_and_is_not_created(self):
        self.assertEqual(self.mined(), [])
        self.assertFalse((self.alm / "almanac.db").exists())


if __name__ == "__main__":
    unittest.main()
