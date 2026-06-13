"""Scanner protocol, file discovery and shared helpers."""

from __future__ import annotations

import hashlib
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from ..config import ALWAYS_PRUNE, Config
from ..models import Symbol


# @ctx tag: scanner contract, symbol identity, change detection
def hash_text(text: str | None) -> str:
    """Return the stable content hash used to detect symbol/doc changes."""
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# @ctx related: Symbol, PythonScanner, UniversalCtxScanner
def symbol_id(file_path: str, qualified_name: str, kind: str) -> str:
    """Return a deterministic id so the same symbol keeps identity across scans."""
    return hashlib.sha1(f"{file_path}::{qualified_name}::{kind}".encode()).hexdigest()


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-ish glob, including `**`, into a regex."""
    i, n, out = 0, len(pattern), []
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(rel: str, patterns: list[re.Pattern[str]]) -> bool:
    """Return whether a relative path matches any compiled include/exclude pattern."""
    return any(p.match(rel) for p in patterns)


# @ctx tag: file discovery, scan configuration
# @ctx note: New language adapters only run when config.include covers their extensions.
def discover_files(config: Config) -> list[Path]:
    """Return source files under the project root honoring include/exclude globs."""
    includes = [_glob_to_regex(p) for p in config.include]
    excludes = [_glob_to_regex(p) for p in config.exclude]
    root = config.root
    found: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune unwanted directories in-place for efficiency.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ALWAYS_PRUNE
            and not _matches_any(_rel(root, Path(dirpath) / d) + "/", excludes)
        ]
        for fname in filenames:
            abs_path = Path(dirpath) / fname
            rel = _rel(root, abs_path)
            if _matches_any(rel, excludes):
                continue
            if includes and not _matches_any(rel, includes):
                continue
            found.append(abs_path)
    return sorted(found)


def _rel(root: Path, path: Path) -> str:
    """Return a POSIX relative path from *root* to *path*."""
    return path.relative_to(root).as_posix()


# @ctx tag: scanner adapter, language extension point
# @ctx related: PythonScanner, scan, Symbol
class Scanner(ABC):
    """Contract for language-aware symbol extraction adapters.

    Implementations decide whether they handle a file and yield normalized
    `Symbol` rows. The CLI owns file discovery, persistence and marker binding.
    """

    language: str = "unknown"

    @abstractmethod
    def can_handle(self, path: Path) -> bool:
        """Whether this scanner understands the given file."""

    @abstractmethod
    def scan(self, path: Path, rel_path: str, source: str) -> Iterator[Symbol]:
        """Yield symbols extracted from *source*."""
