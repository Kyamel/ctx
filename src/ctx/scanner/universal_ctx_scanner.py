"""Language-agnostic scanner for `@ctx` marker comments.

It does not parse the host language; it scans line by line for markers of the
form `@ctx <kind>: <text>` and turns them into notes, invariants or tags.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ..models import ContextNote, Invariant, Symbol


# @ctx tag: ctx markers, extracted context
@dataclass
class CtxMarkers:
    """Collected results from scanning a file for `@ctx` markers."""

    notes: list[ContextNote] = field(default_factory=list)
    invariants: list[Invariant] = field(default_factory=list)
    # (line, tag) pairs — the line lets us resolve which symbol (if any) it binds to.
    tags: list[tuple[int, str]] = field(default_factory=list)
    # (line, referenced_name) pairs from `@ctx related:`.
    related: list[tuple[int, str]] = field(default_factory=list)


# kinds we recognise after the prefix
_KINDS = ("note", "warning", "invariant", "why", "tag", "related")


# @ctx tag: marker scanner, notes, invariants, tags, relations
# @ctx related: scan, resolve_symbol_for_marker, ContextNote, Invariant, Relation
class UniversalCtxScanner:
    """Extract `@ctx` comments from any text file, independent of language syntax."""

    def __init__(self, prefix: str = "@ctx") -> None:
        """Compile the marker regex for the configured prefix."""
        # The marker must be the first token on the line, ignoring only
        # indentation and a leading comment/quote opener (# // -- ; * <!-- """).
        # This rejects prose that merely *mentions* "<prefix> note:" mid-sentence
        # (e.g. inside a docstring) while still accepting a marker that is the
        # first word of a docstring line.
        escaped = re.escape(prefix)
        self._pattern = re.compile(
            rf"^\s*[#/;*\-!<>\"']*\s*"
            rf"{escaped}\s+(?P<kind>{'|'.join(_KINDS)})\s*:\s*(?P<body>.*)$",
            re.IGNORECASE,
        )

    def scan(self, rel_path: str, source: str) -> CtxMarkers:
        """Return all recognized `@ctx` markers found in *source*."""
        result = CtxMarkers()
        for lineno, line in enumerate(source.splitlines(), start=1):
            m = self._pattern.match(line)
            if not m:
                continue
            kind = m.group("kind").lower()
            body = m.group("body").strip()
            if not body:
                continue
            self._dispatch(kind, body, rel_path, lineno, result)
        return result

    def _dispatch(
        self, kind: str, body: str, rel_path: str, lineno: int, result: CtxMarkers
    ) -> None:
        """Append a parsed marker body to the appropriate result bucket."""
        marker_id = _marker_id(rel_path, lineno, kind, body)
        if kind in ("note", "why"):
            result.notes.append(
                ContextNote(
                    id=marker_id,
                    title=f"{kind} @ {rel_path}:{lineno}",
                    content=body,
                    importance="normal",
                    file_path=rel_path,
                    line=lineno,
                    source="ctx-marker",
                )
            )
        elif kind in ("invariant", "warning"):
            result.invariants.append(
                Invariant(
                    id=marker_id,
                    scope=rel_path,
                    content=body,
                    severity="error" if kind == "warning" else "warning",
                    file_path=rel_path,
                    line=lineno,
                    source="ctx-marker",
                )
            )
        elif kind == "tag":
            # Comma-separated only, so a multi-word tag stays a single tag.
            for tag in body.split(","):
                tag = tag.strip()
                if tag:
                    result.tags.append((lineno, tag))
        elif kind == "related":
            for ref in body.split(","):
                ref = ref.strip()
                if ref:
                    result.related.append((lineno, ref))


def _marker_id(rel_path: str, lineno: int, kind: str, body: str) -> str:
    """Return the deterministic id for a source marker."""
    raw = f"{rel_path}:{lineno}:{kind}:{body}"
    return "ctx_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# Lines allowed between a marker and the symbol below it for them to count as
# "physically attached": comments (#) and decorators (@). A blank line or any
# real code breaks the connection, sending the marker to file scope.
def _is_filler(text: str) -> bool:
    """Return whether text may sit between a marker and the symbol below it."""
    stripped = text.strip()
    return bool(stripped) and (stripped.startswith("#") or stripped.startswith("@"))


# @ctx tag: marker binding, symbol attachment
# @ctx note: Markers above comments/decorators attach below; inner markers attach inside.
def resolve_symbol_for_marker(
    line: int, symbols: list[Symbol], source_lines: list[str]
) -> str | None:
    """Decide which symbol (if any) a marker on *line* belongs to.

    Rules, in priority order:
      1. Physically above: the nearest symbol starting below the marker, when
         every line between them is a comment/decorator (no blank, no code).
      2. Contained: the innermost symbol whose body spans the marker line.
      3. Otherwise None — the marker belongs to the file/module.
    """
    below = [s for s in symbols if s.start_line > line]
    if below:
        target = min(below, key=lambda s: s.start_line)
        if all(
            _is_filler(source_lines[n - 1]) if 0 <= n - 1 < len(source_lines) else False
            for n in range(line + 1, target.start_line)
        ):
            return target.id

    containing = [s for s in symbols if s.start_line <= line <= s.end_line]
    if containing:
        return max(containing, key=lambda s: s.start_line).id

    return None
