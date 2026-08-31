#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""House rules that hold for the whole repository rather than for one module."""

import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]

# Spelled as an escape rather than as itself, so this file passes its own test
# and the rule needs no exception to be able to state itself.
EM_DASH = "\u2014"

# Files that are allowed to carry one, and none is: the entry exists so that
# adding an exception is an edit somebody has to justify here rather than a
# character that slips in unnoticed.
EM_DASH_ALLOWED: frozenset[str] = frozenset()


def repository_files() -> list[Path]:
    """Every file the repository carries, tracked or newly written but not ignored.

    The working tree and not just the index, because a rule that only applies
    once a file is staged is a rule that greets the author at the commit rather
    than at the test they were already running.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY, capture_output=True, text=True, check=False,
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout, so there is no file list to hold to this")
    return [REPOSITORY / name for name in listed.stdout.split("\0") if name]


class TestTheHouseRules:
    """Stated in the contributing notes, and now checked rather than remembered."""

    def test_no_file_carries_an_em_dash(self):
        """The rule was already the rule and had already lapsed twice, in the
        ruff ignore comments and in .gitignore, because nothing looked. Prose,
        comments, log lines and documentation alike: one character, one check."""
        offenders = sorted(
            f"{path.relative_to(REPOSITORY)}:{number}"
            for path in repository_files()
            if path.is_file() and path.name not in EM_DASH_ALLOWED
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
            )
            if EM_DASH in line
        )
        assert offenders == []


def main():
    pass


if __name__ == "__main__":
    main()
