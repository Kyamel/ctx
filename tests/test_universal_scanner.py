from __future__ import annotations

from ctx.scanner import UniversalCtxScanner

SOURCE = """\
# @ctx note: this module is load-bearing
# @ctx why: kept for backwards compat
# @ctx invariant: ids must be unique
// @ctx warning: never call this in a loop
# @ctx tag: core, billing
# @ctx related: other_module
plain code line
"""


def test_collects_all_marker_kinds():
    markers = UniversalCtxScanner().scan("a.py", SOURCE)
    assert len(markers.notes) == 2  # note + why
    assert len(markers.invariants) == 2  # invariant + warning
    assert {t for _, t in markers.tags} == {"core", "billing"}
    assert markers.related == [(6, "other_module")]  # (line, ref)


def test_warning_is_error_severity():
    markers = UniversalCtxScanner().scan("a.py", "// @ctx warning: boom")
    assert markers.invariants[0].severity == "error"


def test_custom_prefix():
    markers = UniversalCtxScanner(prefix="@mem").scan("a.py", "# @mem note: hi")
    assert markers.notes[0].content == "hi"


def test_tags_split_on_comma_only_not_whitespace():
    markers = UniversalCtxScanner().scan("a.py", "# @ctx tag: app entry point, core")
    assert [t for _, t in markers.tags] == ["app entry point", "core"]


def test_marker_must_be_first_token_not_mid_prose():
    src = (
        '    """Insert a `@ctx related:` edge, returning True."""\n'  # prose -> ignored
        "x = '@ctx note: not a marker'\n"  # embedded in code -> ignored
        "# @ctx note: real comment marker\n"  # comment -> kept
        "    @ctx tag: docstring-first-word\n"  # first word of a docstring line -> kept
        "    * @ctx why: block comment line\n"  # block-comment continuation -> kept
    )
    m = UniversalCtxScanner().scan("a.py", src)
    assert [n.content for n in m.notes] == ["real comment marker", "block comment line"]
    assert [t for _, t in m.tags] == ["docstring-first-word"]
    assert m.related == []  # the prose mention created no relation
