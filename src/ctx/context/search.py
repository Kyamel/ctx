"""FTS5-backed retrieval of relevant context for a prompt."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass
class SearchHit:
    """A ranked row reference returned from the unified FTS table."""

    row_type: str  # symbol | note | invariant
    ref_id: str
    rank: float


_TOKEN = re.compile(r"[A-Za-z0-9_]+")


# @ctx tag: search query, fts retrieval
# @ctx related: rebuild_fts, build_markdown, ask
def build_match_query(prompt: str) -> str | None:
    """Turn a free-form prompt into a safe FTS5 MATCH expression.

    Each alphanumeric token becomes a prefix term, OR-combined so partial
    relevance still surfaces results.
    """
    tokens = [t for t in _TOKEN.findall(prompt) if len(t) >= 2]
    if not tokens:
        return None
    # De-duplicate while preserving order.
    seen: list[str] = []
    for t in tokens:
        if t.lower() not in (s.lower() for s in seen):
            seen.append(t)
    return " OR ".join(f'"{t}"*' for t in seen)


# @ctx tag: context retrieval
def search(conn: sqlite3.Connection, prompt: str, limit: int) -> list[SearchHit]:
    """Return ranked search hits across symbols, notes and invariants."""
    match = build_match_query(prompt)
    if match is None:
        return []
    rows = conn.execute(
        """
        SELECT row_type, ref_id, rank
        FROM context_fts
        WHERE context_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    return [SearchHit(r["row_type"], r["ref_id"], r["rank"]) for r in rows]
