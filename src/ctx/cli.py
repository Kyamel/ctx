"""Typer CLI entrypoint for ctx."""

from __future__ import annotations

import hashlib
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import db, git
from .config import (
    DEFAULT_CONFIG_TOML,
    Config,
    CtxError,
    die,
    require_config,
)
from .context import build_markdown, search
from .models import (
    STATUS_CHANGED,
    STATUS_NEW,
    STATUS_UNCHANGED,
    ContextNote,
    Invariant,
    Relation,
    ScanResult,
    now_iso,
)
from .review import collect_review, project_map
from .scanner import (
    PythonScanner,
    UniversalCtxScanner,
    discover_files,
    resolve_symbol_for_marker,
)

app = typer.Typer(
    help="ctx — local active project-memory for AI tooling.",
    no_args_is_help=True,
    add_completion=False,
)
note_app = typer.Typer(help="Manage manual context notes.", no_args_is_help=True)
invariant_app = typer.Typer(help="Manage manual invariants.", no_args_is_help=True)
app.add_typer(note_app, name="note")
app.add_typer(invariant_app, name="invariant")

console = Console()
err_console = Console(stderr=True)

# @ctx tag: scanner registry, language extension point
# @ctx note: Add new language scanners here after discover_files includes their files.
# @ctx related: Scanner, PythonScanner, scan
SCANNERS = [PythonScanner()]


def _config_or_die() -> Config:
    """Load the active project config or terminate with a user-facing error."""
    try:
        return require_config()
    except CtxError as exc:
        die(str(exc))
        raise  # unreachable, keeps type-checkers happy


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


# @ctx tag: app entry point
@app.command()
def init() -> None:
    """Create `.ctx/`, the SQLite database and a default config."""
    root = Path.cwd()
    ctx_dir = root / ".ctx"
    if ctx_dir.exists():
        console.print(f"[yellow].ctx already exists at[/] {ctx_dir}")
    ctx_dir.mkdir(exist_ok=True)

    config_path = ctx_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    db.init_db(ctx_dir / "context.db", root)
    console.print(
        Panel.fit(
            f"Initialized ctx project at [bold]{root}[/]\n"
            f"- {ctx_dir / 'context.db'}\n"
            f"- {config_path}",
            title="ctx init",
            border_style="green",
        )
    )


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
# @ctx tag: scan pipeline, symbol persistence, marker binding
# @ctx related: discover_files, UniversalCtxScanner, upsert_symbol, resolve_symbol_for_marker
# @ctx note: Orchestrates discovery, symbol extraction, marker binding and FTS rebuild.
@app.command()
def scan() -> None:
    """Scan the project, extract symbols + markers and persist them."""
    cfg = _config_or_die()
    commit = git.current_commit(cfg.root)
    result = ScanResult(commit_hash=commit)

    universal = UniversalCtxScanner(prefix=cfg.marker_prefix)
    live_ids: set[str] = set()
    files = discover_files(cfg)

    dangling: list[tuple[str, str]] = []
    conn = db.connect(cfg.db_path)
    try:
        scan_seq = db.bump_scan_seq(conn)
        # Marker-derived data is state derived from source: wipe and re-derive
        # it each scan so removed markers disappear and counts stay truthful.
        db.clear_marker_data(conn)
        for path in files:
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            result.files_scanned += 1
            rel = path.relative_to(cfg.root).as_posix()

            # 1. Language-aware symbols.
            file_symbols: list = []
            scanner = next((s for s in SCANNERS if s.can_handle(path)), None)
            if scanner is not None:
                for sym in scanner.scan(path, rel, source):
                    sym.commit_hash = commit
                    live_ids.add(sym.id)
                    status = db.upsert_symbol(conn, sym)
                    _tally(result, status)
                    file_symbols.append(sym)

            # 2. Universal @ctx markers (any file type). Counts reflect rows
            #    actually inserted, so duplicate markers are not double-counted.
            #    Each marker binds to the symbol it sits inside / just above,
            #    or to the file when no symbol is physically connected.
            source_lines = source.splitlines()
            markers = universal.scan(rel, source)
            for note in markers.notes:
                note.symbol_id = resolve_symbol_for_marker(note.line, file_symbols, source_lines)
                if db.add_note(conn, note):
                    result.notes_added += 1
            for inv in markers.invariants:
                sid = resolve_symbol_for_marker(inv.line, file_symbols, source_lines)
                if sid is not None:
                    inv.symbol_id = sid
                    inv.scope = next(s.qualified_name for s in file_symbols if s.id == sid)
                if db.add_invariant(conn, inv):
                    result.invariants_added += 1
            for line_no, tag in markers.tags:
                sid = resolve_symbol_for_marker(line_no, file_symbols, source_lines)
                # Bound to a symbol -> tag just that symbol; otherwise a single
                # file-scope tag (not one row per symbol in the file).
                if sid is not None:
                    added = db.add_tag(conn, tag, source="ctx-marker", symbol_id=sid)
                else:
                    added = db.add_tag(conn, tag, source="ctx-marker", file_path=rel)
                if added:
                    result.tags_added += 1
            for line_no, ref in markers.related:
                sid = resolve_symbol_for_marker(line_no, file_symbols, source_lines)
                relation = Relation(
                    id=_gen_marker_id("rel", rel, line_no, ref),
                    from_symbol_id=sid,
                    from_file_path=None if sid is not None else rel,
                    to_ref=ref,
                    line=line_no,
                )
                if db.add_relation(conn, relation):
                    result.relations_added += 1

        result.symbols_deleted = db.mark_deleted(conn, live_ids, commit, scan_seq)
        conn.execute("UPDATE projects SET last_scan_commit = ? WHERE id = 1", (commit,))
        db.rebuild_fts(conn)
        dangling = _dangling_rows(conn)
        conn.commit()
    finally:
        conn.close()

    _print_scan_summary(result)
    _warn_dangling(dangling)


