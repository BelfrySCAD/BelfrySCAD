"""Find the colour literal under a position in a line of OpenSCAD source.

Two shapes, both lexical -- this runs on the line the mouse is over, with
no AST and no render behind it, so it has to work on whatever half-typed
text is there:

* a quoted string Qt knows as a colour -- a CSS/SVG name (`"red"`,
  `"SteelBlue"`) or `#rgb` / `#rrggbb`;
* a 3- or 4-element vector whose components are all literal numbers in
  0..1, which is OpenSCAD's own range for `color()` (`[1, 0, 0]`,
  `[0.2, 0.4, 0.9, 0.5]`).

`QColor` is the name table on purpose: the evaluator's own `css_colors.cpp`
was generated from a live PySide6 install, so the two agree by construction
-- including on excluding `rebeccapurple`, which CSS3 has and Qt does not.

Being a colour is only half of it: the literal also has to sit somewhere
that means a colour, which is what `in_color_context` decides. A vector of
three numbers in range is a colour or a point depending entirely on where
it is written, and `translate([0.5, 0, 0.2])` is not asking about colour.

Everything here works on ONE line -- the line the mouse is over. A call
split across lines (`color(\\n    "red")`) is not matched. That is the price
of answering with no AST, which is what lets this work on the half-typed
lines the parser rejects.
"""
from __future__ import annotations

import re

from PySide6.QtGui import QColor

#: A double-quoted string with no escapes. A colour name or `#rrggbb` never
#: contains a backslash, so anything that does is not one.
_STRING = re.compile(r'"([^"\\\n]*)"')

#: The innermost `[...]`, so a point inside `[[0,0,0], [1,1,1]]` finds the
#: triple it is actually in rather than the outer list.
_VECTOR = re.compile(r'\[([^\[\]\n]*)\]')

#: A plain numeric literal. Not an expression: `[r, g, b]` where the
#: components are variables cannot be resolved without evaluating, and this
#: has to answer from the text alone.
_NUMBER = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\Z')

_HEX = re.compile(r'#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z')
_NAME = re.compile(r'[A-Za-z]+\Z')


def color_for_name(text: str) -> QColor | None:
    """The colour a string literal's CONTENT names, or None.

    Only the two spellings OpenSCAD itself takes: a bare name, and `#rgb`
    or `#rrggbb`. Qt would also accept `#rrrgggbbb`, `#aarrggbb` and
    `transparent`, which would tooltip colours no OpenSCAD build renders.
    """
    if not (_NAME.match(text) or _HEX.match(text)):
        return None
    c = QColor.fromString(text)
    return c if c.isValid() else None


def looks_like_color_vector(parts: list[str]) -> QColor | None:
    """The colour a vector's components spell, or None.

    Three or four literal numbers, every one of them in 0..1 -- OpenSCAD's
    own range for `color()`. Nothing in a vector's text says whether it is a
    colour or a point, so this is only half the test; `in_color_context`
    is the other half.
    """
    if len(parts) not in (3, 4):
        return None
    vals = []
    for p in parts:
        p = p.strip()
        if not _NUMBER.match(p):
            return None
        v = float(p)
        if not 0.0 <= v <= 1.0:
            return None
        vals.append(v)
    return QColor.fromRgbF(*vals)


#: Calls whose arguments are colours. `recolor()` and `color_this()` are
#: BOSL2's; `color()` is the language's own.
_COLOR_CALLS = frozenset({"color", "recolor", "color_this"})

#: Argument names that mean a colour. `c=` is BOSL2's short spelling, and is
#: accepted only as an ARGUMENT -- a bare `c = [0.5, 0.5, 0.5];` says
#: nothing about what it holds.
_COLOR_ARGS = frozenset({"color", "colour", "c"})

_COLORISH = re.compile(r"colou?r", re.IGNORECASE)
_IDENT_BEFORE = re.compile(r"([A-Za-z_$][A-Za-z0-9_]*)\s*\Z")
#: A single `=`, not `==`/`<=`/`>=`/`!=`.
_ASSIGN_BEFORE = re.compile(r"(?<![=!<>])=\s*\Z")


