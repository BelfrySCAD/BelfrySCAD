"""Reformat/pretty-print an OpenSCAD source fragment for the code editor's
"Reformat Selection" context-menu item (see editor.py's contextMenuEvent).

Scope is deliberately limited to *structural* formatting -- statement/block
indentation, brace placement (K&R-style, `} else {` merged onto one line),
one statement per line (including `include`/`use`, whose `<path>` has no
terminating semicolon and so needed its own rule -- without it the next
statement ran onto the end of the include), a modifier's child on its own
indented line, and collapsing runs of blank lines to at most one. An over-long argument list or
vector is then reflowed across lines (`_wrap_long_lists`); one that already
fits, or that the user wrapped by hand, is left as written.

The parser ships its own pretty-printer (`oscad::toOpenscad`, exposed to
Python as `format_source`), which is more thorough than this. It is not used
here deliberately: it drops the braces from a single-child block, writes
`l = 40` where this keeps `l=40`, and pulls a trailing `// comment` inside
the call it follows. Those are the user's formatting choices to keep, so
this pass stays token-based and touches only what it is asked to.
Structural formatting is done on a token stream. Separator spacing inside
argument lists and vector literals is then normalised from the AST
(`_space_separators`), which is possible because
`openscad_cpp_evaluator.parse_ast_string` exposes every node's source span:
each argument's own text is taken VERBATIM from its span, so nothing is
re-serialised and no expression printer is needed -- only the commas
between them are rewritten. Anything this pass can't account for is left
exactly as the user wrote it.
Semicolons/braces are only treated as structural at paren/bracket depth 0,
which is always correct for valid OpenSCAD grammar (neither ever nests
inside a `(...)`/`[...]`).
"""
from __future__ import annotations

import re

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
    gate for whether the "Reformat Selection" menu item is offered at all.

    Parses the string directly. This used to write a NamedTemporaryFile and
    parse that, because the only parse entry point took a path; parse_ast_string
    removes the file I/O, and with it the delete-on-failure cleanup and the
    chance of leaving a stray file behind. It runs on every right-click in
    the editor, so the saving is worth having.
    """
    if not text.strip():
        return False
    from openscad_cpp_evaluator import ParseError, parse_ast_string
    try:
        parse_ast_string(text)
        return True
    except ParseError:
        return False


def _is_include(line: str) -> bool:
    """Whether `line` so far is an `include`/`use` awaiting its `<path>`.

    Distinguishes that `<` from a less-than: only these two statements take
    an angle-bracketed path, and only as the whole statement.
    """
    return re.fullmatch(r"\s*(include|use)\s*", line) is not None


def _is_declaration(line: str) -> bool:
    """Whether `line` is a module/function declaration.

    Its parentheses hold a parameter list, so what follows is the body, not
    a child to be indented under a modifier.
    """
    return re.match(r"\s*(module|function)\b", line) is not None


def format_scad(text: str, indent_size: int = 4) -> str:
    """Reformat `text` (assumed to already pass can_format) -- see module
    docstring for exactly what is and isn't normalized."""
    tokens = _tokenize(text)
    out: list[str] = []
    cur = ""
    indent = 0
    paren_depth = 0
    # Extra indent for a modifier's child, e.g. the `cube(1)` in
    # `translate(...) cube(1);`. Separate from `indent`, which only braces
    # move, and reset by the `;` that ends the statement.
    chain = 0
    # Inside the `<...>` of an `include`/`use`. Those have no terminating
    # semicolon, so nothing else here would ever end the line -- the next
    # statement was being run onto the end of the include. The path is also
    # copied verbatim while this is set, since whitespace in it is part of
    # a filename rather than something to normalise.
    in_path = False
    i = 0
    n = len(tokens)

    def indent_str() -> str:
        return " " * ((indent + chain) * indent_size)

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
            if paren_depth > 0 or in_path:
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

        if kind == "sym" and txt == "<" and not in_path and _is_include(cur):
            cur += txt
            in_path = True
            i += 1
            continue

        if kind == "sym" and txt == ">" and in_path:
            cur += txt
            in_path = False
            flush()
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
            # A modifier's child goes on its own line, indented under it:
            # `translate(...) cube(1);` reads as one thing acting on
            # another, and running them together hides that. Only when the
            # next thing really is a child -- `{` opens a block (handled
            # below, K&R), `;` ends the statement, and `=` means this was a
            # function declaration's parameter list, not a call.
            if paren_depth == 0 and txt == ")":
                j = i
                # Skip newlines too, not just spaces. A statement is joined
                # onto one line before this runs, so an already-broken
                # chain arrives with a newline here -- refusing to break on
                # one meant reformatting twice gave two different results.
                while j < n and tokens[j][0] == "ws":
                    j += 1
                if (j < n and tokens[j][0] in ("word", "num", "string")
                        and tokens[j][1] != "else"
                        and not _is_declaration(cur)):
                    flush()
                    chain += 1
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
            chain = 0
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
    formatted = "\n".join(out) + ("\n" if out else "")

    # Separator spacing is a second, AST-driven pass over the already
    # structurally-formatted text (see _space_separators). Gated on the
    # result still parsing to the same shape: this function REPLACES the
    # user's selection, so a bad rewrite silently corrupts their code.
    # Any doubt at all and the token-formatted text is returned unchanged.
    spaced = _space_separators(formatted)
    if not _same_shape(formatted, spaced):
        spaced = formatted
    # Reflow last, so it sees the normalised `, ` spacing and measures the
    # lines it will actually produce.
    return _wrap_long_lists(spaced, WRAP_WIDTH, indent_size)