def _tally(result: ScanResult, status: str) -> None:
    """Increment scan counters based on a symbol lifecycle status."""
    if status == STATUS_NEW:
        result.symbols_new += 1
    elif status == STATUS_CHANGED:
        result.symbols_changed += 1
    elif status == STATUS_UNCHANGED:
        result.symbols_unchanged += 1


def _print_scan_summary(result: ScanResult) -> None:
    """Render the scan result counters as a Rich table."""
    table = Table(title="Scan complete", show_header=False, border_style="cyan")
    table.add_row("files scanned", str(result.files_scanned))
    table.add_row("new symbols", f"[green]{result.symbols_new}[/]")
    table.add_row("changed symbols", f"[yellow]{result.symbols_changed}[/]")
    table.add_row("unchanged symbols", str(result.symbols_unchanged))
    table.add_row("deleted symbols", f"[red]{result.symbols_deleted}[/]")
    table.add_row("@ctx notes", str(result.notes_added))
    table.add_row("@ctx invariants", str(result.invariants_added))
    table.add_row("@ctx tags", str(result.tags_added))
    table.add_row("@ctx relations", str(result.relations_added))
    table.add_row("commit", result.commit_hash or "[dim]n/a[/]")
    console.print(table)


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
@app.command()
def review() -> None:
    """Show new, changed, deleted and undocumented symbols."""
    cfg = _config_or_die()
    conn = db.connect(cfg.db_path)
    try:
        buckets = collect_review(conn)
        dangling = _dangling_rows(conn)
    finally:
        conn.close()

    sections = [
        ("New", "green", buckets.new),
        ("Changed", "yellow", buckets.changed),
        ("Deleted", "red", buckets.deleted),
        ("Undocumented", "magenta", buckets.undocumented),
    ]
    any_rows = False
    for title, color, rows in sections:
        if not rows:
            continue
        any_rows = True
        table = Table(title=f"{title} ({len(rows)})", border_style=color, header_style=color)
        table.add_column("symbol")
        table.add_column("kind")
        table.add_column("location")
        table.add_column("doc")
        for row in rows:
            has_doc = "yes" if (row["documentation"] or "").strip() else "[dim]no[/]"
            table.add_row(
                row["qualified_name"],
                row["kind"],
                f"{row['file_path']}:{row['start_line']}",
                has_doc,
            )
        console.print(table)

    if dangling:
        any_rows = True
        _warn_dangling(dangling)

    if not any_rows:
        console.print("[green]Nothing to review — everything is documented and unchanged.[/]")


# --------------------------------------------------------------------------- #
# prune
# --------------------------------------------------------------------------- #
@app.command()
def prune() -> None:
    """Permanently remove soft-deleted symbols (and their tags/relations/history)."""
    cfg = _config_or_die()
    conn = db.connect(cfg.db_path)
    try:
        removed = db.purge_deleted(conn)
        db.rebuild_fts(conn)
        conn.commit()
    finally:
        conn.close()
    if removed:
        console.print(f"[green]Pruned {removed} deleted symbol(s) from the database.[/]")
    else:
        console.print("[green]Nothing to prune.[/]")


