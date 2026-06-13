"""SQLite persistence layer for ctx, including FTS5 search index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import (
    STATUS_CHANGED,
    STATUS_DELETED,
    STATUS_NEW,
    STATUS_UNCHANGED,
    ContextNote,
    Invariant,
    Relation,
    Symbol,
    now_iso,
)

# @ctx tag: persistence, sqlite schema, fts index
# @ctx note: Stores symbols, marker/manual context rows and the FTS index for ctx ask.
# @ctx related: Symbol, ContextNote, Invariant, Relation, rebuild_fts
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    root_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_scan_commit TEXT,
    scan_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    language TEXT,
    kind TEXT,
    semantic_kind TEXT,
    name TEXT,
    qualified_name TEXT,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    documentation TEXT,
    documentation_kind TEXT,
    code_hash TEXT,
    documentation_hash TEXT,
    commit_hash TEXT,
    parent_symbol_id TEXT,
    confidence REAL,
    status TEXT,
    deleted_scan INTEGER
);

-- A tag targets either a symbol (symbol_id) or a whole file (file_path),
-- never expanded across every symbol in the file.
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id TEXT,
    file_path TEXT,
    tag TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS context_notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    importance TEXT,
    file_path TEXT,
    line INTEGER,
    symbol_id TEXT,
    source TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS invariants (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    severity TEXT,
    tags TEXT,
    file_path TEXT,
    line INTEGER,
    symbol_id TEXT,
    source TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS symbol_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id TEXT NOT NULL,
    code_hash TEXT,
    documentation_hash TEXT,
    commit_hash TEXT,
    status TEXT,
    changed_at TEXT
);

-- `@ctx related:` edges: a symbol or file points at a referenced name.
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    from_symbol_id TEXT,
    from_file_path TEXT,
    to_ref TEXT NOT NULL,
    line INTEGER,
    source TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols (file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols (qualified_name);
CREATE INDEX IF NOT EXISTS idx_tags_symbol ON tags (symbol_id);
CREATE INDEX IF NOT EXISTS idx_tags_file ON tags (file_path);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations (from_symbol_id);
-- COALESCE keeps the uniqueness working even though symbol_id/file_path are
-- nullable (SQLite treats raw NULLs as always-distinct).
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_unique
    ON tags (COALESCE(symbol_id, ''), COALESCE(file_path, ''), tag);

CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5 (
    row_type UNINDEXED,
    ref_id UNINDEXED,
    name,
    qualified_name,
    file_path,
    documentation,
    tags,
    content
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sensible defaults, applying light migrations."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER a table to add *column* if an older database lacks it."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if cols and column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema."""
    _ensure_column(conn, "context_notes", "symbol_id", "TEXT")
    _ensure_column(conn, "invariants", "symbol_id", "TEXT")
    _ensure_column(conn, "projects", "scan_seq", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "symbols", "deleted_scan", "INTEGER")

    # tags gained a file_path target (file-scope tags) and dropped the inline
    # UNIQUE(symbol_id, tag) constraint, so the table must be rebuilt.
    tag_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tags)")}
    if tag_cols and "file_path" not in tag_cols:
        conn.executescript(
            """
            ALTER TABLE tags RENAME TO tags_legacy;
            CREATE TABLE tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol_id TEXT,
                file_path TEXT,
                tag TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual'
            );
            INSERT INTO tags (symbol_id, file_path, tag, source)
                SELECT symbol_id, NULL, tag, source FROM tags_legacy;
            DROP TABLE tags_legacy;
            CREATE INDEX IF NOT EXISTS idx_tags_symbol ON tags (symbol_id);
            CREATE INDEX IF NOT EXISTS idx_tags_file ON tags (file_path);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_unique
                ON tags (COALESCE(symbol_id, ''), COALESCE(file_path, ''), tag);
            """
        )

    # `relations` was added later; create it for databases that predate it.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS relations (
            id TEXT PRIMARY KEY,
            from_symbol_id TEXT,
            from_file_path TEXT,
            to_ref TEXT NOT NULL,
            line INTEGER,
            source TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_relations_from ON relations (from_symbol_id);
        """
    )
    conn.commit()


def init_db(db_path: Path, root: Path) -> None:
    """Create the schema and register the project row."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO projects (id, root_path, created_at) VALUES (1, ?, ?)",
            (str(root), now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Scan generations
# --------------------------------------------------------------------------- #
def bump_scan_seq(conn: sqlite3.Connection) -> int:
    """Increment and return the project's scan counter (the current generation)."""
    seq = current_scan_seq(conn) + 1
    conn.execute("UPDATE projects SET scan_seq = ? WHERE id = 1", (seq,))
    return seq


def current_scan_seq(conn: sqlite3.Connection) -> int:
    """Return the current scan generation counter, or 0 before any scan."""
    row = conn.execute("SELECT scan_seq FROM projects WHERE id = 1").fetchone()
    return (row["scan_seq"] or 0) if row else 0


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #
def get_symbol(conn: sqlite3.Connection, symbol_id: str) -> sqlite3.Row | None:
    """Return a symbol row by id, or None when it is absent."""
    return conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()


def upsert_symbol(conn: sqlite3.Connection, sym: Symbol) -> str:
    """Insert or update a symbol, returning its computed status.

    Status is derived by comparing the incoming code/doc hashes with whatever
    is already stored.  History rows are appended when something changes.
    """
    existing = get_symbol(conn, sym.id)
    if existing is None or existing["status"] == STATUS_DELETED:
        # Brand new, or a previously-deleted symbol that reappeared.
        status = STATUS_NEW
    elif existing["code_hash"] == sym.code_hash:
        status = STATUS_UNCHANGED
    else:
        status = STATUS_CHANGED
    sym.status = status

    conn.execute(
        """
        INSERT INTO symbols (
            id, language, kind, semantic_kind, name, qualified_name, file_path,
            start_line, end_line, signature, documentation, documentation_kind,
            code_hash, documentation_hash, commit_hash, parent_symbol_id,
            confidence, status
        ) VALUES (
            :id, :language, :kind, :semantic_kind, :name, :qualified_name, :file_path,
            :start_line, :end_line, :signature, :documentation, :documentation_kind,
            :code_hash, :documentation_hash, :commit_hash, :parent_symbol_id,
            :confidence, :status
        )
        ON CONFLICT(id) DO UPDATE SET
            language=excluded.language, kind=excluded.kind,
            semantic_kind=excluded.semantic_kind, name=excluded.name,
            qualified_name=excluded.qualified_name, file_path=excluded.file_path,
            start_line=excluded.start_line, end_line=excluded.end_line,
            signature=excluded.signature, documentation=excluded.documentation,
            documentation_kind=excluded.documentation_kind, code_hash=excluded.code_hash,
            documentation_hash=excluded.documentation_hash, commit_hash=excluded.commit_hash,
            parent_symbol_id=excluded.parent_symbol_id, confidence=excluded.confidence,
            status=excluded.status
        """,
        sym.model_dump(),
    )

    if status != STATUS_UNCHANGED:
        _record_history(
            conn, sym.id, sym.code_hash, sym.documentation_hash, sym.commit_hash, status
        )
    return status


def mark_deleted(
    conn: sqlite3.Connection, live_ids: set[str], commit_hash: str | None, scan_seq: int
) -> int:
    """Mark symbols absent from *live_ids* as deleted, stamping the scan gen.

    Only symbols not already deleted are touched, so `deleted_scan` records the
    generation at which a symbol *first* disappeared — that's what `review`
    uses to show recent deletions only.
    """
    rows = conn.execute("SELECT id FROM symbols WHERE status != ?", (STATUS_DELETED,)).fetchall()
    deleted = 0
    for row in rows:
        if row["id"] not in live_ids:
            conn.execute(
                "UPDATE symbols SET status = ?, deleted_scan = ? WHERE id = ?",
                (STATUS_DELETED, scan_seq, row["id"]),
            )
            _record_history(conn, row["id"], "", None, commit_hash, STATUS_DELETED)
            deleted += 1
    return deleted


def purge_deleted(conn: sqlite3.Connection) -> int:
    """Permanently remove soft-deleted symbols and their dependent rows."""
    rows = conn.execute("SELECT id FROM symbols WHERE status = ?", (STATUS_DELETED,)).fetchall()
    ids = [r["id"] for r in rows]
    for sid in ids:
        conn.execute("DELETE FROM tags WHERE symbol_id = ?", (sid,))
        conn.execute("DELETE FROM symbol_history WHERE symbol_id = ?", (sid,))
        conn.execute("DELETE FROM relations WHERE from_symbol_id = ?", (sid,))
        conn.execute("DELETE FROM symbols WHERE id = ?", (sid,))
    return len(ids)


def _record_history(
    conn: sqlite3.Connection,
    symbol_id: str,
    code_hash: str,
    doc_hash: str | None,
    commit_hash: str | None,
    status: str,
) -> None:
    """Append one lifecycle history row for a symbol."""
    conn.execute(
        """INSERT INTO symbol_history
           (symbol_id, code_hash, documentation_hash, commit_hash, status, changed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (symbol_id, code_hash, doc_hash, commit_hash, status, now_iso()),
    )


# --------------------------------------------------------------------------- #
# Tags / notes / invariants
# --------------------------------------------------------------------------- #
def find_symbols_by_ref(conn: sqlite3.Connection, ref: str) -> list[sqlite3.Row]:
    """Find symbols by exact name or qualified_name."""
    return conn.execute(
        "SELECT * FROM symbols WHERE (name = ? OR qualified_name = ?) AND status != ?",
        (ref, ref, STATUS_DELETED),
    ).fetchall()


def add_tag(
    conn: sqlite3.Connection,
    tag: str,
    *,
    source: str = "manual",
    symbol_id: str | None = None,
    file_path: str | None = None,
) -> bool:
    """Attach a tag to a symbol or a file. Returns True if it was newly added.

    Exactly one of *symbol_id* / *file_path* should be set; a file-scope tag is
    a single row, not one per symbol in the file.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO tags (symbol_id, file_path, tag, source) VALUES (?, ?, ?, ?)",
        (symbol_id, file_path, tag, source),
    )
    return cur.rowcount > 0


def tags_for_symbol(conn: sqlite3.Connection, symbol_id: str) -> list[str]:
    """Tags directly on a symbol (not the file-scope tags of its file)."""
    rows = conn.execute(
        "SELECT tag FROM tags WHERE symbol_id = ? ORDER BY tag", (symbol_id,)
    ).fetchall()
    return [r["tag"] for r in rows]


def tags_for_file(conn: sqlite3.Connection, file_path: str) -> list[str]:
    """File-scope tags (symbol_id IS NULL) for a given file."""
    rows = conn.execute(
        "SELECT tag FROM tags WHERE file_path = ? AND symbol_id IS NULL ORDER BY tag",
        (file_path,),
    ).fetchall()
    return [r["tag"] for r in rows]


def add_note(conn: sqlite3.Connection, note: ContextNote) -> bool:
    """Insert a note, returning True only if it was genuinely new.

    Marker-derived notes have content-based ids, so re-adding an unchanged
    marker is a no-op (rowcount 0); manual notes get unique ids and always
    count as new.
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO context_notes
           (id, title, content, tags, importance, file_path, line, symbol_id, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            note.id,
            note.title,
            note.content,
            ",".join(note.tags),
            note.importance,
            note.file_path,
            note.line,
            note.symbol_id,
            note.source,
            note.created_at,
        ),
    )
    return cur.rowcount > 0


def add_invariant(conn: sqlite3.Connection, inv: Invariant) -> bool:
    """Insert an invariant, returning True only if it was genuinely new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO invariants
           (id, scope, content, severity, tags, file_path, line, symbol_id, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            inv.id,
            inv.scope,
            inv.content,
            inv.severity,
            ",".join(inv.tags),
            inv.file_path,
            inv.line,
            inv.symbol_id,
            inv.source,
            inv.created_at,
        ),
    )
    return cur.rowcount > 0


