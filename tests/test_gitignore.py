"""Tests for .gitignore rules to prevent regression on ignored file patterns."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = ROOT / ".gitignore"

SIDECAR_PATTERNS = [
    ("*.db-shm", "interpretation.db-shm"),
    ("*.db-wal", "interpretation.db-wal"),
    ("*.db-journal", "interpretation.db-journal"),
]


def test_sqlite_sidecar_patterns_defined_in_gitignore() -> None:
    lines = {line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()}
    for pattern, _ in SIDECAR_PATTERNS:
        assert pattern in lines, f"Expected {pattern} to be defined in .gitignore"


@pytest.mark.parametrize(("pattern", "sample_file"), SIDECAR_PATTERNS)
def test_sqlite_sidecar_files_ignored_by_git(pattern: str, sample_file: str) -> None:
    git_bin = shutil.which("git")
    if not git_bin or not (ROOT / ".git").exists():
        pytest.skip("git binary or .git repository not available")

    res = subprocess.run(
        [git_bin, "check-ignore", "-v", sample_file],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Expected '{sample_file}' to be ignored by git, but it was not"

    # Verbose output format is: <source>:<lineno>:<pattern>\t<pathname>
    parts = res.stdout.strip().split("\t")
    assert len(parts) >= 2, f"Unexpected check-ignore output format: {res.stdout}"
    source_info = parts[0].split(":")
    assert len(source_info) >= 3, f"Unexpected source format in check-ignore: {parts[0]}"
    matched_pattern = source_info[2]
    assert matched_pattern == pattern, (
        f"Expected '{sample_file}' to be ignored specifically by rule '{pattern}', "
        f"but matched '{matched_pattern}' instead"
    )

