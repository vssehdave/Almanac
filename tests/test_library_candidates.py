import contextlib
import io
import json
import unittest

import test_mine as tm

from getalmanac import library, mine, playbook
from getalmanac.store import Store

PULL = ["Bash: git pull", "Bash: python build.py"]


class LibraryCandidateTest(tm.Home):
    def entries(self, *rows):
        """rows: (prompt, route, uses, superseded_by); written to the store `mine` opens."""
        store = Store(self.alm / "almanac.db")
        library.setup(store)
        for n, (prompt, route, uses, newer) in enumerate(rows, 1):
            store.run("insert into library (convo, seq, project, prompt, route, uses, created, updated, superseded_by) "
                      "values (?,?,?,?,?,?,?,?,?)", (n, 0, "p", prompt, json.dumps(route), uses, 1.0, 1.0, newer))
        store.close()

    def mined(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return mine.mine(30, 3, self.cfg)

    def test_a_used_mechanical_entry_becomes_a_candidate(self):
        self.entries(("refresh the build output", PULL, 2, 0))
        (c,) = self.mined()
        self.assertEqual((c["tools"], c["examples"], c["count"]), (PULL, ["refresh the build output"], 2))
        self.assertEqual(json.loads(mine.candidates_path().read_text(encoding="utf-8"))["candidates"], [c])
        self.assertIn("refresh the build output", mine.build_prompt(c))  # the unchanged compile path takes it

    def test_one_use_does_not(self):
        self.entries(("refresh the build output", PULL, 1, 0))
        self.assertEqual(self.mined(), [])

    def test_an_edit_step_does_not(self):
        self.entries(("refresh the build output", PULL + ["Edit"], 3, 0))
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