def clear_marker_data(conn: sqlite3.Connection) -> None:
    """Drop all `@ctx`-marker-derived rows so a scan can re-derive them.

    Marker notes/invariants/tags are state derived from source, like symbols;
    clearing them first means removing a marker also removes its row (no
    orphans) and avoids counting unchanged markers as "added" every scan.
    Manually-added notes/invariants/tags (source='manual') are untouched.
    """
    conn.execute("DELETE FROM context_notes WHERE source = 'ctx-marker'")
    conn.execute("DELETE FROM invariants WHERE source = 'ctx-marker'")
    conn.execute("DELETE FROM tags WHERE source = 'ctx-marker'")
    conn.execute("DELETE FROM relations WHERE source = 'ctx-marker'")


def add_relation(conn: sqlite3.Connection, rel: Relation) -> bool:
    """Insert a `@ctx related:` edge, returning True only if it was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO relations
           (id, from_symbol_id, from_file_path, to_ref, line, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            rel.id,
            rel.from_symbol_id,
            rel.from_file_path,
            rel.to_ref,
            rel.line,
            rel.source,
            rel.created_at,
        ),
    )
    return cur.rowcount > 0


def relations_for_symbol(conn: sqlite3.Connection, symbol_id: str) -> list[str]:
    """Referenced names a symbol points at via `@ctx related:`."""
    rows = conn.execute(
        "SELECT to_ref FROM relations WHERE from_symbol_id = ? ORDER BY to_ref",
        (symbol_id,),
    ).fetchall()
    return [r["to_ref"] for r in rows]


def outgoing_relations(conn: sqlite3.Connection, symbol_ids: list[str]) -> list[sqlite3.Row]:
    """Full relation rows leaving the given symbols (this -> ref)."""
    if not symbol_ids:
        return []
    placeholders = ",".join("?" * len(symbol_ids))
    return conn.execute(
        f"SELECT * FROM relations WHERE from_symbol_id IN ({placeholders}) ORDER BY to_ref",
        symbol_ids,
    ).fetchall()


def incoming_relations(conn: sqlite3.Connection, refs: list[str]) -> list[sqlite3.Row]:
    """Full relation rows pointing at any of *refs* (other -> this)."""
    if not refs:
        return []
    placeholders = ",".join("?" * len(refs))
    return conn.execute(
        f"SELECT * FROM relations WHERE to_ref IN ({placeholders}) ORDER BY from_symbol_id",
        refs,
    ).fetchall()


def dangling_relations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Relations whose `to_ref` resolves to no live symbol (likely typos)."""
    return conn.execute(
        """
        SELECT * FROM relations r
        WHERE NOT EXISTS (
            SELECT 1 FROM symbols s
            WHERE s.status != ? AND (s.name = r.to_ref OR s.qualified_name = r.to_ref)
        )
        ORDER BY r.to_ref
        """,
        (STATUS_DELETED,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# FTS index
# --------------------------------------------------------------------------- #
# @ctx tag: fts indexing, context retrieval
# @ctx related: search, build_markdown, ask
# @ctx note: Search quality depends on context_fts folding names, docs, tags and refs.
def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the unified FTS index from symbols, notes and invariants.

    Rebuilding wholesale keeps the index trivially in sync; the dataset is a
    single project, so this is cheap.
    """
    conn.execute("DELETE FROM context_fts")

    for row in conn.execute(
        "SELECT * FROM symbols WHERE status != ?", (STATUS_DELETED,)
    ).fetchall():
        # A symbol is searchable by its own tags plus the file-scope tags of
        # its file, so file-level tags still surface the file's symbols.
        own = tags_for_symbol(conn, row["id"])
        file_level = tags_for_file(conn, row["file_path"])
        tags = " ".join(own + file_level)
        # Fold `@ctx related:` targets into the content so the referenced names
        # are searchable and surface this symbol.
        related = " ".join(relations_for_symbol(conn, row["id"]))
        content = " ".join(p for p in (row["signature"] or "", related) if p)
        conn.execute(
            """INSERT INTO context_fts
               (row_type, ref_id, name, qualified_name, file_path, documentation, tags, content)
               VALUES ('symbol', ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["name"],
                row["qualified_name"],
                row["file_path"],
                row["documentation"] or "",
                tags,
                content,
            ),
        )

    for row in conn.execute("SELECT * FROM context_notes").fetchall():
        conn.execute(
            """INSERT INTO context_fts
               (row_type, ref_id, name, qualified_name, file_path, documentation, tags, content)
               VALUES ('note', ?, ?, '', ?, '', ?, ?)""",
            (row["id"], row["title"], row["file_path"] or "", row["tags"] or "", row["content"]),
        )

    for row in conn.execute("SELECT * FROM invariants").fetchall():
        conn.execute(
            """INSERT INTO context_fts
               (row_type, ref_id, name, qualified_name, file_path, documentation, tags, content)
               VALUES ('invariant', ?, ?, '', ?, '', ?, ?)""",
            (row["id"], row["scope"], row["file_path"] or "", row["tags"] or "", row["content"]),
        )
