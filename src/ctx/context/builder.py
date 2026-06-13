"""Assemble a compact Markdown context block from search hits."""

from __future__ import annotations

import sqlite3

from ..db import find_symbols_by_ref, relations_for_symbol, tags_for_symbol
from .search import SearchHit


# @ctx tag: context rendering, ask output
# @ctx related: search, rebuild_fts, Symbol, ContextNote, Invariant
# @ctx note: This is the Markdown shape returned by ctx ask after retrieval.
def build_markdown(conn: sqlite3.Connection, prompt: str, hits: list[SearchHit]) -> str:
    """Render the retrieved context as compact Markdown (no AI involved)."""
    symbols = [h for h in hits if h.row_type == "symbol"]
    notes = [h for h in hits if h.row_type == "note"]
    invariants = [h for h in hits if h.row_type == "invariant"]

    out: list[str] = [f"# Context for: {prompt.strip()}", ""]

    if not hits:
        out.append("_No relevant context found._")
        return "\n".join(out)

    if symbols:
        out.append("## Relevant symbols")
        out.append("")
        for hit in symbols:
            row = conn.execute("SELECT * FROM symbols WHERE id = ?", (hit.ref_id,)).fetchone()
            if row is None:
                continue
            out.extend(_symbol_block(conn, row))
        out.append("")

    if notes:
        out.append("## Relevant notes")
        out.append("")
        for hit in notes:
            row = conn.execute("SELECT * FROM context_notes WHERE id = ?", (hit.ref_id,)).fetchone()
            if row is None:
                continue
            loc = _origin(conn, row)
            out.append(f"- **{row['title']}**{loc} — {row['content']}")
            if row["tags"]:
                out.append(f"  - tags: {row['tags']}")
        out.append("")

    if invariants:
        out.append("## Relevant invariants")
        out.append("")
        for hit in invariants:
            row = conn.execute("SELECT * FROM invariants WHERE id = ?", (hit.ref_id,)).fetchone()
            if row is None:
                continue
            out.append(
                f"- `[{row['severity']}]` **{row['scope']}**{_origin(conn, row)} — {row['content']}"
            )
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _origin(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Render where a note/invariant comes from: its bound symbol, else file:line."""
    symbol_id = row["symbol_id"] if "symbol_id" in row.keys() else None
    if symbol_id:
        sym = conn.execute(
            "SELECT qualified_name, file_path, start_line FROM symbols WHERE id = ?",
            (symbol_id,),
        ).fetchone()
        if sym is not None:
            return f" (`{sym['qualified_name']}` @ {sym['file_path']}:{sym['start_line']})"
    if row["file_path"]:
        return f" ({row['file_path']}:{row['line']})"
    return ""


def _related_label(conn: sqlite3.Connection, ref: str) -> str:
    """Render a related ref, adding the resolved location when it matches a symbol."""
    matches = find_symbols_by_ref(conn, ref)
    if matches:
        m = matches[0]
        return f"`{ref}` ({m['file_path']}:{m['start_line']})"
    return f"`{ref}`"


def _symbol_block(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """Render one symbol section, including tags, relations and documentation."""
    loc = f"{row['file_path']}:{row['start_line']}"
    lines = [f"### `{row['qualified_name']}` ({row['kind']})", f"- location: ({loc})"]
    if row["signature"]:
        lines.append(f"- signature: `{row['signature']}`")
    tags = tags_for_symbol(conn, row["id"])
    if tags:
        lines.append(f"- tags: {', '.join(tags)}")
    related = [_related_label(conn, ref) for ref in relations_for_symbol(conn, row["id"])]
    if related:
        lines.append(f"- related: {', '.join(related)}")
    if row["documentation"]:
        lines.append("")
        lines.append("> " + row["documentation"].strip().replace("\n", "\n> "))
    lines.append("")
    return lines
