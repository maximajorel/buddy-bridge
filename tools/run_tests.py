"""Run every host-side check for the bridge in one command.

The bridge is stdlib-only Python, so the suite is plain `unittest` discovered
under `tools/tests/` (no pip, no venv: the LXC has neither). This runner exists
for the same reason the firmware's does: a test that silently stops being
collected looks like a test that passes, so the count is printed and compared
against what was discovered.

    python tools/run_tests.py              # everything
    python tools/run_tests.py --filter config
    python tools/run_tests.py --verbose

Exit code is 0 only when every test passed.
"""

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge"
TESTS = ROOT / "tools" / "tests"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filter", default="", help="only run tests whose name contains this")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    for path in (BRIDGE, TESTS):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    loader = unittest.TestLoader()
    if args.filter:
        loader.testNamePatterns = [f"*{args.filter}*"]
    suite = loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))

    if loader.errors:
        for err in loader.errors:
            print(err)
        print("\nFAIL import errors while collecting tests")
        return 1

    count = suite.countTestCases()
    if count == 0:
        print(f"FAIL no tests discovered in {TESTS} (filter={args.filter!r})")
        return 1

    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1,
                                     stream=sys.stdout).run(suite)
    print()
    print(f"{count - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures)} failed, {len(result.errors)} errors")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
