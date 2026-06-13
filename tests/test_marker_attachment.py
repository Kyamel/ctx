"""Rules for binding @ctx markers to the symbol they physically belong to."""

from __future__ import annotations

from pathlib import Path

from ctx.scanner import PythonScanner, UniversalCtxScanner, resolve_symbol_for_marker


def _resolve(source: str):
    """Return {marker_line: qualified_name|None} for every @ctx marker in source."""
    symbols = list(PythonScanner().scan(Path("m.py"), "m.py", source))
    by_id = {s.id: s.qualified_name for s in symbols}
    lines = source.splitlines()
    markers = UniversalCtxScanner().scan("m.py", source)

    sites: dict[int, str | None] = {}
    for note in markers.notes:
        sid = resolve_symbol_for_marker(note.line, symbols, lines)
        sites[note.line] = by_id.get(sid) if sid else None
    for line_no, _tag in markers.tags:
        sid = resolve_symbol_for_marker(line_no, symbols, lines)
        sites[line_no] = by_id.get(sid) if sid else None
    return sites


def test_marker_directly_above_binds_to_symbol():
    src = "# @ctx tag: core\ndef core():\n    return 1\n"
    assert _resolve(src) == {1: "core"}


def test_marker_inside_body_binds_to_symbol():
    src = "def core():\n    x = 1\n    # @ctx note: about x\n    return x\n"
    assert _resolve(src) == {3: "core"}


def test_blank_line_breaks_connection_so_file_scope():
    # Marker at top, separated from the def by a blank line -> belongs to file.
    src = "# @ctx note: module level\n\ndef core():\n    return 1\n"
    assert _resolve(src) == {1: None}


def test_code_between_marker_and_def_is_file_scope():
    src = "# @ctx note: about x\nx = 1\ndef core():\n    return x\n"
    assert _resolve(src) == {1: None}


def test_directly_above_wins_over_containing_class():
    # Comment sits inside the class range but right above the method -> method.
    src = "class Foo:\n    # @ctx tag: greet\n    def greet(self):\n        return 1\n"
    assert _resolve(src) == {2: "Foo.greet"}


def test_decorator_between_marker_and_def_still_binds():
    src = "# @ctx tag: core\n@property\ndef core(self):\n    return 1\n"
    assert _resolve(src) == {1: "core"}


def test_marker_inside_class_but_not_above_method_binds_to_class():
    src = "class Foo:\n    x = 1\n    # @ctx note: class-level detail\n    y = 2\n"
    # No def directly below; the marker is contained in Foo's body.
    assert _resolve(src) == {3: "Foo"}
