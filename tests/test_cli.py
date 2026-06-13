from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctx.cli import app

runner = CliRunner()


@pytest.fixture
def chdir(tmp_path: Path):
    old = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


def _write_project(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "core.py").write_text(
        '''\
# @ctx note: core module powers everything
def add(a, b):
    """Add two numbers."""
    return a + b


class Account:
    """A bank account."""

    def deposit(self, amount):
        # @ctx invariant: amount must be positive
        return amount
''',
        encoding="utf-8",
    )
    # excluded dir must be ignored
    (root / ".venv").mkdir()
    (root / ".venv" / "junk.py").write_text("def should_not_appear():\n    pass\n", "utf-8")


def test_full_workflow(chdir: Path):
    _write_project(chdir)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (chdir / ".ctx" / "context.db").exists()
    assert (chdir / ".ctx" / "config.toml").exists()

    scan_res = runner.invoke(app, ["scan"])
    assert scan_res.exit_code == 0, scan_res.output
    assert "new symbols" in scan_res.output

    # excluded file should not be scanned
    review_res = runner.invoke(app, ["review"])
    assert review_res.exit_code == 0
    assert "should_not_appear" not in review_res.output
    assert "add" in review_res.output

    # tagging
    tag_res = runner.invoke(app, ["tag", "add", "math", "arithmetic"])
    assert tag_res.exit_code == 0, tag_res.output
    assert "Added" in tag_res.output

    # note add
    note_res = runner.invoke(
        app, ["note", "add", "--title", "Design", "--content", "uses ledger pattern"]
    )
    assert note_res.exit_code == 0, note_res.output

    # invariant add
    inv_res = runner.invoke(
        app,
        ["invariant", "add", "--scope", "Account", "--content", "balance never negative"],
    )
    assert inv_res.exit_code == 0, inv_res.output

    # map
    map_res = runner.invoke(app, ["map"])
    assert map_res.exit_code == 0
    assert "pkg/core.py" in map_res.output

    # ask returns markdown context
    ask_res = runner.invoke(app, ["ask", "ledger account balance", "--raw"])
    assert ask_res.exit_code == 0, ask_res.output
    assert "# Context for:" in ask_res.output
    assert "balance never negative" in ask_res.output


