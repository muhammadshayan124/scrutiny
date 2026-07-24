"""Fetches and parses unified diffs -- from a local git repo or a GitHub PR --
into a small internal representation that's easy to feed to an LLM and easy
to use later for locating the actual function a finding points at.

Parsing itself is delegated to `unidiff`, a well-tested library for exactly
this format's edge cases (renames, binary files, no-newline-at-eof markers),
rather than hand-rolling a diff parser.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from unidiff import PatchSet


class DiffFetchError(RuntimeError):
    """Raised when a diff could not be fetched from git or GitHub."""


@dataclass(frozen=True)
class Hunk:
    header: str
    text: str
    added_line_numbers: list[int]


@dataclass(frozen=True)
class FileDiff:
    path: str
    is_new_file: bool
    is_deleted_file: bool
    hunks: list[Hunk]


def get_local_diff(repo_path: str, diff_args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo_path, "diff", *diff_args],
        capture_output=True,
        text=True,
    )
    # `git diff` returns 0 regardless of whether there are changes, but
    # `--no-index` (and `--exit-code`) instead follow classic diff(1)
    # semantics: 1 means "differences found," not an error. Only 2+ is real.
    if result.returncode >= 2:
        raise DiffFetchError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def get_pr_diff(pr_url: str) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", pr_url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DiffFetchError(f"gh pr diff failed: {result.stderr.strip()}")
    return result.stdout


def parse_diff(diff_text: str) -> list[FileDiff]:
    patch_set = PatchSet(diff_text)
    file_diffs = []
    for patched_file in patch_set:
        hunks = []
        for hunk in patched_file:
            lines = [str(line) for line in hunk]
            added_line_numbers = [
                line.target_line_no
                for line in hunk
                if line.is_added and line.target_line_no is not None
            ]
            hunks.append(
                Hunk(
                    header=str(hunk).splitlines()[0] if str(hunk) else "",
                    text="".join(lines),
                    added_line_numbers=added_line_numbers,
                )
            )
        file_diffs.append(
            FileDiff(
                path=patched_file.path,
                is_new_file=patched_file.is_added_file,
                is_deleted_file=patched_file.is_removed_file,
                hunks=hunks,
            )
        )
    return file_diffs
