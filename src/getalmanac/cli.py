"""The `almanac` command."""
import sys

from . import __version__


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["version"]:
        print(f"almanac {__version__}")
        return 0
    print("almanac: nothing to run yet", file=sys.stderr)
    return 2
