"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from ctx import db
from ctx.config import DEFAULT_CONFIG_TOML, load_config


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An initialized ctx project rooted at a temp dir."""
    ctx_dir = tmp_path / ".ctx"
    ctx_dir.mkdir()
    (ctx_dir / "config.toml").write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    db.init_db(ctx_dir / "context.db", tmp_path)
    return tmp_path


@pytest.fixture
def config(project: Path):
    return load_config(project)
