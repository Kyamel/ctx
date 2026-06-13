"""Thin wrappers around git via subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path) -> str | None:
    """Run a git command and return stdout, suppressing unavailable-git errors."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def is_repo(cwd: Path) -> bool:
    """True if *cwd* is inside a git working tree."""
    return _run(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def current_commit(cwd: Path) -> str | None:
    """Return the current HEAD commit hash, or None if unavailable."""
    return _run(["rev-parse", "HEAD"], cwd)
