"""Tests for the update-check version comparison and git-checkout detection.

Pure logic only: no network, no server, no real git operations. Verifies
the MAJOR.MINOR[.BUILD] comparison rule described in README's Versioning
section, and that is_git_checkout() reads the real filesystem correctly.

Run with:  .venv/bin/python -m tests.test_update_check
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import update_check as U

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def test_version_tuple_parses_dotted_integers():
    check("plain version", U._version_tuple("1.01") == (1, 1))
    check("build suffix", U._version_tuple("1.01.1") == (1, 1, 1))
    check("single component", U._version_tuple("2") == (2,))


def test_version_tuple_stops_at_non_numeric_junk():
    # A stray "v" prefix or pre-release suffix shouldn't crash the compare -
    # just stop parsing at the first piece that isn't a plain integer.
    check("stops at non-numeric", U._version_tuple("1.01.rc1") == (1, 1))


def test_is_newer_handles_build_suffix():
    check("same minor, has a build", U._is_newer("1.01.1", "1.01"))
    check("same version is not newer", not U._is_newer("1.01", "1.01"))
    check("older is not newer", not U._is_newer("1.00", "1.01"))
    check("higher minor", U._is_newer("1.02", "1.01"))
    check("higher major beats higher local build",
          U._is_newer("2.00", "1.01.5"))


def test_is_newer_is_symmetric_reasonable():
    check("build vs no-build, equal otherwise",
          not U._is_newer("1.01", "1.01.1"))


def test_is_git_checkout_reads_the_filesystem():
    with tempfile.TemporaryDirectory() as tmp:
        # Point PATHS.root at a throwaway directory rather than mutating the
        # real repo's own .git - is_git_checkout() only reads PATHS.root.
        original_root = U.PATHS.root
        try:
            U.PATHS = U.PATHS.__class__(root=Path(tmp))
            check("no .git -> not a checkout", not U.is_git_checkout())
            (Path(tmp) / ".git").mkdir()
            check("a .git dir -> is a checkout", U.is_git_checkout())
        finally:
            U.PATHS = U.PATHS.__class__(root=original_root)


def main() -> int:
    tests = [
        test_version_tuple_parses_dotted_integers,
        test_version_tuple_stops_at_non_numeric_junk,
        test_is_newer_handles_build_suffix,
        test_is_newer_is_symmetric_reasonable,
        test_is_git_checkout_reads_the_filesystem,
    ]
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
