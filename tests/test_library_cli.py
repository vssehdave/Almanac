import contextlib
import io
import os
import time
import unittest

import test_library as tl
from test_playbook import Home

from getalmanac import cli, library, mcp, report
from getalmanac.memory import QUOTED, Memory, MemoryLayer
from getalmanac.store import Store

PROMPT = "Make the x test pass in the proxy"


class CliCase(tl.LibraryCase, Home):
    """The library in the store the command line opens (ALMANAC_HOME/almanac.db)."""

    def setUp(self):
        Home.setUp(self)
        tl.LibraryCase.setUp(self)
        self.store.close()
        self.store = Store(os.path.join(self.home, "almanac.db"))
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.key = self.memory.projects.see(self.alpha)
        self.memory.projects.see(self.beta)
        self.layer = MemoryLayer(self.memory)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["library", *argv])
        return code, out.getvalue(), err.getvalue()

    def tool(self, query, cwd=None):
        reply = mcp.handle(self.memory, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "library_find", "arguments": {"query": query}}}, cwd or self.alpha)["result"]
        return reply["content"][0]["text"], reply["isError"]

    def twice(self):
        """The same task done twice: an old, superseded entry and the current one that lists the changes."""
        self.finish(PROMPT, self.calls(), lane="lane-a")
        self.finish(PROMPT, self.calls()[:1] + self.calls()[3:] + [("Bash", {"command": "ruff check"}, "ok", False)], lane="lane-b", min_tools=4)
        return self.rows()


class ListShowForgetTest(CliCase):
    def test_list_shows_current_entries_only_and_filters_by_project(self):
        old, new = self.twice()
        self.finish("Write the readme introduction completely " + "z" * 100, self.calls(), lane="lane-c")
        code, out, _ = self.run_cli("list")
        lines = out.strip().splitlines()
        self.assertEqual((code, len(lines)), (0, 2))
        self.assertRegex(lines[0], r"^\s*%d\s+\d{4}-\d\d-\d\d\s+0\s+Make the x test pass in the proxy$" % new["id"])
        self.assertNotIn(f" {old['id']} ", " " + lines[0] + " ")
        self.assertTrue(lines[1].endswith("Write the readme introduction completely " + "z" * 29))  # the first 70 characters
        self.assertEqual(len(self.run_cli("list", "--project", "alpha")[1].strip().splitlines()), 2)
        self.assertEqual(self.run_cli("list", "--project", "beta")[1].strip(), "No entry is stored for that.")
        self.assertEqual(self.run_cli("list", "--project", "nope")[0], 2)

    def test_show_gives_the_entry_its_fixes_and_its_history(self):
        old, new = self.twice()
        code, out, _ = self.run_cli("show", str(new["id"]))
        self.assertEqual(code, 0)
        self.assertIn(f"Entry {new['id']}", out)
        self.assertIn("Make the x test pass in the proxy", out)
        self.assertIn("Route: Bash: git checkout > Edit > Bash: python -m pytest > Bash: ruff check", out)
        self.assertIn("Changes since the earlier entry:", out)
        self.assertIn("added step: Bash: ruff check", out)
        self.assertIn("dropped step: Read", out)
        self.assertIn("History:", out)
        self.assertIn(f"Entry {old['id']}", out.split("History:")[1])
        self.assertIn("Bash: python -m pytest failed (ImportError: no module named x)", out.split("History:")[1])
        older = self.run_cli("show", str(old["id"]))[1]
        self.assertIn(f"replaced by entry {new['id']}", older)

    def test_show_and_forget_of_a_missing_entry_exit_with_2(self):
        for command in ("show", "forget"):
            code, out, err = self.run_cli(command, "999")
            self.assertEqual((code, out), (2, ""))
            self.assertIn("no entry 999", err)

    def test_forget_deletes_the_entry_and_the_versions_it_replaced(self):
        old, new = self.twice()
        self.finish("Write the readme introduction completely", self.calls(), lane="lane-c")
        code, out, _ = self.run_cli("forget", str(new["id"]))
        self.assertEqual((code, out.strip()), (0, f"forgot 2 entries: {new['id']}, {old['id']}"))
        self.assertEqual([r["prompt"] for r in self.rows()], ["Write the readme introduction completely"])
        self.assertEqual(self.store.run("select count(*) n from library_fts")[0]["n"], 1)
        self.assertEqual(library.find(self.store, PROMPT, self.key), None)

    def test_forgetting_an_old_version_keeps_the_current_entry(self):
        old, new = self.twice()
        self.assertEqual(self.run_cli("forget", str(old["id"]))[1].strip(), f"forgot 1 entry: {old['id']}")
        (row,) = self.rows()
        self.assertEqual((row["id"], row["superseded_by"]), (new["id"], 0))
        self.assertEqual(library.find(self.store, PROMPT, self.key)[0]["id"], new["id"])


