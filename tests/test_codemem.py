import contextlib
import io
import os
import unittest

from test_memory import Case, human, reply

from getalmanac import cli, codemem, mcp, project
from getalmanac.memory import Memory
from getalmanac.store import State, Store


def use(call, name, **data):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": call, "name": name, "input": data}]}


def out(call, text="ok", error=False):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": text, **({"is_error": True} if error else {})}]}


class CodeMemoryTest(Case):
    def setUp(self):
        super().setUp()
        self.file = self.alpha + "/src/app.py"
        self.messages = [human("Fix the app"),
                         use("e1", "Edit", file_path=self.file, old_string="a = 1", new_string="a = 2\nb = 3"), out("e1"),
                         use("w1", "Write", file_path=self.alpha + "/src/new.py", content="x = 1\n"), out("w1"),
                         use("e2", "Edit", file_path=self.file, old_string="zzz", new_string="y", ), out("e2", "String not found", True)]

    def index(self, messages=None, lane="lane-1"):
        self.memory.index(lane, "s", self.memory.projects.see(self.alpha), messages or self.messages, reply("Done."))

    def rows(self, path=None):
        return codemem.history(self.store, path or self.file)

    def test_edit_and_write_are_recorded_and_the_failed_edit_is_not(self):
        self.index()
        (edit,) = self.rows()
        self.assertEqual((edit["kind"], edit["status"], edit["tool_use_id"]), ("edit", "ok", "e1"))
        self.assertIn("-1/+2 lines", codemem.render(self.rows()))
        (write,) = self.rows(self.alpha + "/src/new.py")
        self.assertEqual((write["kind"], write["content"], write["size"]), ("write", "x = 1", 6))
        node = self.store.run("select id, convo from node where kind = 'leaf'")[0]
        self.assertEqual((edit["node"], edit["convo"]), (node["id"], node["convo"]))
        self.assertTrue(edit["ts"].endswith("+00:00"))

    def test_recording_again_writes_nothing_new(self):
        self.index()
        self.index(self.messages + [use("e3", "Edit", file_path=self.file, old_string="b", new_string="c"), out("e3")] )
        self.index()
        self.assertEqual([r["tool_use_id"] for r in self.rows()], ["e3", "e1"])
        self.assertEqual(len(self.store.run("select 1 from codeedit")), 3)

    def test_a_large_write_keeps_only_its_hash(self):
        big = "x" * (codemem.KEEP + 1)
        self.index([human("Make it"), use("w", "Write", file_path=self.file, content=big), out("w")])
        (row,) = self.rows()
        self.assertIsNone(row["content"])
        self.assertEqual((len(row["chash"]), row["size"]), (40, len(big)))
        self.assertIn("content not kept", codemem.render(self.rows()))

    def test_a_call_without_a_result_is_unknown_and_an_error_result_removes_it(self):
        codemem.capture(self.store, 1, "p", 1, [use("u", "Edit", file_path=self.file, old_string="a", new_string="b")], 0)
        self.assertEqual(self.rows()[0]["status"], "unknown")
        self.assertIn("result not seen", codemem.render(self.rows()))
        codemem.capture(self.store, 1, "p", 1, [use("u", "Edit", file_path=self.file, old_string="a", new_string="b"), out("u", "no", True)], 0)
        self.assertEqual(self.rows(), [])

    def test_multiedit_and_notebook_and_odd_input(self):
        self.index([human("Go"), use("m", "MultiEdit", file_path=self.file, edits=[
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]), out("m"),
            use("n", "NotebookEdit", notebook_path=self.alpha + "/n.ipynb", new_source="print(1)"), out("n"),
            use("bad", "Edit", file_path=self.file, old_string=None), out("bad")])
        self.assertIn("2 edit(s)", codemem.render(self.rows()))
        self.assertEqual(self.rows(self.alpha + "/n.ipynb")[0]["kind"], "edit")
        self.assertEqual(len(self.rows()), 1)

    def test_paths_are_spelled_one_way(self):
        self.index([human("Go"), use("a", "Edit", file_path=self.file.replace("/", "\\"), old_string="a", new_string="b"), out("a")])
        self.assertEqual(self.rows()[0]["path"], project.normalize(self.file))
        self.assertEqual(len(self.rows(self.file.replace("/", "\\"))), 1)

    def test_mcp_tool_and_cli_print_the_same_history(self):
        self.index()
        text = mcp.call(self.memory, "code_history", {"path": "src/app.py", "limit": 5}, self.alpha)
        self.assertIn(f"exchange {self.rows()[0]['node']}", text)
        self.assertEqual(text, codemem.render(self.rows(), self.file))
        self.assertIn("No recorded changes", mcp.call(self.memory, "code_history", {"path": "nope.py"}, self.alpha))
        self.assertIn("needs a path", mcp.call(self.memory, "code_history", {}, self.alpha))
        self.assertIn("code_history", [t["name"] for t in mcp.TOOLS])

    def test_cli(self):
        self.index()
        self.store.close()
        old = os.environ.get("ALMANAC_HOME")
        os.environ["ALMANAC_HOME"] = self.tmp
        self.addCleanup(lambda: os.environ.pop("ALMANAC_HOME") if old is None else os.environ.update(ALMANAC_HOME=old))
        os.replace(os.path.join(self.tmp, "a.db"), os.path.join(self.tmp, "almanac.db"))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli.main(["code", "history", self.file, "-n", "1"]), 0)
        self.assertIn("exchange", buffer.getvalue())
        self.assertIn("1 edit(s)", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
