"""
agent/packaging/assert_dpapi_executed.py — the real-DPAPI execution assertion (D-20).

Standalone, stdlib-only (xml.etree.ElementTree, no third-party dependency). Takes
one argument, the path to a junit-xml report, and exits non-zero when EITHER zero
`dpapi`-marked tests were collected at all, OR any of the collected ones was
SKIPPED rather than executed.

WHY THIS ASSERTS ON THE REPORT, NEVER ON PYTEST'S EXIT CODE
-------------------------------------------------------------
The real-DPAPI test is skipped off-Windows by design (`@pytest.mark.skipif(not
sys.platform.startswith("win"), ...)`) — that is correct behaviour on a
non-Windows developer machine. But for that very same reason, the identical skip
will fire SILENTLY inside CI too if the runner configuration is ever accidentally
changed away from a Windows runner (a copy-paste into a Linux job, a matrix
expansion gone wrong). `pytest`'s own exit code stays 0 on a skipped test — skips
do not fail a run by default — so checking the exit code alone cannot tell a
"the DPAPI round trip was actually proven to work this run" build from a "the one
test that proves it never even ran" build. A green build with an unexecuted test
is the worst possible outcome, because it looks like proof. This script exists
so the CI step fails on that report shape specifically, not merely on a nonzero
pytest exit code.

USAGE
-----
    python packaging/assert_dpapi_executed.py <path-to-junit-xml-report>

Exits 0 only if at least one `dpapi`-marked test was collected AND none of the
collected ones was skipped. Exits non-zero (1) in every other case, including a
missing or unparseable report file — never raises an unhandled traceback for
that case, so the calling CI step's failure reason is always this script's own
printed message, not a bare Python stack trace.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def _sum_attr(root: ET.Element, attr: str) -> int:
    """
    Sum the integer value of `attr` (e.g. "tests" or "skipped") across every
    <testsuite> element found in the document. A junit-xml report's root is
    either a single <testsuite ...> element, or a <testsuites> element
    wrapping one or more <testsuite ...> children — pytest's own --junitxml
    output uses the latter shape, but this function accepts either so the
    script does not silently misread a differently-shaped report.
    """
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    total = 0
    for suite in suites:
        value = suite.get(attr)
        if value is None:
            continue
        total += int(value)
    return total


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: assert_dpapi_executed.py <junit-xml-path>", file=sys.stderr)
        return 2

    path = argv[1]

    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        # Missing file, unreadable file, or malformed XML all land here —
        # this is a FAIL, not a crash: a missing report means the dpapi suite
        # never ran at all, which is exactly the case this script exists to
        # catch, not a reason to raise an unhandled traceback.
        print(f"FAIL: could not read/parse junit-xml report at {path!r}: {exc}")
        return 1

    root = tree.getroot()
    tests = _sum_attr(root, "tests")
    skipped = _sum_attr(root, "skipped")

    if tests == 0:
        print("FAIL: no dpapi-marked tests were collected at all")
        return 1

    if skipped > 0:
        print(f"FAIL: {skipped} dpapi-marked test(s) were SKIPPED, not executed")
        return 1

    print(f"OK: {tests} dpapi-marked test(s) executed, 0 skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
