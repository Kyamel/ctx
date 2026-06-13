from __future__ import annotations

from pathlib import Path

from ctx.scanner import PythonScanner

SOURCE = '''\
def top_level(a, b=2, *args, **kwargs):
    """A module function."""
    return a + b


class Greeter:
    """Greets people."""

    def greet(self, name: str) -> str:
        """Return a greeting."""
        return f"hi {name}"

    async def shout(self, name):
        return name.upper()
'''


def _scan(text: str):
    scanner = PythonScanner()
    return list(scanner.scan(Path("mod.py"), "mod.py", text))


def test_extracts_functions_classes_methods():
    symbols = {s.qualified_name: s for s in _scan(SOURCE)}
    assert set(symbols) == {"top_level", "Greeter", "Greeter.greet", "Greeter.shout"}

    assert symbols["top_level"].kind == "function"
    assert symbols["Greeter"].kind == "class"
    assert symbols["Greeter.greet"].kind == "method"


def test_method_parent_link():
    symbols = {s.qualified_name: s for s in _scan(SOURCE)}
    assert symbols["Greeter.greet"].parent_symbol_id == symbols["Greeter"].id


def test_signature_and_docstring():
    symbols = {s.qualified_name: s for s in _scan(SOURCE)}
    sig = symbols["top_level"].signature
    assert "top_level(" in sig and "b=2" in sig and "*args" in sig and "**kwargs" in sig
    assert symbols["Greeter.greet"].documentation == "Return a greeting."
    assert symbols["Greeter.shout"].documentation is None


def test_code_hash_changes_on_edit():
    a = {s.qualified_name: s for s in _scan(SOURCE)}
    edited = SOURCE.replace("return a + b", "return a + b + 1")
    b = {s.qualified_name: s for s in _scan(edited)}
    assert a["top_level"].id == b["top_level"].id  # identity stable
    assert a["top_level"].code_hash != b["top_level"].code_hash


def test_syntax_error_degrades_gracefully():
    assert _scan("def broken(:\n") == []