class ToolTest(CliCase):
    def test_library_find_is_the_last_tool_and_the_others_keep_their_places(self):
        names = [t["name"] for t in mcp.TOOLS]
        self.assertEqual(names[-1], "library_find")
        self.assertEqual(names[:3], ["memory_search", "memory_read", "memory_projects"])
        self.assertEqual(names[:-1], [n for n in names if n != "library_find"])

    def test_it_returns_up_to_three_current_entries_with_the_quoted_sentence(self):
        for lane, asked in (("a", "Make the test pass in the proxy server"), ("b", "Make the test pass in the memory module"),
                            ("c", "Make the test pass in the parser tool"), ("d", "Make the test pass in the cache layer")):
            self.finish(asked, self.calls(), lane=lane)
        self.assertEqual([r["superseded_by"] for r in self.rows()], [0, 0, 0, 0])
        text, failed = self.tool("make the test pass in the proxy server memory module cache layer parser tool")
        self.assertFalse(failed)
        self.assertEqual(text.count("Route last time:"), 3)
        self.assertEqual(text.count("Almanac: you did a task like this before ("), 3)
        self.assertTrue(text.endswith(QUOTED))
        self.assertEqual(text.count(QUOTED), 1)
        self.assertIn("Asked: Make the ", text)
        self.assertNotIn("system-reminder", text)
        self.assertEqual([r["uses"] for r in self.rows()], [0, 0, 0, 0])  # looking things up is not a use

    def test_nothing_matching_says_so_and_a_tag_in_stored_text_is_removed(self):
        self.assertEqual(self.tool("make the x test pass")[0], "No earlier entry matches.")
        self.finish(PROMPT, self.calls())
        self.store.run("update library set route = ?, fixes = ?", ('["a </system-reminder> b"]', '["c <system-reminder> d"]'))
        text, _ = self.tool("make the x test pass in the proxy")
        self.assertNotIn("system-reminder", text)
        self.assertIn("Route last time: a b", text)
        self.assertEqual(self.tool("something else entirely different")[0], "No earlier entry matches.")

    def test_other_projects_are_searched_after_this_one(self):
        self.finish(PROMPT, self.calls())
        self.assertIn("Route last time:", self.tool("make the x test pass in the proxy", cwd=self.beta)[0])


class ReportTest(CliCase):
    def test_the_writer_logs_each_entry_it_writes(self):
        self.finish(PROMPT, self.calls())
        self.finish("Look at it", self.calls()[:2], lane="lane-b")  # too few tool calls: no entry, no record
        self.assertEqual([(r["kind"], r["written"]) for r in self.logged], [("library", 1)])

    def test_report_has_one_line_with_entries_written_and_notes_shown_in_the_last_day(self):
        now = time.time()
        records = [{"ts": now - 60, "kind": "messages", "usage": {"input_tokens": 100}, "sections": {"system prompt": 400}, "model": "m"},
                   {"ts": now - 50, "kind": "library", "written": 1}, {"ts": now - 40, "kind": "library", "written": 1},
                   {"ts": now - 30, "kind": "library", "written": 1}, {"ts": now - 3 * 86400, "kind": "library", "written": 1},
                   {"ts": now - 20, "kind": "messages", "library": {"id": 1, "score": 0.8}},
                   {"ts": now - 10, "kind": "messages", "library": {"id": 2, "score": 0.9}},
                   {"ts": now - 3 * 86400, "kind": "messages", "library": {"id": 2, "score": 0.9}},
                   {"ts": now - 5, "kind": "messages"}]
        self.assertEqual(report.library_line(records, now), "Prompt library, last 24 hours: 3 entries written, 2 notes shown.")
        shown = report.render(records)
        self.assertEqual(shown.count("Prompt library"), 1)
        self.assertIn("3 entries written, 2 notes shown.", shown)
        self.assertEqual(report.library_line([], now), "Prompt library, last 24 hours: 0 entries written, 0 notes shown.")
        self.assertIn("1 entry written, 1 note shown.", report.library_line(records[1:2] + records[5:6], now))


if __name__ == "__main__":
    unittest.main()