def test_marker_data_does_not_accumulate(chdir: Path):
    """Re-scanning identical @ctx markers must not pile up notes/invariants,
    and removing a marker must drop its derived row (no orphans)."""
    pkg = chdir / "pkg"
    pkg.mkdir()
    src = pkg / "m.py"
    src.write_text(
        "# @ctx note: load-bearing module\n"
        "# @ctx invariant: ids stay unique\n"
        "def f():\n    return 1\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["init"]).exit_code == 0

    import sqlite3

    def counts() -> tuple[int, int]:
        conn = sqlite3.connect(chdir / ".ctx" / "context.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM context_notes").fetchone()[0]
            i = conn.execute("SELECT COUNT(*) FROM invariants").fetchone()[0]
            return n, i
        finally:
            conn.close()

    assert runner.invoke(app, ["scan"]).exit_code == 0
    assert counts() == (1, 1)
    # Scanning again with no source change keeps the totals stable.
    assert runner.invoke(app, ["scan"]).exit_code == 0
    assert counts() == (1, 1)

    # Remove the marker -> its derived note disappears (the invariant stays).
    src.write_text("def f():\n    return 1\n", encoding="utf-8")
    assert runner.invoke(app, ["scan"]).exit_code == 0
    assert counts() == (0, 0)


def test_file_scope_tag_is_a_single_row(chdir: Path):
    """A file-scope @ctx tag must be one row pointing at the file, not one per
    symbol; a glued tag must bind to exactly its symbol."""
    pkg = chdir / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        # file-scope tag: blank line separates it from any def
        "# @ctx tag: module-wide\n"
        "\n"
        "def a():\n    return 1\n"
        "\n"
        "# @ctx tag: glued\n"
        "def b():\n    return 2\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["scan"]).exit_code == 0

    import sqlite3

    conn = sqlite3.connect(chdir / ".ctx" / "context.db")
    conn.row_factory = sqlite3.Row
    try:
        file_tags = conn.execute(
            "SELECT tag, file_path FROM tags WHERE symbol_id IS NULL"
        ).fetchall()
        glued = conn.execute(
            "SELECT s.qualified_name FROM tags t JOIN symbols s ON s.id = t.symbol_id "
            "WHERE t.tag = 'glued'"
        ).fetchall()
    finally:
        conn.close()

    # exactly one row for the module-wide tag, attached to the file
    assert [(r["tag"], r["file_path"]) for r in file_tags] == [("module-wide", "pkg/m.py")]
    # glued tag bound to exactly one symbol
    assert [r["qualified_name"] for r in glued] == ["b"]


def test_related_marker_is_persisted_and_resolved(chdir: Path):
    """`@ctx related:` becomes a stored edge from the bound symbol, and `ask`
    resolves the target to its location."""
    pkg = chdir / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "def helper():\n    return 1\n\n\n"
        "# @ctx related: helper\n"
        "def caller():\n    return helper()\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["init"]).exit_code == 0
    scan_res = runner.invoke(app, ["scan"])
    assert scan_res.exit_code == 0, scan_res.output
    assert "@ctx relations" in scan_res.output

    import sqlite3

    conn = sqlite3.connect(chdir / ".ctx" / "context.db")
    conn.row_factory = sqlite3.Row
    try:
        rels = conn.execute(
            "SELECT s.qualified_name AS frm, r.to_ref FROM relations r "
            "JOIN symbols s ON s.id = r.from_symbol_id"
        ).fetchall()
    finally:
        conn.close()
    # edge from caller -> helper, bound to the caller symbol
    assert [(r["frm"], r["to_ref"]) for r in rels] == [("caller", "helper")]

    # re-scanning does not duplicate the edge
    assert runner.invoke(app, ["scan"]).exit_code == 0
    conn = sqlite3.connect(chdir / ".ctx" / "context.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
    finally:
        conn.close()

    # ask surfaces the relation with the resolved target location
    ask_res = runner.invoke(app, ["ask", "caller", "--raw"])
    assert ask_res.exit_code == 0, ask_res.output
    assert "related:" in ask_res.output and "helper" in ask_res.output


def test_related_command_navigates_both_directions(chdir: Path):
    pkg = chdir / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "def helper():\n    return 1\n\n\n"
        "# @ctx related: helper\n"
        "def caller():\n    return helper()\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["scan"]).exit_code == 0

    # caller -> helper shows as outgoing for caller...
    out = runner.invoke(app, ["related", "caller"])
    assert out.exit_code == 0, out.output
    assert "outgoing (1)" in out.output and "helper" in out.output

    # ...and as incoming for helper.
    inc = runner.invoke(app, ["related", "helper"])
    assert inc.exit_code == 0, inc.output
    assert "incoming (1)" in inc.output and "caller" in inc.output


def test_dangling_relation_warns_in_scan_and_review(chdir: Path):
    pkg = chdir / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "# @ctx related: nonexistent_target\ndef caller():\n    return 1\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["init"]).exit_code == 0

    scan_res = runner.invoke(app, ["scan"])
    assert scan_res.exit_code == 0
    assert "unresolved @ctx related" in scan_res.output
    assert "nonexistent_target" in scan_res.output

    review_res = runner.invoke(app, ["review"])
    assert review_res.exit_code == 0
    assert "nonexistent_target" in review_res.output


def test_deleted_shown_once_then_drops_off_and_prune_removes(chdir: Path):
    pkg = chdir / "pkg"
    pkg.mkdir()
    src = pkg / "m.py"
    src.write_text("def keep():\n    return 1\n\n\ndef gone():\n    return 2\n", encoding="utf-8")
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["scan"]).exit_code == 0

    # remove `gone` and rescan -> it shows up as deleted this scan
    src.write_text("def keep():\n    return 1\n", encoding="utf-8")
    assert runner.invoke(app, ["scan"]).exit_code == 0
    r1 = runner.invoke(app, ["review"])
    assert "Deleted" in r1.output and "gone" in r1.output

    # a subsequent scan (nothing further deleted) -> `gone` no longer in review
    assert runner.invoke(app, ["scan"]).exit_code == 0
    r2 = runner.invoke(app, ["review"])
    assert "gone" not in r2.output

    # it still lingers in the db until pruned
    import sqlite3

    def deleted_count() -> int:
        conn = sqlite3.connect(chdir / ".ctx" / "context.db")
        try:
            return conn.execute("SELECT COUNT(*) FROM symbols WHERE status = 'deleted'").fetchone()[
                0
            ]
        finally:
            conn.close()

    assert deleted_count() == 1
    prune_res = runner.invoke(app, ["prune"])
    assert prune_res.exit_code == 0 and "Pruned 1" in prune_res.output
    assert deleted_count() == 0


def test_commands_require_init(chdir: Path):
    res = runner.invoke(app, ["scan"])
    assert res.exit_code != 0
    assert "ctx init" in (res.output + str(res.stderr if hasattr(res, "stderr") else ""))
