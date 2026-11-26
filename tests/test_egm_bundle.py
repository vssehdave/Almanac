import re
import unittest
from pathlib import Path

EGM = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "egm"
ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _md_files():
    return sorted(EGM.rglob("*.md"))


class EgmBundleLinks(unittest.TestCase):
    def test_plugin_root_paths_resolve(self):
        for f in _md_files():
            for ref in ROOT_REF.findall(f.read_text(encoding="utf-8")):
                if "<" in ref or "{" in ref:
                    continue  # a pattern such as agents/<file>.md, not a path
                with self.subTest(file=f.name, ref=ref):
                    self.assertTrue((EGM / ref.rstrip(".,")).exists())

    def test_relative_markdown_links_resolve(self):
        for f in _md_files():
            for link in MD_LINK.findall(f.read_text(encoding="utf-8")):
                if re.match(r"[a-z]+:|#|/", link) or re.search(r"[\[$<{]", link):  # template placeholder
                    continue
                target = link.split("#")[0]
                if target:
                    with self.subTest(file=f.name, link=link):
                        self.assertTrue((f.parent / target).exists())

    def test_no_reference_to_a_missing_agents_file(self):
        for f in _md_files():
            for name in re.findall(r"agents/([\w-]+\.md)", f.read_text(encoding="utf-8")):
                with self.subTest(file=f.name, agent=name):
                    self.assertTrue((EGM / "agents" / name).is_file())

    def test_agent_names_resolve_to_files(self):
        for f in sorted((EGM / "agents").glob("*.md")):
            self.assertIn(f"name: egm-", f.read_text(encoding="utf-8"), f.name)
        self.assertFalse((EGM / ".claude-plugin" / "agents").exists())

    def test_agents_have_no_model_pin(self):
        for f in sorted((EGM / "agents").glob("*.md")):
            head = f.read_text(encoding="utf-8").split("---")[1]
            self.assertNotRegex(head, r"(?m)^model:", f.name)


if __name__ == "__main__":
    unittest.main()
