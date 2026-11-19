import contextlib
import io
import json
import sqlite3
import unittest
from unittest import mock

import test_mine as tm

from getalmanac import library, mine, playbook
from getalmanac.memory import Memory
from getalmanac.project import Projects
from getalmanac.store import Store

PULL = ["Bash: git pull", "Bash: python build.py"]
FORMS = ["Bash: git status", "Bash: python -m pytest", "Bash: npm run build"]  # the step form: rtk unwrapped, flags and cd dropped


class LibraryCandidateTest(tm.Home):
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
        self.entries((tm.REPORT[0], FORMS, 4, 0))
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