# --------------------------------------------------------------------------- #
# tag
# --------------------------------------------------------------------------- #
@app.command()
def tag(
    symbol: str = typer.Argument(..., help="Symbol name or qualified_name."),
    tags: list[str] = typer.Argument(..., help="One or more tags to add."),
) -> None:
    """Attach manual tags to a symbol."""
    cfg = _config_or_die()
    conn = db.connect(cfg.db_path)
    try:
        matches = db.find_symbols_by_ref(conn, symbol)
        if not matches:
            die(f"No symbol matches '{symbol}'. Run 'ctx scan' or check the name.")
        added = 0
        for row in matches:
            for t in tags:
                if db.add_tag(conn, t, source="manual", symbol_id=row["id"]):
                    added += 1
        db.rebuild_fts(conn)
        conn.commit()
    finally:
        conn.close()
    target = matches[0]["qualified_name"] if len(matches) == 1 else f"{len(matches)} symbols"
    console.print(f"[green]Added {added} tag(s)[/] to {target}: {', '.join(tags)}")


# --------------------------------------------------------------------------- #
# note add
# --------------------------------------------------------------------------- #
@note_app.command("add")
def note_add(
    title: str = typer.Option(..., "--title", "-t", prompt=True, help="Note title."),
    content: str = typer.Option(..., "--content", "-c", prompt=True, help="Note body."),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
    importance: str = typer.Option("normal", "--importance", "-i", help="low|normal|high"),
) -> None:
    """Add a manual context note."""
    cfg = _config_or_die()
    note = ContextNote(
        id=_gen_id("note", title, content),
        title=title,
        content=content,
        tags=_split_tags(tags),
        importance=importance,
    )
    conn = db.connect(cfg.db_path)
    try:
        db.add_note(conn, note)
        db.rebuild_fts(conn)
        conn.commit()
    finally:
        conn.close()
    console.print(f"[green]Note added[/] ({note.id}): {title}")


