"""Internal Pydantic models used across ctx."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# Symbol lifecycle statuses.
STATUS_NEW = "new"
STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_DELETED = "deleted"


def now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class Symbol(BaseModel):
    """A code symbol (function, class or method) extracted from a source file."""

    id: str
    language: str = "python"
    kind: str
    semantic_kind: str | None = None
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""
    documentation: str | None = None
    documentation_kind: str = "docstring"
    code_hash: str = ""
    documentation_hash: str | None = None
    commit_hash: str | None = None
    parent_symbol_id: str | None = None
    confidence: float = 1.0
    status: str = STATUS_NEW


class Tag(BaseModel):
    """A manual or extracted tag attached to a symbol or a whole file."""

    tag: str
    symbol_id: str | None = None
    file_path: str | None = None
    source: str = "manual"


class ContextNote(BaseModel):
    """A free-form note giving context about the project."""

    id: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    importance: str = "normal"
    file_path: str | None = None
    line: int | None = None
    # Symbol this note attaches to, when the marker sits inside/just above one.
    symbol_id: str | None = None
    source: str = "manual"
    created_at: str = Field(default_factory=now_iso)


class Invariant(BaseModel):
    """A rule/constraint that should hold within a given scope."""

    id: str
    scope: str
    content: str
    severity: str = "warning"
    tags: list[str] = Field(default_factory=list)
    file_path: str | None = None
    line: int | None = None
    # Symbol this invariant attaches to, when resolvable from the marker site.
    symbol_id: str | None = None
    source: str = "manual"
    created_at: str = Field(default_factory=now_iso)


class Relation(BaseModel):
    """A `@ctx related:` edge from a symbol/file to a referenced name."""

    id: str
    to_ref: str
    from_symbol_id: str | None = None
    from_file_path: str | None = None
    line: int | None = None
    source: str = "ctx-marker"
    created_at: str = Field(default_factory=now_iso)


class SymbolHistory(BaseModel):
    """A recorded change in a symbol's hashes/status across scans."""

    symbol_id: str
    code_hash: str
    documentation_hash: str | None
    commit_hash: str | None
    status: str
    changed_at: str = Field(default_factory=now_iso)


class ScanResult(BaseModel):
    """Aggregate counts returned by a scan."""

    files_scanned: int = 0
    symbols_new: int = 0
    symbols_changed: int = 0
    symbols_unchanged: int = 0
    symbols_deleted: int = 0
    notes_added: int = 0
    invariants_added: int = 0
    tags_added: int = 0
    relations_added: int = 0
    commit_hash: str | None = None
