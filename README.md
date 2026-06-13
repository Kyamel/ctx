# ctx

`ctx` is a local **active project-memory** CLI. It scans your source code,
extracts symbols and documentation, stores the context in SQLite, detects
changes via Git/hashing, and produces compact Markdown context blocks to feed
into AI tools. No AI is called internally — `ctx` only *prepares* context.

## Stack

Python 3.12+ · uv · Typer · Rich · SQLite (+ FTS5) · Pydantic · tree-sitter
(reserved for future multi-language parsing) · pytest · ruff.

## Setup

This repo uses a Nix shell that provides the Python interpreter and `uv`; all
Python libraries are managed by `uv` (not Nix).

```sh
nix-shell          # provides python3.12 + uv + git
uv sync            # installs Python deps into .venv
uv run ctx --help
```

## Commands

| Command | What it does |
| --- | --- |
| `ctx init` | Create `.ctx/`, `context.db`, `config.toml`, and the schema. |
| `ctx scan` | Scan files, extract Python symbols (via `ast`) + `@ctx` markers, hash code/docs, record the Git commit, mark symbols `new`/`unchanged`/`changed`/`deleted`. |
| `ctx review` | Rich table of new, changed, deleted and undocumented symbols. |
| `ctx tag SYMBOL TAG...` | Add manual tags to a symbol (by name or qualified name). |
| `ctx note add` | Add a manual context note (title, content, tags, importance). |
| `ctx invariant add` | Add a manual invariant (scope, content, severity, tags). |
| `ctx ask "prompt"` | FTS5 search across symbols/docs/tags/notes/invariants; emits compact Markdown. |
| `ctx map` | Per-file symbol counts and documentation coverage. |

## `@ctx` markers

Drop these in a comment in *any* text file:

```python
# @ctx note: this module is load-bearing
# @ctx why: kept for backwards compatibility
# @ctx invariant: ids must stay unique
# @ctx warning: never call inside a loop
# @ctx tag: core, billing
# @ctx related: other_module
```

## Config (`.ctx/config.toml`)

```toml
[scan]
include = ["**/*.py"]
exclude = [".git/**", ".ctx/**", ".venv/**", "__pycache__/**", "dist/**", "build/**"]

[context]
default_limit = 8

[markers]
prefix = "@ctx"
```

## Tests

```sh
uv run pytest
```

## Architecture

```
src/ctx/
  cli.py          # Typer commands, scan orchestration
  config.py       # project discovery + config + file globbing rules
  db.py           # SQLite schema, upserts, status detection, FTS rebuild
  git.py          # subprocess git helpers
  models.py       # Pydantic models
  scanner/        # base protocol + file discovery, python (ast), universal @ctx
  context/        # FTS5 search + Markdown builder
  review/         # review buckets + project map queries
```

Scanners are pluggable: add a `Scanner` subclass and register it in
`cli.SCANNERS` to support a new language (tree-sitter is the intended path).
