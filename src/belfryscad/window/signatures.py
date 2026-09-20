"""Signature strings for the editor's argument hint (issue #517).

The reporter's words: OpenSCAD's autocomplete is "incredibly useful -- not
for autocompletion, but to save me from having to view documentation on
syntax", because typing `(` pops up the argument list. Completion of names
already worked here; showing what the arguments ARE did not.

Two sources, because the two kinds of callable know different things:

* **Builtins** have no `.scad` declaration to read, so their signatures are
  written down below. There is no way to derive them.
* **Everything else** -- the script's own modules and functions, and every
  one reached through `include`/`use` -- is found via the parsed scope,
  which gives the declaring file and line. The declaration is then read
  back off disk. The scope's entries carry `name` and `position` and
  nothing about parameters, so reading the source is the only route, and
  it has the happy property of showing exactly what the author wrote,
  defaults included.

Qt-free on purpose: the parsing and lookup are the parts worth testing, and
they test without a widget or a GL context.
"""
from __future__ import annotations

import re
from functools import lru_cache

#: Argument lists for the builtins the editor knows (they match
#: CodeEditor's highlighting sets). Written out rather than derived: a
#: builtin has no declaration anywhere to read. Defaults are shown where
#: OpenSCAD documents one, so the hint answers "what can I pass?" and "what
#: happens if I don't?" at once.
BUILTIN_SIGNATURES: dict[str, str] = {
    # 3D primitives
    "cube": "cube(size=1, center=false)",
    "sphere": "sphere(r | d, $fa, $fs, $fn)",
    "cylinder": "cylinder(h, r1 | d1, r2 | d2, center=false, $fa, $fs, $fn)",
    "polyhedron": "polyhedron(points, faces, convexity=1)",
    # 2D primitives
    "circle": "circle(r | d, $fa, $fs, $fn)",
    "square": "square(size=1, center=false)",
    "polygon": "polygon(points, paths, convexity=1)",
    "text": ("text(t, size=10, font, halign=\"left\", valign=\"baseline\", "
             "spacing=1, direction=\"ltr\", language=\"en\", script=\"latin\", $fn)"),
    # transforms
    "translate": "translate(v) { ... }",
    "rotate": "rotate(a) | rotate(a, v) { ... }",
    "scale": "scale(v) { ... }",
    "resize": "resize(newsize, auto=false, convexity=10) { ... }",
    "mirror": "mirror(v) { ... }",
    "multmatrix": "multmatrix(m) { ... }",
    "color": "color(c, alpha=1.0) | color(\"name\", alpha) { ... }",
    "offset": "offset(r | delta, chamfer=false) { ... }",
    # CSG
    "union": "union() { ... }",
    "difference": "difference() { ... }",
    "intersection": "intersection() { ... }",
    "hull": "hull() { ... }",
    "minkowski": "minkowski(convexity) { ... }",
    "render": "render(convexity=1) { ... }",
    # extrusion / import
    "linear_extrude": ("linear_extrude(height, center=false, convexity=10, "
                       "twist=0, slices, scale=1.0, $fn)"),
    "rotate_extrude": "rotate_extrude(angle=360, convexity=2, $fa, $fs, $fn)",
    "surface": "surface(file, center=false, invert=false, convexity=1)",
    "projection": "projection(cut=false) { ... }",
    "import": "import(file, convexity=1, layer, $fn)",
    "roof": "roof(method=\"voronoi\", convexity=2) { ... }",
    # flow / misc modules
    "children": "children(index | [start:end] | vector)",
    "echo": "echo(value, ...)",
    "assert": "assert(condition, message)",
    # math functions
    "abs": "abs(x)", "sign": "sign(x)", "ceil": "ceil(x)", "floor": "floor(x)",
    "round": "round(x)", "sqrt": "sqrt(x)", "ln": "ln(x)", "exp": "exp(x)",
    "log": "log(x)", "pow": "pow(base, exponent)",
    "sin": "sin(degrees)", "cos": "cos(degrees)", "tan": "tan(degrees)",
    "asin": "asin(x)", "acos": "acos(x)", "atan": "atan(x)",
    "atan2": "atan2(y, x)",
    "max": "max(a, b, ...) | max(vector)",
    "min": "min(a, b, ...) | min(vector)",
    "norm": "norm(vector)", "cross": "cross(a, b)",
    "rands": "rands(min, max, count, seed)",
    # list / string functions
    "concat": "concat(a, b, ...)", "len": "len(list | string)",
    "str": "str(a, b, ...)", "chr": "chr(number | vector | range)",
    "ord": "ord(character)",
    "search": "search(match, list, num_returns_per_match=1, index_col_num=0)",
    "lookup": "lookup(key, <[key, value], ...>)",
    # type tests
    "is_undef": "is_undef(value)", "is_bool": "is_bool(value)",
    "is_num": "is_num(value)", "is_string": "is_string(value)",
    "is_list": "is_list(value)", "is_function": "is_function(value)",
    "is_object": "is_object(value)",
    # other
    "version": "version()", "version_num": "version_num()",
    "parent_module": "parent_module(index)",
    "object": "object(key=value, ...)",
    "textmetrics": "textmetrics(text, size, font, halign, valign, spacing, direction, language, script)",
    "fontmetrics": "fontmetrics(size, font)",
}

