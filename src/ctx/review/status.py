"""Queries powering `ctx review` and `ctx map`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..db import current_scan_seq
from ..models import STATUS_CHANGED, STATUS_DELETED, STATUS_NEW


@dataclass
class ReviewBuckets:
    """Grouped symbol rows shown by `ctx review`."""

    new: list[sqlite3.Row] = field(default_factory=list)
    changed: list[sqlite3.Row] = field(default_factory=list)
    deleted: list[sqlite3.Row] = field(default_factory=list)
    undocumented: list[sqlite3.Row] = field(default_factory=list)


def collect_review(conn: sqlite3.Connection) -> ReviewBuckets:
    """Gather symbols grouped by review-relevant status."""
    buckets = ReviewBuckets()
    buckets.new = _by_status(conn, STATUS_NEW)
    buckets.changed = _by_status(conn, STATUS_CHANGED)
    # Only show symbols deleted in the most recent scan; older soft-deletions
    # linger in the db (until `ctx prune`) but stop cluttering every review.
    buckets.deleted = conn.execute(
        """SELECT * FROM symbols
           WHERE status = ? AND deleted_scan = ?
           ORDER BY file_path, start_line""",
        (STATUS_DELETED, current_scan_seq(conn)),
    ).fetchall()
    buckets.undocumented = conn.execute(
        """SELECT * FROM symbols
           WHERE status != ? AND (documentation IS NULL OR documentation = '')
           ORDER BY file_path, start_line""",
        (STATUS_DELETED,),
    ).fetchall()
    return buckets


def _by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Return symbols with one lifecycle status ordered by source location."""
    return conn.execute(
        "SELECT * FROM symbols WHERE status = ? ORDER BY file_path, start_line",
        (status,),
    ).fetchall()


@dataclass
class FileSummary:
    """Documentation coverage for a single file."""

    file_path: str
    total: int
    documented: int

    @property
    def undocumented(self) -> int:
        """Return how many symbols in this file lack documentation."""
        return self.total - self.documented


def project_map(conn: sqlite3.Connection) -> list[FileSummary]:
    """Per-file symbol counts with documentation coverage."""
    rows = conn.execute(
        """
        SELECT file_path,
               COUNT(*) AS total,
               SUM(CASE WHEN documentation IS NOT NULL AND documentation != ''
                        THEN 1 ELSE 0 END) AS documented
        FROM symbols
        WHERE status != ?
        GROUP BY file_path
        ORDER BY file_path
        """,
        (STATUS_DELETED,),
    ).fetchall()
    return [FileSummary(r["file_path"], r["total"], r["documented"] or 0) for r in rows]
