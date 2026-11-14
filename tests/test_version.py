import io
import unittest
from contextlib import redirect_stdout

from getalmanac import __version__, cli


class VersionTest(unittest.TestCase):
    def test_version_command_prints_version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["version"]), 0)
        self.assertIn(__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()
