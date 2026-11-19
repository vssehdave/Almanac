import json
import unittest

from helpers import Rig

from getalmanac import config, startup


class StubZeroTest(unittest.TestCase):
    def test_zero_keeps_no_description_even_a_short_one(self):
        self.assertEqual(startup.stub("Short one.", 0), "")
        self.assertEqual(startup.stub("Short one.", 90), "Short one.")

    def test_the_default_cuts_at_a_word_and_leaves_no_trailing_punctuation(self):
        cut = startup.stub("Create or edit a PowerPoint slide deck from an outline, with speaker notes; " * 3, 60)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(len(cut), 61)
        self.assertNotRegex(cut[:-1], r"[,.;:\s-]$")
        self.assertTrue(" ".join(cut[:-1].split()) in " ".join(("Create or edit a PowerPoint slide deck from an outline, with speaker notes; " * 3).split()))

    def test_zero_lists_names_only_and_a_match_shows_the_full_text(self):
        self.assertEqual(config.DEFAULTS["skills"]["stub_chars"], 60)
        more = " It also covers many adjacent situations that take a lot of words to describe in full." * 3
        skills = {"slides": "Create or edit a PowerPoint slide deck from an outline." + more, "tiny": "Short one.",
                  "pdf-tools": "Read, merge or fill PDF forms." + more, "charts": "Build a chart or plot from tabular data." + more}
        listing = startup.SKILL_MARK + "\n\n" + "\n".join(f"- {n}: {d}" for n, d in skills.items())
        body = {"model": "m", "system": "s", "tools": [],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "<system-reminder>r</system-reminder>"},
                                                           {"type": "text", "text": "make a slide deck for the board"}]},
                             {"role": "system", "content": [{"type": "text", "text": "Hi\n\n" + listing + "\n\nEnd."}]}]}
        rig = Rig(layers=[startup.SkillsLayer()], overrides={"skills.stub_chars": 0})
        self.addCleanup(rig.close)
        rig.messages(body)
        sent = rig.sent()["messages"]
        text = sent[1]["content"][0]["text"]
        self.assertIn("\n- slides\n- tiny\n", text)
        self.assertNotIn("- slides:", text)
        self.assertNotIn("…", text)
        self.assertIn("- slides: " + skills["slides"], json.dumps(sent[0]))  # shown in full beside the message that matched
        self.assertTrue(text.endswith("End."))


if __name__ == "__main__":
    unittest.main()