def _blanked(text: str) -> str:
    """`text` with string bodies and any `//` comment replaced by spaces.

    Offsets are preserved, so the structural scan below can look for the
    `(` and `=` that mean something without tripping over one written
    inside a string or a comment.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
        elif text.startswith("//", i):
            for k in range(i, n):
                out[k] = " "
            break
        else:
            i += 1
    return "".join(out)


def in_color_context(text: str, start: int) -> bool:
    """Does the literal at `start` sit somewhere that means a colour?

    Three ways, all lexical and all on this line alone:

    * an argument of `color()`, `recolor()` or `color_this()`, however
      deeply nested inside it;
    * the right-hand side of an assignment whose name says colour --
      `thecolor = [0.5, 0.75, 1.0];` yes, `p1 = [0.5, 0.75, 1.0];` no,
      since the two are identical as text and only the name tells them
      apart;
    * a `color=` / `colour=` / `c=` argument of any call.

    Without this, every `translate([0.5, 0, 0.2])` and every path point in
    range got a swatch -- the components of a colour and of a point are the
    same numbers.
    """
    before = _blanked(text)[:start]

    m = _ASSIGN_BEFORE.search(before)
    if m:
        name_m = _IDENT_BEFORE.search(before[:m.start()])
        if name_m:
            name = name_m.group(1)
            lead = before[:name_m.start(1)].rstrip()
            is_argument = lead.endswith(("(", ","))
            if is_argument and name.lower() in _COLOR_ARGS:
                return True
            if _COLORISH.search(name):
                return True

    # Every call still open at `start`, innermost last: a colour one
    # anywhere in that chain counts, so `color(concat(c, [1]))` works.
    depth_stack = []
    for i, ch in enumerate(before):
        if ch == "(":
            depth_stack.append(i)
        elif ch == ")" and depth_stack:
            depth_stack.pop()
    for open_paren in depth_stack:
        name_m = _IDENT_BEFORE.search(before[:open_paren])
        if name_m and name_m.group(1).lower() in _COLOR_CALLS:
            return True
    return False


def find_color_literal(text: str, offset: int):
    """`(start, end, QColor)` for the colour literal `offset` sits in.

    Strings first: `color("red")` has the string inside a `(...)`, not a
    `[...]`, so the two shapes never overlap, but a string *inside* a vector
    (`["red", 1]`) should be read as the string it is.

    The literal has to be BOTH a colour and in a colour position -- see
    `in_color_context`.
    """
    for m in _STRING.finditer(text):
        if m.start() <= offset <= m.end():
            c = color_for_name(m.group(1))
            if c and in_color_context(text, m.start()):
                return (m.start(), m.end(), c)
            return None

    for m in _VECTOR.finditer(text):
        if m.start() <= offset <= m.end():
            c = looks_like_color_vector(m.group(1).split(","))
            if c and in_color_context(text, m.start()):
                return (m.start(), m.end(), c)
            return None

    return None


def describe(c: QColor) -> str:
    """The colour in the spellings the literal does not show: its name if it
    has one, `#rrggbb`, and the 0..1 components `color()` takes."""
    r, g, b, a = c.getRgbF()
    parts = [c.name(QColor.NameFormat.HexRgb)]
    for name, known in _NAMES.items():
        if known == c.rgb():
            parts.insert(0, name)
            break
    nums = ", ".join(f"{v:.3g}" for v in (r, g, b))
    parts.append(f"[{nums}]" if a >= 1.0 else f"[{nums}, {a:.3g}]")
    return " · ".join(parts)


#: name -> packed rgb, for the reverse lookup `describe` does. Built once
#: from Qt's own list, which is the same list `color_for_name` resolves.
_NAMES = {n: QColor.fromString(n).rgb() for n in QColor.colorNames()}
