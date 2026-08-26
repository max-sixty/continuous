"""The generator's file I/O must name UTF-8 rather than inherit the locale's.

`init` runs on the adopter's machine, not on a runner we control, and the
files it writes are committed to their repo and parsed by GitHub Actions as
UTF-8. Every generated workflow carries an em dash in its header comment, so
on a machine whose preferred encoding is not UTF-8 — Windows defaults to the
ANSI code page (cp1252) through Python 3.14, and PEP 540's UTF-8 mode is off
there — `Path.write_text` without `encoding=` writes that em dash as the
single byte 0x97 and the workflow file is no longer valid UTF-8. Nothing
downstream catches it: the write succeeds, `tend check` doesn't read the
files, and the corruption reaches the adopter's repo.

A static check rather than a locale-forcing one, because the property that
matters is "no call site inherits the locale" — that holds for call sites
added later, which a test that renders under one forced encoding would not
cover.
"""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "tend"

# `read_text`/`write_text` are text-only, so they always need `encoding=`.
# `open` needs it only in text mode.
TEXT_ONLY = {"read_text", "write_text"}


def _mode_arg(call: ast.Call) -> str | None:
    """The literal `mode` passed to an `open` call, if it is a literal.

    `mode` is the first positional argument of `Path.open` but the second of
    the builtin `open`, whose first is the path. Reading the wrong slot makes
    `path.open("rb")` look like a text-mode call.
    """
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    pos = 0 if isinstance(call.func, ast.Attribute) else 1
    if len(call.args) > pos and isinstance(call.args[pos], ast.Constant):
        return call.args[pos].value
    return None


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _offenders(source: str, path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in TEXT_ONLY | {"open"}:
            continue
        if name == "open":
            mode = _mode_arg(node)
            # An unreadable (non-literal) mode is treated as text: assuming
            # binary would be the direction that lets a real offender through.
            if mode is not None and "b" in mode:
                continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        found.append(f"{path.name}:{node.lineno}: {name}() without encoding=")
    return found


def test_generator_file_io_names_utf8() -> None:
    offenders = [
        line
        for py in sorted(PACKAGE.rglob("*.py"))
        for line in _offenders(py.read_text(encoding="utf-8"), py)
    ]
    assert not offenders, (
        "these calls inherit the locale's encoding instead of naming UTF-8, so "
        "on a non-UTF-8 machine they garble the generator's own templates "
        "or write a workflow file that is not valid UTF-8:\n  " + "\n  ".join(offenders)
    )


def test_the_check_would_catch_a_regression() -> None:
    # The guard above passes trivially if `_offenders` stops recognising the
    # call shapes, and it is the only thing standing between a new `write_text`
    # and a corrupt workflow file on an adopter's machine.
    source = """
        from pathlib import Path

        Path("a").write_text(x)
        Path("b").read_text()
        Path("c").open()
        open("d")
        open("e", "w")

        Path("ok1").write_text(x, encoding="utf-8")
        Path("ok2").read_text(encoding="utf-8")
        open("ok3", encoding="utf-8")
        open("ok4", "rb")
        open("ok5", mode="wb")
        # `mode` sits at a different position in each form; both are binary.
        Path("ok6").open("rb")
        Path("ok7").open(mode="wb")
    """
    offenders = _offenders(dedent(source), Path("sample.py"))
    assert [o.split(": ")[1] for o in offenders] == [
        "write_text() without encoding=",
        "read_text() without encoding=",
        "open() without encoding=",
        "open() without encoding=",
        "open() without encoding=",
    ]
