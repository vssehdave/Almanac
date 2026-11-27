import importlib.util
import re
import unittest
from pathlib import Path

EGM = Path(__file__).resolve().parent.parent / "src" / "getalmanac" / "bundle" / "egm"
ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _md_files():
    return sorted(EGM.rglob("*.md"))


def _load_plan_mode():
    spec = importlib.util.spec_from_file_location("plan_mode", EGM / "scripts" / "plan_mode.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_agents_set_their_model(self):
        # reviewers on Sonnet so a full review is not ten Opus agents at once
        want = {"synthesizer": "opus", "bootstrap_orchestrator": "haiku"}
        for f in sorted((EGM / "agents").glob("*.md")):
            head = f.read_text(encoding="utf-8").split("---")[1]
            self.assertRegex(head, r"(?m)^model: %s$" % want.get(f.stem, "sonnet"), f.name)

    def test_reviewers_list_memory_tools(self):
        reviewers = ["design_critic", "implementation_readiness", "zoom_out", "codebase_details",
                     "comprehension", "inventive_catalyst", "structural", "style"]
        for name in reviewers:
            text = (EGM / "agents" / f"{name}.md").read_text(encoding="utf-8")
            tools = re.search(r"(?m)^tools: \[(.*)\]$", text).group(1)
            for tool in ("memory_search", "memory_read", "code_history", "code_related"):
                with self.subTest(agent=name, tool=tool):
                    self.assertIn(f"mcp__almanac__{tool}", tools)
            self.assertIn("open files only for what memory cannot answer", " ".join(text.split()), name)

    def test_review_skills_name_roster_and_shared_packet(self):
        for skill in ("egm-review", "egm-review-loop"):
            text = (EGM / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            with self.subTest(skill=skill):
                self.assertIn("## Default roster", text)
                for agent in ("egm-design", "egm-implementation-readiness", "egm-zoom-out", "egm-codebase-details", "egm-synthesizer"):
                    self.assertIn(agent, text)
                self.assertIn("## Shared context packet", text)
                for tool in ("memory_pins", "memory_search", "code_history"):
                    self.assertIn(tool, text)

    def test_egm_review_plan_mode_section_precedes_constraints(self):
        text = (EGM / "skills" / "egm-review" / "SKILL.md").read_text(encoding="utf-8")
        plan = text.find("## Plan mode")
        self.assertNotEqual(plan, -1)
        self.assertLess(plan, text.find("## Execution Constraints"))

    def test_egm_review_plan_mode_section_carries_nudge(self):
        text = (EGM / "skills" / "egm-review" / "SKILL.md").read_text(encoding="utf-8")
        start = text.find("## Plan mode")
        self.assertNotEqual(start, -1)
        end = text.find("\n## ", start)
        section = text[start:] if end == -1 else text[start:end]
        self.assertIn(_load_plan_mode().NUDGE, section)


if __name__ == "__main__":
    unittest.main()
