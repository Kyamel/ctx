"""Project location and configuration handling."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CTX_DIRNAME = ".ctx"
DB_FILENAME = "context.db"
CONFIG_FILENAME = "config.toml"

DEFAULT_CONFIG_TOML = """\
[scan]
include = ["**/*.py"]
exclude = [".git/**", ".ctx/**", ".venv/**", "__pycache__/**", "dist/**", "build/**"]

[context]
default_limit = 8

[markers]
prefix = "@ctx"
"""

# Directory names that are always pruned while walking, regardless of config.
# @ctx tag: scan configuration, file discovery
# @ctx note: Language support needs a registered scanner and matching scan.include globs.
# @ctx related: discover_files, Scanner, scan
ALWAYS_PRUNE = {".git", ".ctx", ".venv", "venv", "node_modules", "__pycache__"}


class CtxError(Exception):
    """User-facing error with a friendly message."""


@dataclass
class Config:
    """Parsed configuration plus resolved project paths."""

    root: Path
    include: list[str] = field(default_factory=lambda: ["**/*.py"])
    exclude: list[str] = field(default_factory=list)
    default_limit: int = 8
    marker_prefix: str = "@ctx"

    @property
    def ctx_dir(self) -> Path:
        """Return the `.ctx` directory for this project."""
        return self.root / CTX_DIRNAME

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this project."""
        return self.ctx_dir / DB_FILENAME

    @property
    def config_path(self) -> Path:
        """Return the TOML configuration path for this project."""
        return self.ctx_dir / CONFIG_FILENAME


def find_root(start: Path | None = None) -> Path | None:
    """Walk upward from *start* looking for a `.ctx/` directory."""
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / CTX_DIRNAME).is_dir():
            return candidate
    return None


def load_config(root: Path) -> Config:
    """Load configuration from `<root>/.ctx/config.toml`, applying defaults."""
    cfg = Config(root=root)
    config_path = root / CTX_DIRNAME / CONFIG_FILENAME
    if config_path.exists():
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
        scan = data.get("scan", {})
        cfg.include = list(scan.get("include", cfg.include))
        cfg.exclude = list(scan.get("exclude", cfg.exclude))
        cfg.default_limit = int(data.get("context", {}).get("default_limit", cfg.default_limit))
        cfg.marker_prefix = str(data.get("markers", {}).get("prefix", cfg.marker_prefix))
    return cfg


def require_config(start: Path | None = None) -> Config:
    """Return the active config, or raise a friendly error if uninitialized."""
    root = find_root(start)
    if root is None:
        raise CtxError(
            "No ctx project found here. Run 'ctx init' first to create a .ctx/ directory."
        )
    return load_config(root)


def die(message: str, code: int = 1) -> None:
    """Print a friendly error and exit."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)
