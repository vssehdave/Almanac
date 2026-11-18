"""Security pass 2: linear-time cleaning, what recall may quote, typed-text detection, redaction, hand-off signing."""
import time
import unittest

from getalmanac.memory import clean


class CleanSpeedTest(unittest.TestCase):
    def test_a_tag_opener_followed_by_spaces_is_not_quadratic(self):
        start = time.time()
        clean("<" + " " * 100000)
        self.assertLess(time.time() - start, 0.5)

    def test_many_begin_lines_without_an_end_are_not_quadratic(self):
        start = time.time()
        clean("-----BEGIN PRIVATE KEY-----\n" * 8000)
        self.assertLess(time.time() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
