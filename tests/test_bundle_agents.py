import re
import unittest
from pathlib import Path

from getalmanac.cli import COMMANDS

BUNDLE = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "almanac"
AGENTS = sorted((BUNDLE / "agents").glob("*.md"))
SKILLS = sorted((BUNDLE / "skills").glob("*/SKILL.md"))
TICKED = re.compile(r"`([^`\n]+)`")
SKILL_REF = re.compile(r"^(\w[\w-]*):([\w-]+)$")
CMD_REF = re.compile(r"^almanac (\w[\w-]*)")
JS_FILE = re.compile(r"\w\.tsx?\b")
READ_ONLY = ("planner", "agent-evaluator", "loop-operator", "silent-failure-hunter", "surveyor")


def _front(path):
    parts = path.read_text(encoding="utf-8").split("---", 2)
    fields = {}
    for line in parts[1].splitlines() if len(parts) == 3 else []:
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
    return fields


class BundleAgents(unittest.TestCase):
    def test_agents_exist(self):
        self.assertTrue(AGENTS)

    def test_frontmatter(self):
        for f in AGENTS:
            with self.subTest(agent=f.name):
                head = _front(f)
                for key in ("name", "description", "model", "effort"):
                    self.assertTrue(head.get(key), key)
                self.assertEqual(head.get("name"), f.stem)

    def test_skill_references(self):
        for f in AGENTS + SKILLS:
            for span in TICKED.findall(f.read_text(encoding="utf-8")):
                m = SKILL_REF.match(span)
                if m and span != "plugin:skill":  # the adopt skill names the form itself
                    with self.subTest(file=str(f.relative_to(BUNDLE)), ref=span):
                        self.assertEqual(m.group(1), "almanac")
                        self.assertTrue((BUNDLE / "skills" / m.group(2) / "SKILL.md").is_file() or (BUNDLE / "agents" / f"{m.group(2)}.md").is_file(), m.group(0))

    def test_no_foreign_plugin_names(self):
        for f in AGENTS:
            for span in TICKED.findall(f.read_text(encoding="utf-8")):
                with self.subTest(agent=f.name, ref=span):
                    self.assertFalse(re.match(r"(ponytail:|crew)", span))

    def test_cli_commands_exist(self):
        for f in AGENTS + SKILLS:
            for span in TICKED.findall(f.read_text(encoding="utf-8")):
                m = CMD_REF.match(span)
                if m:
                    with self.subTest(file=str(f.relative_to(BUNDLE)), ref=span):
                        self.assertIn(m.group(1), COMMANDS)

    def test_no_javascript_leftovers(self):
        # shortcut: ai-regression-testing is a web-testing guide kept as is; drop it here once it is rewritten or removed
        for f in [f for f in AGENTS + SKILLS if f.parent.name != "ai-regression-testing"]:
            text = f.read_text(encoding="utf-8")
            with self.subTest(file=str(f.relative_to(BUNDLE))):
                self.assertNotIn("npm ", text)
                self.assertNotIn(".catch(", text)
                self.assertIsNone(JS_FILE.search(text))

    def test_skill_tool_listed(self):
        for f in AGENTS:
            tools = _front(f).get("tools")
            names = [n for n in re.findall(r"almanac:([\w-]+)", f.read_text(encoding="utf-8")) if (BUNDLE / "skills" / n).is_dir()]
            if tools and names:
                with self.subTest(agent=f.name):
                    self.assertIn("Skill", [t.strip() for t in tools.split(",")])

    def test_read_only_agents(self):
        for name in READ_ONLY:
            with self.subTest(agent=name):
                tools = _front(BUNDLE / "agents" / f"{name}.md").get("tools", "")
                self.assertFalse({"Edit", "Write"} & {t.strip() for t in tools.split(",")})

    def test_no_foreign_text(self):
        for f in AGENTS:
            text = f.read_text(encoding="utf-8")
            with self.subTest(agent=f.name):
                self.assertNotIn("ECC", text)

    def test_skill_name_matches_folder(self):
        for f in SKILLS:
            with self.subTest(skill=f.parent.name):
                self.assertEqual(_front(f).get("name"), f.parent.name)


if __name__ == "__main__":
    unittest.main()