# --------------------------------------------------------------------------- #
# invariant add
# --------------------------------------------------------------------------- #
@invariant_app.command("add")
def invariant_add(
    scope: str = typer.Option(..., "--scope", "-s", prompt=True, help="Where it applies."),
    content: str = typer.Option(..., "--content", "-c", prompt=True, help="The rule."),
    severity: str = typer.Option("warning", "--severity", help="info|warning|error"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags."),
) -> None:
    """Add a manual invariant."""
    cfg = _config_or_die()
    inv = Invariant(
        id=_gen_id("inv", scope, content),
        scope=scope,
        content=content,
        severity=severity,
        tags=_split_tags(tags),
    )
    conn = db.connect(cfg.db_path)
    try:
        db.add_invariant(conn, inv)
        db.rebuild_fts(conn)
        conn.commit()
    finally:
        conn.close()
    console.print(f"[green]Invariant added[/] ({inv.id}) for scope '{scope}'")


# --------------------------------------------------------------------------- #
# ask
# --------------------------------------------------------------------------- #
# @ctx tag: context retrieval, ask command
# @ctx related: search, build_markdown, rebuild_fts
@app.command()
def ask(
    prompt: str = typer.Argument(..., help="What you want context about."),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Max results."),
    raw: bool = typer.Option(False, "--raw", help="Print plain Markdown (no panel)."),
) -> None:
    """Retrieve relevant context as compact Markdown (no AI is called)."""
    cfg = _config_or_die()
    conn = db.connect(cfg.db_path)
    try:
        hits = search(conn, prompt, limit or cfg.default_limit)
        markdown = build_markdown(conn, prompt, hits)
    finally:
        conn.close()

    if raw:
        print(markdown)
    else:
        console.print(Syntax(markdown, "markdown", theme="ansi_dark", word_wrap=True))


# --------------------------------------------------------------------------- #
# map
# --------------------------------------------------------------------------- #
@app.command()
def map() -> None:
    """Show a summary of scanned files and documentation coverage."""
    cfg = _config_or_die()
    conn = db.connect(cfg.db_path)
    try:
        summaries = project_map(conn)
    finally:
        conn.close()

    if not summaries:
        console.print("[yellow]No symbols yet. Run 'ctx scan' first.[/]")
        return

    table = Table(title="Project map", border_style="cyan")
    table.add_column("file")
    table.add_column("symbols", justify="right")
    table.add_column("documented", justify="right")
    table.add_column("undocumented", justify="right")
    tot = doc = 0
    for s in summaries:
        tot += s.total
        doc += s.documented
        table.add_row(
            s.file_path,
            str(s.total),
            f"[green]{s.documented}[/]",
            f"[magenta]{s.undocumented}[/]" if s.undocumented else "0",
        )
    table.add_section()
    table.add_row(
        f"[bold]{len(summaries)} files[/]",
        str(tot),
        f"[green]{doc}[/]",
        f"[magenta]{tot - doc}[/]",
    )
    console.print(table)


# --------------------------------------------------------------------------- #
# related
# --------------------------------------------------------------------------- #
@app.command()
def related(
    symbol: str = typer.Argument(..., help="Symbol name or qualified_name."),
) -> None:
    """Navigate `@ctx related:` edges for a symbol, in both directions."""
    cfg = _config_or_die()
    conn = db.connect(cfg.db_path)
    try:
        matches = db.find_symbols_by_ref(conn, symbol)
        if not matches:
            die(f"No symbol matches '{symbol}'. Run 'ctx scan' or check the name.")
        sym_ids = [m["id"] for m in matches]
        refs = sorted({m["name"] for m in matches} | {m["qualified_name"] for m in matches})
        outgoing = db.outgoing_relations(conn, sym_ids)
        incoming = db.incoming_relations(conn, refs)

        title = matches[0]["qualified_name"] if len(matches) == 1 else symbol
        console.print(f"[bold]Relations for[/] [cyan]{title}[/]")

        out_table = Table(title=f"→ outgoing ({len(outgoing)})", border_style="green")
        out_table.add_column("references")
        out_table.add_column("target location")
        for row in outgoing:
            target = db.find_symbols_by_ref(conn, row["to_ref"])
            loc = (
                f"{target[0]['file_path']}:{target[0]['start_line']}"
                if target
                else "[dim]unresolved[/]"
            )
            out_table.add_row(row["to_ref"], loc)

        in_table = Table(title=f"← incoming ({len(incoming)})", border_style="magenta")
        in_table.add_column("source")
        in_table.add_column("via ref")
        for row in incoming:
            in_table.add_row(_relation_source_label(conn, row), row["to_ref"])
    finally:
        conn.close()

    if not outgoing and not incoming:
        console.print(f"[yellow]No @ctx related edges touch '{title}'.[/]")
        return
    if outgoing:
        console.print(out_table)
    if incoming:
        console.print(in_table)


def _relation_source_label(conn, row) -> str:
    """Describe the 'from' side of a relation: its symbol, else its file:line."""
    if row["from_symbol_id"]:
        sym = db.get_symbol(conn, row["from_symbol_id"])
        if sym is not None:
            return f"{sym['qualified_name']} ({sym['file_path']}:{sym['start_line']})"
    if row["from_file_path"]:
        return f"<file> {row['from_file_path']}:{row['line']}"
    return "?"


def _dangling_rows(conn) -> list[tuple[str, str]]:
    """(to_ref, source label) for each `@ctx related:` edge that resolves to nothing."""
    return [(r["to_ref"], _relation_source_label(conn, r)) for r in db.dangling_relations(conn)]


def _warn_dangling(rows: list[tuple[str, str]]) -> None:
    """Print warnings for relations whose targets cannot be resolved."""
    if not rows:
        return
    console.print(
        f"\n[yellow]⚠ {len(rows)} unresolved @ctx related reference(s) "
        f"(possible typo or external target):[/]"
    )
    for to_ref, source in rows:
        console.print(f"  [yellow]·[/] [bold]{to_ref}[/] ← referenced by {source}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _split_tags(raw: str | None) -> list[str]:
    """Parse comma-separated CLI tag input into normalized tag names."""
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _gen_id(prefix: str, *parts: str) -> str:
    """Generate a timestamped id for manually-created notes/invariants."""
    digest = hashlib.sha1(("|".join((*parts, now_iso()))).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _gen_marker_id(prefix: str, *parts: object) -> str:
    """Deterministic id for marker-derived rows (no timestamp, so re-derivable)."""
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


if __name__ == "__main__":
    app()