# ---------------------------------------------------------------------------
# AST-driven separator spacing
# ---------------------------------------------------------------------------

# Node kinds whose children are a comma-separated list the user sees as one
# "argument list": a call's arguments, and a vector/list-comprehension's
# elements. Mapped to the dict key holding that list.
_SEPARATED_LISTS = {
    "ModularCall": "arguments",
    "PrimaryCall": "arguments",
    "ListComprehension": "elements",
    "ModularEcho": "arguments",
    "ModularAssert": "arguments",
    "EchoOp": "arguments",
    "AssertOp": "arguments",
    "FunctionLiteral": "parameters",
    "ModuleDeclaration": "parameters",
    "FunctionDeclaration": "parameters",
}


def _walk(node, out: list) -> None:
    """Collect every dict node in the tree, parents before children."""
    if isinstance(node, dict) and "kind" in node:
        out.append(node)
        for key, val in node.items():
            if key not in ("kind", "position"):
                _walk(val, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def _space_separators(text: str) -> str:
    """Normalise `a,b` to `a, b` between arguments and vector elements.

    Works purely on spans: each item's own source text is copied verbatim,
    and only the gap BETWEEN two consecutive items is rewritten. Nothing is
    re-serialised, so `1.500`, `1e3`, comments and hand-formatting inside an
    argument all survive untouched.

    A gap is only touched when it is exactly a comma surrounded by plain
    horizontal whitespace. Any gap containing a newline (a deliberately
    wrapped list) or a comment is left alone -- those are choices the user
    made, and this pass has no business overriding them.
    """
    from openscad_cpp_evaluator import ParseError, parse_ast_string

    try:
        nodes = parse_ast_string(text, True)
    except ParseError:
        return text

    flat: list = []
    _walk(nodes, flat)

    # (start, end, replacement) for each gap, collected across the whole
    # tree then applied back-to-front so earlier offsets stay valid.
    edits: list[tuple[int, int, str]] = []
    for node in flat:
        key = _SEPARATED_LISTS.get(node["kind"])
        if not key:
            continue
        items = node.get(key) or []

        # A string literal's span currently covers only its opening quote
        # (a parser bug, openscad_cpp_evaluator <= 0.17.0), which drags the
        # enclosing argument's end_offset back into the middle of the
        # string. Every offset derived from such an item is then wrong, so
        # skip the whole list rather than trust any gap in it -- without
        # this, `f("h,l,height,length", x)` had a comma rewritten INSIDE
        # the string, silently changing its value (caught on BOSL2's
        # isosurface.scad).
        # A string literal's span currently covers only its opening quote
        # (a parser bug, openscad_cpp_evaluator <= 0.17.0). That drags the
        # enclosing item's end_offset into the middle of the string, and an
        # outer list's offsets with it, so a "gap" can land inside string
        # CONTENT. An unbalanced quote count in an item's own span text is
        # the tell. Caught on BOSL2's isosurface.scad, where a comma inside
        # "h,l,height,length" was being rewritten -- silently changing the
        # string's value.
        if any(text[i["position"]["start_offset"]:i["position"]["end_offset"]].count('"') % 2
               for i in items):
            continue

        for left, right in zip(items, items[1:]):
            gap_start = left["position"]["end_offset"]
            gap_end = right["position"]["start_offset"]
            if gap_start >= gap_end:
                continue  # spans touch or overlap -- nothing to normalise
            gap = text[gap_start:gap_end]
            if gap == ", ":
                continue  # already correct; skip the no-op edit
            if '"' in gap:
                continue  # belt-and-braces: never rewrite across a string
            if re.fullmatch(r"[ \t]*,[ \t]*", gap):
                edits.append((gap_start, gap_end, ", "))

    if not edits:
        return text

    out = text
    for start, end, replacement in sorted(edits, reverse=True):
        out = out[:start] + replacement + out[end:]

    # Self-verify rather than trusting the span arithmetic above. This
    # rewrites a user's source, and the spans it relies on have already
    # been shown to be wrong in at least one case; a structural check
    # costs one extra parse and makes the function safe by construction
    # whatever else turns out to be off. Callers get the input back
    # unchanged rather than a plausible-looking corruption.
    return out if _same_shape(text, out) else text


#: Longest line the reflow pass leaves alone. A list that already fits is
#: never touched, so short calls keep the shape the user gave them.
WRAP_WIDTH = 80


def _line_indent(text: str, offset: int) -> str:
    """The leading whitespace of the line `offset` falls on."""
    start = text.rfind("\n", 0, offset) + 1
    line = text[start:].split("\n")[0]
    return line[:len(line) - len(line.lstrip())]


def _line_len(text: str, offset: int) -> int:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", start)
    return (len(text) if end < 0 else end) - start


def _wrap_one_long_list(text: str, width: int, indent_size: int):
    """Reflow the outermost over-long comma list, or None if none is.

    One per call, with the caller re-parsing in between: wrapping an outer
    list moves everything inside it, and re-deriving spans is easier to be
    sure of than keeping several rewrites' offsets consistent. A nested
    list then wraps on a later round, by which point its own indentation is
    already right.
    """
    from openscad_cpp_evaluator import ParseError, parse_ast_string

    try:
        nodes = parse_ast_string(text, True)
    except ParseError:
        return None
    flat: list = []
    _walk(nodes, flat)

    best = None
    for node in flat:
        key = _SEPARATED_LISTS.get(node["kind"])
        if not key:
            continue
        items = node.get(key) or []
        if len(items) < 2:
            continue
        pos = node.get("position") or {}
        start, end = pos.get("start_offset"), pos.get("end_offset")
        if start is None or end is None or _line_len(text, start) <= width:
            continue
        # The same span hazard `_space_separators` documents: a string
        # literal's span can end mid-string, dragging every offset around
        # it out of place. An unbalanced quote count is the tell.
        if any(text[i["position"]["start_offset"]:i["position"]["end_offset"]].count('"') % 2
               for i in items):
            continue
        if any("\n" in text[a["position"]["end_offset"]:b["position"]["start_offset"]]
               for a, b in zip(items, items[1:])):
            continue      # already wrapped by hand -- that was a choice
        if best is None or start < best[0]:
            best = (start, end, items)

    if best is None:
        return None

    start, end, items = best
    outer = _line_indent(text, start)
    inner = outer + " " * indent_size
    prefix = text[start:items[0]["position"]["start_offset"]].rstrip()
    suffix = text[items[-1]["position"]["end_offset"]:end].lstrip()
    pieces = [text[i["position"]["start_offset"]:i["position"]["end_offset"]]
              for i in items]

    # Greedy fill rather than one item per line: a long vector of numbers
    # reads as a block of data, and one element per line turns a 60-point
    # path into three screens of scrolling.
    lines: list[str] = []
    cur = inner
    for k, piece in enumerate(pieces):
        chunk = piece + ("," if k < len(pieces) - 1 else "")
        if cur != inner and len(cur) + 1 + len(chunk) > width:
            lines.append(cur)
            cur = inner + chunk
        else:
            cur = cur + chunk if cur == inner else cur + " " + chunk
    lines.append(cur)
    return text[:start] + prefix + "\n" + "\n".join(lines) + "\n" + outer + suffix + text[end:]


def _wrap_long_lists(text: str, width: int = WRAP_WIDTH, indent_size: int = 4) -> str:
    """Reflow every over-long argument list and vector literal.

    Verified the way `_space_separators` is: a rewrite that changes the
    parse tree is discarded and the last good text returned. The round cap
    is a backstop against a rewrite that never settles, not something a
    real selection is expected to reach.
    """
    out = text
    for _ in range(200):
        nxt = _wrap_one_long_list(out, width, indent_size)
        if nxt is None or nxt == out or not _same_shape(out, nxt):
            break
        out = nxt
    return out if _same_shape(text, out) else text


def _shape(text: str):
    """A structure-only fingerprint of `text`'s AST: node kinds and nesting,
    with every position and literal value dropped. Two sources with the same
    fingerprint differ at most in whitespace between tokens."""
    from openscad_cpp_evaluator import parse_ast_string

    def walk(node):
        if isinstance(node, dict) and "kind" in node:
            return (node["kind"], tuple(
                walk(v) for k, v in sorted(node.items()) if k not in ("kind", "position")))
        if isinstance(node, list):
            return tuple(walk(x) for x in node)
        return node

    return walk(parse_ast_string(text, True))


def _same_shape(before: str, after: str) -> bool:
    """Whether a rewrite left the parse tree structurally identical.

    The safety gate on any transformation applied to a user's selection: if
    a rewrite drops an argument, moves a comment, or changes an operator's
    grouping, the fingerprints diverge and the rewrite is discarded. A parse
    failure on either side counts as unsafe.
    """
    from openscad_cpp_evaluator import ParseError
    try:
        return _shape(before) == _shape(after)
    except (ParseError, Exception):
        return False
