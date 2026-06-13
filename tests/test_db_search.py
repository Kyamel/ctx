from __future__ import annotations

from pathlib import Path

from ctx import db
from ctx.context.builder import build_markdown
from ctx.context.search import build_match_query, search
from ctx.models import ContextNote, Symbol


def _symbol(name: str, doc: str) -> Symbol:
    return Symbol(
        id=f"id_{name}",
        kind="function",
        name=name,
        qualified_name=name,
        file_path="a.py",
        start_line=1,
        end_line=2,
        signature=f"def {name}()",
        documentation=doc,
        code_hash="h1",
    )


def test_build_match_query_filters_short_tokens():
    assert build_match_query("a the parser") == '"the"* OR "parser"*'
    assert build_match_query("!!! ??") is None


def test_status_lifecycle(project: Path, config):
    conn = db.connect(config.db_path)
    sym = _symbol("widget", "builds a widget")
    assert db.upsert_symbol(conn, sym) == "new"
    assert db.upsert_symbol(conn, sym) == "unchanged"
    sym.code_hash = "h2"
    assert db.upsert_symbol(conn, sym) == "changed"
    deleted = db.mark_deleted(conn, set(), None, scan_seq=1)
    assert deleted == 1
    # A re-appearing symbol counts as new again, not unchanged.
    assert db.upsert_symbol(conn, sym) == "new"
    conn.close()


def test_fts_search_finds_symbols_and_notes(project: Path, config):
    conn = db.connect(config.db_path)
    db.upsert_symbol(conn, _symbol("parser", "parses tokens into a tree"))
    db.add_note(
        conn,
        ContextNote(id="n1", title="Tokenizer", content="splits input into tokens"),
    )
    db.rebuild_fts(conn)
    conn.commit()

    hits = search(conn, "tokens", limit=10)
    kinds = {h.row_type for h in hits}
    assert "symbol" in kinds and "note" in kinds
    conn.close()


def test_context_symbol_locations_use_clickable_start_line_only(project: Path, config):
    conn = db.connect(config.db_path)
    db.upsert_symbol(conn, _symbol("scanner", "extracts symbols"))
    db.rebuild_fts(conn)
    conn.commit()

    hits = search(conn, "scanner", limit=10)
    markdown = build_markdown(conn, "scanner", hits)

    assert "- location: (a.py:1)" in markdown
    assert "a.py:1-2" not in markdown
    conn.close()