#: `module NAME(` or `function NAME(`, at the start of a declaration.
_DECL_RE = re.compile(r'\b(module|function)\s+([A-Za-z_]\w*)\s*\(')


def _balanced_through(text: str, open_at: int) -> int | None:
    """Index just past the `)` matching the `(` at `open_at`, or None.

    Counts only parens outside strings and comments -- a default like
    `msg=")"` or a trailing `// (see below)` would otherwise close the list
    early and truncate the hint. BOSL2 has both.
    """
    depth = 0
    i, n = open_at, len(text)
    while i < n:
        c = text[i]
        if c in '"\'':
            quote = c
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == '\\' else 1
        elif c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def signature_from_source(text: str, name: str, line: int) -> str | None:
    """Pull `name`'s declaration out of `text`, starting at 1-based `line`.

    Declarations routinely span many lines -- BOSL2's `cuboid()` puts one
    parameter per line over a dozen of them -- so this reads on until the
    parens balance rather than taking the one line the scope points at.
    """
    lines = text.splitlines()
    if not (1 <= line <= len(lines)):
        return None
    # Search from the declaring line; a decorator-free language means the
    # match is on that line itself, but allow a little slack for a scope
    # position that points at a leading comment.
    window = "\n".join(lines[line - 1:line + 60])
    for m in _DECL_RE.finditer(window):
        if m.group(2) != name:
            continue
        end = _balanced_through(window, m.end() - 1)
        if end is None:
            return None
        return _tidy(window[m.start():end])
    return None


def _tidy(decl: str) -> str:
    """One line, single-spaced, comments stripped -- a tooltip, not a diff."""
    decl = re.sub(r'/\*.*?\*/', ' ', decl, flags=re.S)
    decl = re.sub(r'//[^\n]*', ' ', decl)
    decl = re.sub(r'\s+', ' ', decl).strip()
    return re.sub(r'\(\s+', '(', re.sub(r'\s+\)', ')', decl))


@lru_cache(maxsize=256)
def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def signature_for(name: str, scope=None, live_text: str = None,
                  live_origin: str = None) -> str | None:
    """The hint for `name`, or None if nothing is known about it.

    Builtins win: a script may define `module cube()` of its own, but the
    hint is for the name being typed and the builtin table is the answer
    the language guarantees.

    `live_text`/`live_origin` let a declaration in the buffer being edited
    be read from the buffer rather than from disk, so a signature is right
    as soon as it is typed rather than after the next save.
    """
    builtin = BUILTIN_SIGNATURES.get(name)
    if builtin is not None:
        return builtin
    if scope is None:
        return None
    for attr in ('modules', 'functions'):
        table = getattr(scope, attr, None)
        if not isinstance(table, dict):
            continue
        decl = table.get(name)
        if decl is None:
            continue
        pos = getattr(decl, 'position', None)
        origin, line = getattr(pos, 'origin', None), getattr(pos, 'line', None)
        if not origin or not line:
            continue
        if live_text is not None and live_origin and origin == live_origin:
            text = live_text
        else:
            text = _read(origin)
        if text is None:
            continue
        sig = signature_from_source(text, name, int(line))
        if sig:
            return sig
    return None
