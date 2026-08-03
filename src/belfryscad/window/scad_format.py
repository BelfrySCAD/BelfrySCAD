"""Reformat/pretty-print an OpenSCAD source fragment for the code editor's
"Reformat Selection" context-menu item (see editor.py's contextMenuEvent).

Scope is deliberately limited to *structural* formatting -- statement/block
indentation, brace placement (K&R-style, `} else {` merged onto one line),
one statement per line, and collapsing runs of blank lines to at most one.
Anything inside parens/brackets (function args, vector literals, list
comprehensions, ...) is copied through verbatim, newlines and all, rather
than reflowed -- OpenSCAD has no accessible full-fidelity AST via
openscad_cpp_evaluator (RootScope only exposes a name->definition symbol
table, for Go to Definition, not a walkable statement tree), so this works
directly off a token stream instead of round-tripping through an AST.
Semicolons/braces are only treated as structural at paren/bracket depth 0,
which is always correct for valid OpenSCAD grammar (neither ever nests
inside a `(...)`/`[...]`).
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

_TOKEN_RE = re.compile(r'''
      (?P<ws>\s+)
    | (?P<linecomment>//[^\n]*)
    | (?P<blockcomment>/\*.*?\*/)
    | (?P<string>"(?:\\.|[^"\\])*")
    | (?P<word>[A-Za-z_$][A-Za-z0-9_]*)
    | (?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)
    | (?P<sym>.)
''', re.VERBOSE | re.DOTALL)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        tokens.append((kind, m.group()))
    return tokens


def can_format(text: str) -> bool:
    """Whether `text` parses standalone as its own tiny .scad file -- the
    gate for whether the "Reformat Selection" menu item is offered at all."""
    if not text.strip():
        return False
    from openscad_cpp_evaluator import parse as _oce_parse, ParseError
    with tempfile.NamedTemporaryFile(suffix=".scad", mode="w", encoding="utf-8",
                                      delete=False) as f:
        f.write(text)
        tmp_path = f.name
    try:
        _oce_parse(tmp_path)
        return True
    except ParseError:
        return False
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def format_scad(text: str, indent_size: int = 4) -> str:
    """Reformat `text` (assumed to already pass can_format) -- see module
    docstring for exactly what is and isn't normalized."""
    tokens = _tokenize(text)
    out: list[str] = []
    cur = ""
    indent = 0
    paren_depth = 0
    i = 0
    n = len(tokens)

    def indent_str() -> str:
        return " " * (indent * indent_size)

    # Tracks whether a newline has appeared (at paren_depth 0) since the
    # last line was actually emitted -- distinguishes a line comment that
    # trails the previous statement on the same original line (append to
    # that already-emitted line) from one that starts its own line.
    saw_newline_since_flush = True

    def flush() -> None:
        nonlocal cur, saw_newline_since_flush
        s = cur.rstrip()
        if s:
            out.append(s)
            saw_newline_since_flush = False
        cur = ""

    while i < n:
        kind, txt = tokens[i]

        if kind == "ws":
            if paren_depth > 0:
                cur += txt
            elif "\n" in txt:
                saw_newline_since_flush = True
                if not cur.strip() and txt.count("\n") >= 2:
                    if out and out[-1] != "":
                        out.append("")
            elif cur and not cur.endswith(" "):
                cur += " "
            i += 1
            continue

        if kind == "linecomment":
            if paren_depth > 0:
                cur += txt
            elif cur.strip():
                cur = cur.rstrip() + "  " + txt
                flush()
            elif out and not saw_newline_since_flush:
                out[-1] = out[-1] + "  " + txt
            else:
                cur = indent_str() + txt
                flush()
            i += 1
            continue

        if kind == "blockcomment":
            if paren_depth > 0:
                cur += txt
            elif not cur.strip():
                cur = indent_str() + txt
            else:
                cur += " " + txt
            i += 1
            continue

        if kind == "sym" and txt in "([":
            if not cur and paren_depth == 0:
                cur = indent_str()
            cur += txt
            paren_depth += 1
            i += 1
            continue

        if kind == "sym" and txt in ")]":
            cur += txt
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue

        if kind == "sym" and txt == "{" and paren_depth == 0:
            cur = (indent_str() + "{") if not cur.strip() else cur.rstrip() + " {"
            flush()
            indent += 1
            i += 1
            continue

        if kind == "sym" and txt == "}" and paren_depth == 0:
            flush()
            indent = max(0, indent - 1)
            cur = indent_str() + "}"
            j = i + 1
            while j < n and tokens[j][0] in ("ws", "linecomment", "blockcomment"):
                j += 1
            if j < n and tokens[j] == ("word", "else"):
                cur += " "
                i += 1
                continue
            flush()
            i += 1
            continue

        if kind == "sym" and txt == ";" and paren_depth == 0:
            cur = cur.rstrip() + ";"
            flush()
            i += 1
            continue

        # word / num / string / any other sym (operators, structural chars
        # while paren_depth > 0, modifier chars like # ! %) -- appended
        # verbatim, exactly as adjacent to neighboring tokens in the
        # original (spacing between them is controlled entirely by "ws"
        # tokens above).
        if not cur and paren_depth == 0:
            cur = indent_str()
        cur += txt
        i += 1

    flush()
    return "\n".join(out) + ("\n" if out else "")
