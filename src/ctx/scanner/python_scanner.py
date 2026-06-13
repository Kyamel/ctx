"""Python symbol extraction using the standard-library `ast` module."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from ..models import Symbol
from .base import Scanner, hash_text, symbol_id

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


# @ctx tag: python scanner, reference implementation
# @ctx related: Scanner, Symbol, scan
# @ctx note: Template: can_handle gates files; scan yields symbols; _make_symbol normalizes.
class PythonScanner(Scanner):
    """Extract Python classes, functions and methods using the stdlib AST."""

    language = "python"

    def can_handle(self, path: Path) -> bool:
        """Return True for Python source files."""
        return path.suffix == ".py"

    def scan(self, path: Path, rel_path: str, source: str) -> Iterator[Symbol]:
        """Yield top-level function/class symbols plus direct class methods."""
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            # A file we cannot parse simply yields no symbols (friendly degrade).
            return
        lines = source.splitlines()

        for node in tree.body:
            if isinstance(node, _FUNC_TYPES):
                yield self._make_symbol(node, node.name, "function", rel_path, lines, None)
            elif isinstance(node, ast.ClassDef):
                cls_sym = self._make_symbol(node, node.name, "class", rel_path, lines, None)
                yield cls_sym
                yield from self._scan_methods(node, cls_sym, rel_path, lines)

    def _scan_methods(
        self, cls: ast.ClassDef, cls_sym: Symbol, rel_path: str, lines: list[str]
    ) -> Iterator[Symbol]:
        """Yield direct methods for a class and link them to the class symbol."""
        for node in cls.body:
            if isinstance(node, _FUNC_TYPES):
                qname = f"{cls.name}.{node.name}"
                yield self._make_symbol(node, qname, "method", rel_path, lines, cls_sym.id)

    def _make_symbol(
        self,
        node: ast.AST,
        qualified_name: str,
        kind: str,
        rel_path: str,
        lines: list[str],
        parent_id: str | None,
    ) -> Symbol:
        """Build the normalized `Symbol` object stored by the database layer."""
        name = qualified_name.split(".")[-1]
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)
        snippet = "\n".join(lines[start - 1 : end])
        doc = ast.get_docstring(node) if isinstance(node, (ast.ClassDef, *_FUNC_TYPES)) else None
        signature = _signature(node) if isinstance(node, _FUNC_TYPES) else f"class {name}"

        return Symbol(
            id=symbol_id(rel_path, qualified_name, kind),
            language=self.language,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            file_path=rel_path,
            start_line=start,
            end_line=end,
            signature=signature,
            documentation=doc,
            documentation_kind="docstring",
            code_hash=hash_text(snippet),
            documentation_hash=hash_text(doc),
            parent_symbol_id=parent_id,
        )


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a simple `name(args)` signature from the function's arguments."""
    a = node.args
    parts: list[str] = []

    posonly = list(a.posonlyargs)
    args = list(a.args)
    defaults = list(a.defaults)
    # Defaults align to the tail of posonly+args.
    all_positional = posonly + args
    offset = len(all_positional) - len(defaults)
    for idx, arg in enumerate(all_positional):
        if idx - offset >= 0:
            parts.append(f"{arg.arg}={_unparse(defaults[idx - offset])}")
        else:
            parts.append(arg.arg)
        if posonly and idx == len(posonly) - 1:
            parts.append("/")

    if a.vararg:
        parts.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        parts.append("*")

    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=False):
        if default is not None:
            parts.append(f"{arg.arg}={_unparse(default)}")
        else:
            parts.append(arg.arg)

    if a.kwarg:
        parts.append(f"**{a.kwarg.arg}")

    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({', '.join(parts)})"


def _unparse(node: ast.AST) -> str:
    """Best-effort source rendering for default argument values."""
    try:
        return ast.unparse(node)
    except Exception:
        return "..."
