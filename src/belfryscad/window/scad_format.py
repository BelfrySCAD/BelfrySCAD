"""Reformat/pretty-print an OpenSCAD source fragment for the code editor's
"Reformat Selection" context-menu item (see editor.py's contextMenuEvent).

How freely lines may be broken is a `FormatProfile` (#466) -- Compact,
Default or Expanded -- and the wrap column is a parameter the editor fills
from its rightmost column guide rather than the old hard-coded 80. Brace
style is not a setting: K&R in every profile.

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
from dataclasses import dataclass

#: Fallback wrap column when a caller has no column guide to offer.
WRAP_WIDTH = 80

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


@dataclass(frozen=True)
class FormatProfile:
    """What a reformat is allowed to spend vertical space on (#466).

    Brace style is deliberately NOT a setting: K&R everywhere, so a profile
    cannot disagree with another about code all three format the same way.
    A parameter with one value in every profile is just the behaviour.
    """

    #: `let(a = 1) cube(a);` -- put the child statement on its own indented
    #: line. The rule that produces most of the vertical space people object
    #: to, since it fires on every transform chain as well.
    break_chained_child: bool = True
    #: Wrap an argument list whatever its length, rather than only one that
    #: overruns. Argument and parameter lists only: a bare vector literal is
    #: data, and one element per line turns a 60-point path into three
    #: screens of scrolling.
    wrap_every_list: bool = False
    #: One argument per line when wrapping, instead of a greedy fill.
    wrap_one_per_line: bool = False
    #: Collapse a run of blank lines to one.
    collapse_blank_lines: bool = True


#: The built-in profiles, in the order the menu offers them.
PROFILES: dict[str, FormatProfile] = {
    "Compact": FormatProfile(break_chained_child=False),
    "Default": FormatProfile(),
    "Expanded": FormatProfile(wrap_every_list=True, wrap_one_per_line=True,
                          collapse_blank_lines=False),
}

DEFAULT_PROFILE = "Default"


def format_scad(text: str, indent_size: int = 4,
                profile: FormatProfile | None = None,
                width: int = WRAP_WIDTH) -> str:
    """Reformat `text` (assumed to already pass can_format) -- see module
    docstring for exactly what is and isn't normalized.

    `profile` chooses how freely lines may be broken (see FormatProfile);
    `width` is the column to wrap at, which callers take from the editor's
    rightmost column guide so the formatter and the guide agree."""
    profile = profile or PROFILES[DEFAULT_PROFILE]
    tokens = _tokenize(text)
    out: list[str] = []
    cur = ""
    indent = 0
    paren_depth = 0
    # `[...]` nesting on its own. A newline inside one is the author's
    # arrangement of data; inside a `(...)` it is argument layout.
    bracket_depth = 0
    # A `//` comment is running and the newline that ends it has not been
    # seen yet. Collapsing THAT newline swallows the rest of the argument
    # list into the comment: `max(a,  // why` + `b);` became
    # `max(a,  // why b);`, which still parses and computes something else.
    open_line_comment = False
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
            if in_path:
                cur += txt
            elif paren_depth > 0:
                # A newline inside an ARGUMENT list is layout this pass is
                # about to decide for itself, so collapse it and let the
                # profile re-wrap -- otherwise a list some other profile
                # already broke apart survives every later reformat, and
                # switching profiles does nothing. Inside a `[...]` it is
                # the author's own arrangement of data (a matrix in rows, a
                # path a point per line) and is left alone.
                # ...nor the newline BEFORE one: pulling a comment up onto
                # the previous line re-attaches it to a different node, so
                # the rewrite is no longer purely whitespace.
                next_is_comment = (i + 1 < n
                                   and tokens[i + 1][0] in ("linecomment",
                                                            "blockcomment"))
                if ("\n" in txt and bracket_depth == 0
                        and not open_line_comment and not next_is_comment):
                    closing = (i + 1 < n and tokens[i + 1][0] == "sym"
                               and tokens[i + 1][1] in ")],;")
                    if (cur and not closing
                            and not cur.endswith((" ", "(", "["))):
                        cur += " "
                else:
                    cur += txt
                if "\n" in txt:
                    open_line_comment = False
            elif "\n" in txt:
                saw_newline_since_flush = True
                # Mid-statement: with break_chained_child off nothing
                # flushed here, so without this `translate(...)` and its
                # child were glued into `translate(...)cuboid(`.
                if cur.strip() and not cur.endswith(" "):
                    cur += " "
                if not cur.strip() and txt.count("\n") >= 2:
                    if profile.collapse_blank_lines:
                        if out and out[-1] != "":
                            out.append("")
                    elif out:
                        out.extend([""] * (txt.count("\n") - 1))
            elif cur and not cur.endswith(" "):
                cur += " "
            i += 1
            continue

        if kind == "linecomment":
            if paren_depth > 0:
                cur += txt
                open_line_comment = True
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
            if txt == "[":
                bracket_depth += 1
            i += 1
            continue

        if kind == "sym" and txt in ")]":
            cur += txt
            paren_depth = max(0, paren_depth - 1)
            if txt == "]":
                bracket_depth = max(0, bracket_depth - 1)
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
                        and not _is_declaration(cur)
                        and profile.break_chained_child):
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
    # Break at the expression's own joints first; wrapping an argument list
    # is the fallback for what is still too long after that.
    broken = _break_function_bodies(spaced, width, indent_size)
    return _wrap_long_lists(broken, width, indent_size, profile)


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

#: Of those, the ones `wrap_every_list` fires on regardless of length: the
#: argument and parameter lists a person would call "the arguments".
#: `ListComprehension` is excluded on purpose -- `translate([1, 0, 0])` is
#: one argument that happens to be a vector, and data does not read better
#: one number per line.
_ARGUMENT_LISTS = frozenset(_SEPARATED_LISTS) - {"ListComprehension"}


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


def _line_indent(text: str, offset: int) -> str:
    """The leading whitespace of the line `offset` falls on."""
    start = text.rfind("\n", 0, offset) + 1
    line = text[start:].split("\n")[0]
    return line[:len(line) - len(line.lstrip())]


def _line_len(text: str, offset: int) -> int:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", start)
    return (len(text) if end < 0 else end) - start


def _col_at(text: str, pos: int) -> int:
    """The column `pos` sits at, counting from 1."""
    return pos - (text.rfind("\n", 0, pos) + 1)


def _wrap_one_long_list(text: str, width: int, indent_size: int,
                        profile: FormatProfile):
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
        if start is None or end is None:
            continue
        always = profile.wrap_every_list and node["kind"] in _ARGUMENT_LISTS
        if not always:
            if _line_len(text, start) <= width:
                continue
            # Measured against where the ITEMS end, not where the node
            # does: a `FunctionDeclaration` spans its body too, so
            # `function foo(a, b, c) = <long body>;` looked over-long and
            # split `a, b, c` across three lines to fix a line whose
            # length was all body. A list that itself fits is not what
            # makes the line too long -- skipping it lets the round fall
            # through to the one that really does overflow. This is also
            # the rule as a person states it: keep the arguments inline
            # unless there are a lot of them.
            if _col_at(text, items[-1]["position"]["end_offset"]) <= width:
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

    chunks = [p + ("," if k < len(pieces) - 1 else "")
              for k, p in enumerate(pieces)]
    if profile.wrap_one_per_line:
        lines = [inner + chunk for chunk in chunks]
    else:
        # Greedy fill rather than one item per line: a long vector of
        # numbers reads as a block of data, and one element per line turns a
        # 60-point path into three screens of scrolling.
        lines = []
        cur = inner
        for chunk in chunks:
            if cur != inner and len(cur) + 1 + len(chunk) > width:
                lines.append(cur)
                cur = inner + chunk
            else:
                cur = cur + chunk if cur == inner else cur + " " + chunk
        lines.append(cur)
    return text[:start] + prefix + "\n" + "\n".join(lines) + "\n" + outer + suffix + text[end:]


def _wrap_long_lists(text: str, width: int = WRAP_WIDTH, indent_size: int = 4,
                     profile: FormatProfile | None = None) -> str:
    """Reflow every over-long argument list and vector literal.

    Verified the way `_space_separators` is: a rewrite that changes the
    parse tree is discarded and the last good text returned. The round cap
    is a backstop against a rewrite that never settles, not something a
    real selection is expected to reach.
    """
    profile = profile or PROFILES[DEFAULT_PROFILE]
    out = text
    for _ in range(200):
        nxt = _wrap_one_long_list(out, width, indent_size, profile)
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


# -- Finding the transform wrapper a gizmo should edit ----------------------

@dataclass
class TransformCall:
    """A `name(...)` call found immediately before some offset."""
    start: int              # offset of the `name` token
    end: int                # offset just past the closing `)`
    args: list              # positional argument texts, in order
    named: dict             # name -> argument text


def _skip_back_trivia(text: str, i: int) -> int:
    """Back over whitespace and comments ending at `i`."""
    while i > 0:
        j = i
        while j > 0 and text[j - 1] in " \t\r\n":
            j -= 1
        if j >= 2 and text[j - 2:j] == "*/":
            k = text.rfind("/*", 0, j - 2)
            if k < 0:
                return j
            j = k
        else:
            # A line comment only counts if the run back to the line start
            # really is one -- "//" inside a string is not.
            nl = text.rfind("\n", 0, j)
            line = text[nl + 1:j]
            pos = line.find("//")
            if pos >= 0 and line[:pos].count('"') % 2 == 0:
                j = nl + 1 + pos
            else:
                return j
        if j == i:
            return j
        i = j
    return i


def _match_back_paren(text: str, close: int):
    """Offset of the `(` matching the `)` at `close`, or None.

    Counts nesting and steps over strings and comments, which a regex
    cannot: `translate([f("a)b"), 0, 0])` closes where it looks like it
    does not.
    """
    depth = 0
    i = close
    while i >= 0:
        c = text[i]
        if c == '"':
            j = i - 1
            while j >= 0:
                if text[j] == '"' and (j == 0 or text[j - 1] != "\\"):
                    break
                j -= 1
            i = j - 1
            continue
        if c == "/" and i > 0 and text[i - 1] == "*":
            k = text.rfind("/*", 0, i)
            if k < 0:
                return None
            i = k - 1
            continue
        if c in ")]}":
            depth += 1
        elif c in "([{":
            depth -= 1
            if depth == 0:
                return i if c == "(" else None
        i -= 1
    return None


def _split_args(text: str) -> list:
    """Top-level comma split, ignoring commas inside brackets or strings."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(text):
        c = text[i]
        if c == '"':
            j = i + 1
            while j < len(text):
                if text[j] == '"' and text[j - 1] != "\\":
                    break
                j += 1
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail or out:
        out.append(tail)
    return out


def find_transform_call(source: str, before: int, name: str):
    """The `name(...)` call ending immediately before `before`, or None.

    Replaces a regex that matched only a literal three-element
    `translate([a, b, c])` anchored to the node, and so missed a named
    argument, a two-element vector, a comment between the wrapper and its
    child, or any nesting -- inserting a SECOND wrapper each time instead
    of updating the one that was there (#452).
    """
    i = _skip_back_trivia(source, before)
    if i <= 0 or source[i - 1] != ")":
        return None
    open_paren = _match_back_paren(source, i - 1)
    if open_paren is None:
        return None
    j = _skip_back_trivia(source, open_paren)
    k = j
    while k > 0 and (source[k - 1].isalnum() or source[k - 1] in "_$"):
        k -= 1
    if source[k:j] != name:
        return None
    if k > 0 and (source[k - 1].isalnum() or source[k - 1] in "_$."):
        return None            # part of a longer identifier
    args, named = [], {}
    for piece in _split_args(source[open_paren + 1:i - 1]):
        if not piece:
            continue
        eq = -1
        depth = 0
        for n, c in enumerate(piece):
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "=" and depth == 0 and (n + 1 >= len(piece) or piece[n + 1] != "="):
                eq = n
                break
        if eq > 0:
            named[piece[:eq].strip()] = piece[eq + 1:].strip()
        else:
            args.append(piece)
    return TransformCall(k, i, args, named)


#: Calls a new transform may be inserted OUTSIDE of. Everything else stops
#: the walk, and deliberately: `for (i=[0:3]) translate(...) cube();` is a
#: wrapper too, but hoisting a drag outside the loop would move every
#: iteration rather than the one the user grabbed. Same for `if`, a
#: `difference()` operand, or a user module's call.
TRANSFORM_WRAPPERS = ("translate", "rotate", "scale", "mirror", "resize",
                      "multmatrix", "color")


def transform_at(source: str, at: int, name: str):
    """A `name(...)` call that BEGINS at `at`, or None.

    The counterpart to `find_transform_call`, which only ever looks
    backwards. A selected span does not always sit after its wrappers: when
    the evaluator attributes a body to the statement rather than to the call
    inside it, the span *starts with* the very transform a drag should
    update. Scanning backwards from there finds nothing, and the drag then
    wrapped the statement in a second one.
    """
    j = at
    while j < len(source) and (source[j].isalnum() or source[j] in "_$"):
        j += 1
    if source[at:j] != name:
        return None
    k = j
    while k < len(source) and source[k] in " \t\r\n":
        k += 1
    if k >= len(source) or source[k] != "(":
        return None
    depth = 0
    i = k
    while i < len(source):
        c = source[i]
        if c == '"':
            i += 1
            while i < len(source) and not (source[i] == '"' and source[i - 1] != "\\"):
                i += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                end = i + 1
                return _build_transform_call(source, at, k, end)
        i += 1
    return None


def _build_transform_call(source: str, start: int, open_paren: int, end: int):
    args, named = [], {}
    for piece in _split_args(source[open_paren + 1:end - 1]):
        if not piece:
            continue
        eq, depth = -1, 0
        for n, c in enumerate(piece):
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == "=" and depth == 0 and (n + 1 >= len(piece) or piece[n + 1] != "="):
                eq = n
                break
        if eq > 0:
            named[piece[:eq].strip()] = piece[eq + 1:].strip()
        else:
            args.append(piece)
    return TransformCall(start, end, args, named)


def find_transform_chain(source: str, before: int) -> list:
    """The transform calls wrapping the node at `before`, innermost first.

    Walks out while each enclosing call is one of `TRANSFORM_WRAPPERS`.
    Stops at anything else, so the chain only ever contains calls a new
    transform can be safely hoisted outside of.
    """
    chain = []
    at = before
    while True:
        found = None
        for name in TRANSFORM_WRAPPERS:
            call = find_transform_call(source, at, name)
            if call is not None:
                found = (name, call)
                break
        if found is None:
            return chain
        name, call = found
        chain.append((name, call))
        at = call.start


def vector_texts(call: TransformCall, keyword: str, size: int, fill: str):
    """Each component of `call`'s vector argument as its ORIGINAL text,
    padded to `size` with `fill`, or None if there is no vector there.

    Components are kept verbatim, expressions included, so a nudge can be
    appended to one rather than replacing it (or, worse, stacking another
    wrapper around the whole call).
    """
    text = call.named.get(keyword) or (call.args[0] if call.args else None)
    if not text:
        return None
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    parts = [p.strip() for p in _split_args(text[1:-1]) if p.strip() != "" or True]
    parts = [p for p in parts if p != ""]
    if not parts or len(parts) > size:
        return None
    return parts + [fill] * (size - len(parts))


def _fmt_num(v: float) -> str:
    return f"{v:.4g}"


#: A trailing ` + 5` / ` - 2.5` / ` * 3` this function itself appended, so a
#: second nudge extends it instead of chaining `base/2 + 1 + 1 + 1`.
_TRAILING_DELTA = re.compile(r'^(?P<head>.*?)\s*(?P<op>[+\-*])\s*(?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?)$')


def nudge_component(text: str, amount: float, mode: str) -> str:
    """`text` adjusted by `amount`, keeping an expression intact.

    `mode` is "add" (translate, rotate) or "mul" (scale). A plain number is
    recomputed; anything else has the adjustment appended, because
    replacing `wall/2` with `7.5` throws away the relationship the user
    wrote -- and wrapping the whole call in another transform, which is
    what used to happen, accumulates wrappers on every drag.

    Scale parenthesises: `w+1` scaled by 2 is `(w+1)*2`, never `w+1*2`.
    """
    text = text.strip()
    # An untouched component keeps its own spelling. Recomputing it would
    # reformat text the drag never moved -- 1e3 to 1000, 1.500 to 1.5 --
    # which is the churn docs/wysiwyg.md records against the old rewrite.
    if amount == (0 if mode == "add" else 1):
        return text
    try:
        return _fmt_num(float(text) + amount) if mode == "add" \
            else _fmt_num(float(text) * amount)
    except ValueError:
        pass
    if mode == "add":
        m = _TRAILING_DELTA.match(text)
        if m and m.group("op") in "+-":
            base = float(m.group("num")) * (1 if m.group("op") == "+" else -1)
            total = base + amount
            if total == 0:
                return m.group("head")
            sign = "+" if total > 0 else "-"
            return f"{m.group('head')} {sign} {_fmt_num(abs(total))}"
        sign = "+" if amount > 0 else "-"
        return f"{text} {sign} {_fmt_num(abs(amount))}"
    m = _TRAILING_DELTA.match(text)
    if m and m.group("op") == "*":
        return f"{m.group('head')} * {_fmt_num(float(m.group('num')) * amount)}"
    needs_parens = any(c in text for c in "+-") and not text.lstrip("-").replace(".", "").isdigit()
    base = f"({text})" if needs_parens else text
    return f"{base} * {_fmt_num(amount)}"


# -- Finding the value under the cursor ------------------------------------

def _arg_spans(text: str, start: int, end: int):
    """Spans of the top-level comma-separated arguments in `text[start:end]`,
    as absolute offsets with surrounding whitespace trimmed off.

    Position-aware, unlike `_split_args`, which strips each piece and so
    loses the offsets a caret has to be matched against.
    """
    spans, depth, seg = [], 0, start
    i = start
    while i < end:
        c = text[i]
        if c == '"':
            i += 1
            while i < end and not (text[i] == '"' and text[i - 1] != "\\"):
                i += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            spans.append((seg, i))
            seg = i + 1
        i += 1
    spans.append((seg, end))
    out = []
    for a, b in spans:
        while a < b and text[a] in " \t\r\n":
            a += 1
        while b > a and text[b - 1] in " \t\r\n":
            b -= 1
        if b > a:
            out.append((a, b))
    return out


def _enclosing_group(text: str, offset: int):
    """(open_index, close_index) of the innermost `(...)` or `[...]` that
    contains `offset`, or None. Strings and nesting are respected."""
    depth, opener = 0, None
    i, stack = 0, []
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while i < len(text) and not (text[i] == '"' and text[i - 1] != "\\"):
                i += 1
        elif c in "([":
            stack.append(i)
        elif c in ")]":
            if stack:
                start = stack.pop()
                if start < offset <= i:
                    return start, i
        i += 1
    return None


_NUMBER_CHARS = "0123456789."


def _split_argument_name(text: str, start: int, end: int):
    """`(name, start, end)` with a leading `name =` dropped, so `angle=90`
    nudges the 90 rather than growing an `angle=90 + 15`, and the name is
    still available to decide the step size. Only a bare identifier followed
    by a single `=` counts -- `a == b` is a comparison, not an argument."""
    i = start
    while i < end and (text[i].isalnum() or text[i] in "_$"):
        i += 1
    if i == start:
        return None, start, end
    j = i
    while j < end and text[j] in " \t":
        j += 1
    if j >= end or text[j] != "=" or text[j + 1:j + 2] == "=":
        return None, start, end
    j += 1
    while j < end and text[j] in " \t":
        j += 1
    if j >= end:
        return None, start, end
    return text[start:i], j, end


def find_value_span(text: str, offset: int):
    """(start, end) of the number or expression the cursor sits in, or None.

    Two shapes, in order:

    * inside a `(...)` or `[...]` -- the comma-separated argument containing
      the cursor, so `translate([1, 2, 3])` nudges the component the caret
      is in and `left(wall/2)` nudges the whole expression;
    * otherwise the number token under or just before the cursor, which
      covers `wall = 3;` and `$fn = 64;`.

    Deliberately not transform-aware: what encloses the value does not
    matter, which is why `cube(10)` and `$fn` work as well as `xrot(45)`.
    """
    group = _enclosing_group(text, offset)
    if group is not None:
        open_i, close_i = group
        for seg_start, seg_end in _arg_spans(text, open_i + 1, close_i):
            if seg_start <= offset <= seg_end:
                return _split_argument_name(text, seg_start, seg_end)[1:]
        return None

    i = offset
    if i > 0 and (i >= len(text) or text[i] not in _NUMBER_CHARS):
        i -= 1
    if i < 0 or i >= len(text) or text[i] not in _NUMBER_CHARS:
        return None
    start = i
    while start > 0 and text[start - 1] in _NUMBER_CHARS:
        start -= 1
    end = i
    while end < len(text) and text[end] in _NUMBER_CHARS:
        end += 1
    if start > 0 and text[start - 1] == "-":
        start -= 1
    return start, end


#: Calls whose arguments are angles, so a nudge steps in degrees rather
#: than units. BOSL2's axis-specific spellings included.
ROTATION_CALLS = ("rotate", "rot", "xrot", "yrot", "zrot")

#: Argument names that hold an angle whatever the call is, so
#: `rotate_extrude(angle=90)` and `cyl(chamfang=30)` step in degrees too.
_ANGLE_ARGS = ("a", "angle", "ang", "chamfang", "spin", "twist")


def enclosing_call_name(text: str, offset: int):
    """The identifier of the call whose argument list contains `offset`, or
    None. Used only to choose a step size -- nothing about finding or
    rewriting the value depends on it."""
    group = _enclosing_group(text, offset)
    while group is not None:
        open_i, _close = group
        j = open_i
        while j > 0 and text[j - 1] in " \t\r\n":
            j -= 1
        k = j
        while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_$"):
            k -= 1
        if k < j:
            return text[k:j]
        if open_i == 0:
            return None
        group = _enclosing_group(text, open_i)
    return None


def is_angle_value(text: str, offset: int) -> bool:
    """Whether the value at `offset` is an angle -- inside a rotation call,
    or bound to an `a=`/`angle=` argument."""
    if enclosing_call_name(text, offset) in ROTATION_CALLS:
        return True
    group = _enclosing_group(text, offset)
    if group is None:
        return False
    for seg_start, seg_end in _arg_spans(text, group[0] + 1, group[1]):
        if seg_start <= offset <= seg_end:
            return _split_argument_name(text, seg_start, seg_end)[0] in _ANGLE_ARGS
    return False


# ---------------------------------------------------------------------------
# Reflowing a comment block (#467)
#
# Rewraps a run of `//` comment lines to a width, repeating each line's own
# leading pattern -- what vim's `gq` does. Editing BOSL2 documentation means
# rewrapping by hand otherwise, which is the whole ask.

#: `indent + // + spaces`, captured so a wrapped line can repeat it verbatim.
_COMMENT_PREFIX = re.compile(r"^([ \t]*//+[ \t]*)(.*)$")


def comment_prefix(line: str) -> str | None:
    """The `//`-and-spacing a comment line begins with, or None if the line
    is not a whole-line comment. `"//   Makes a widget."` gives `"//   "`.

    Trailing comments (`cube(1); // why`) deliberately return None: the code
    before them is not text to be rewrapped."""
    m = _COMMENT_PREFIX.match(line)
    return m.group(1) if m else None


def comment_block_at(lines: list[str], index: int) -> tuple[int, int] | None:
    """The half-open run of lines around `index` sharing its exact prefix.

    Exact, not merely "both are comments", and that is the point: in

        // Description:
        //   Makes a widget of the given size, with a hole
        //   that goes all the way through.

    the body is `//   ` and the header is `// `, so reflowing from inside the
    body rewraps the two body lines and leaves the header alone. A looser
    rule would fold the header into the paragraph and destroy the block."""
    if not (0 <= index < len(lines)):
        return None
    prefix = comment_prefix(lines[index])
    if prefix is None:
        return None
    start = index
    while start > 0 and comment_prefix(lines[start - 1]) == prefix:
        start -= 1
    end = index + 1
    while end < len(lines) and comment_prefix(lines[end]) == prefix:
        end += 1
    return start, end


def reflow_comment(lines: list[str], width: int) -> list[str]:
    """`lines` (all sharing one prefix) rewrapped to `width` columns total.

    A comment line with nothing after the `//` is a paragraph break: it is
    kept as-is and the paragraphs on either side wrap separately, so a doc
    comment's structure survives. Long words are never broken -- a URL or a
    `some_function()` split across lines would stop being either."""
    import textwrap

    if not lines:
        return []
    prefix = comment_prefix(lines[0]) or ""
    # The prefix is re-applied by textwrap, so measure the body against what
    # is left of the width. A prefix wider than the target would give a
    # negative width and raise; one column always remains.
    body_width = max(1, width - len(prefix.expandtabs()))

    out: list[str] = []
    para: list[str] = []

    def flush():
        if not para:
            return
        out.extend(textwrap.wrap(
            " ".join(para), width=body_width,
            initial_indent=prefix, subsequent_indent=prefix,
            break_long_words=False, break_on_hyphens=False,
        ) or [prefix.rstrip()])
        para.clear()

    for line in lines:
        # Strip each line's OWN prefix, not the block's: a bare `//` is a
        # paragraph break, and it does not start with a `//   ` body prefix,
        # so measuring against the block's would leave the slashes behind
        # and wrap them into the text as a word.
        own = comment_prefix(line)
        body = line[len(own):].strip() if own is not None else line.strip()
        if body:
            para.append(body)
        else:
            flush()
            out.append(prefix.rstrip())          # a blank comment line
    flush()
    return out


# ---------------------------------------------------------------------------
# Breaking a function body at its own structure
#
# A function whose body does not fit used to be joined onto the signature
# line and then rescued by wrapping whatever argument list happened to be
# there, which reads worse than the input it replaced:
#
#     function substr_match(str, start, pattern) = assert(
#         _is_liststr(str), "str must be a string or list"
#     ) assert(...) len(str)-start <len(pattern)? false : _substr_match_recurse(
#         str, start, pattern, len(pattern)
#     );
#
# Breaking at the expression's own joints instead gives BOSL2's own house
# style, which is what Default is meant to look like: the body on its own
# indented line, each leading `assert()`/`echo()` clause on a line, and a
# ternary chain broken after each `:`.

#: Clauses that may precede a function body proper, each taking a line.
_BODY_PREFIX_CALLS = ("assert", "echo")


def _depth_split(tokens):
    """Yield `(index, kind, text, depth)` for `tokens`, depth counting any
    bracket. A `:` only ends a ternary arm at depth 0 -- inside `[0:c]` it
    is a range, and inside a call it belongs to someone else."""
    depth = 0
    for i, (kind, txt) in enumerate(tokens):
        if kind == "sym" and txt in "([{":
            depth += 1
        elif kind == "sym" and txt in ")]}":
            depth -= 1
            yield i, kind, txt, depth
            continue
        yield i, kind, txt, depth


def _split_function_line(line: str, indent_size: int) -> list[str] | None:
    """`line` as signature + body lines, or None if it is not a function
    declaration this can help."""
    stripped = line.lstrip()
    if not stripped.startswith("function "):
        return None
    outer = line[:len(line) - len(stripped)]
    inner = outer + " " * indent_size

    tokens = _tokenize(stripped)
    eq = None
    for i, kind, txt, depth in _depth_split(tokens):
        if depth == 0 and kind == "sym" and txt == "=":
            eq = i
            break
    if eq is None:
        return None

    head = "".join(t for _, t in tokens[:eq]).rstrip()
    body_tokens = tokens[eq + 1:]
    # Drop the leading whitespace token the body starts with.
    while body_tokens and body_tokens[0][0] == "ws":
        body_tokens.pop(0)
    if not body_tokens:
        return None

    lines = [outer + head + " ="]
    cut = 0
    # Leading assert()/echo() clauses, one per line.
    while True:
        # Skip the whitespace between clauses -- without this only the
        # first assert() was recognised and the second kept whatever
        # followed it on the same line.
        while cut < len(body_tokens) and body_tokens[cut][0] == "ws":
            cut += 1
        rest = body_tokens[cut:]
        if not (rest and rest[0][0] == "word" and rest[0][1] in _BODY_PREFIX_CALLS):
            break
        close = None
        for i, kind, txt, depth in _depth_split(rest):
            if kind == "sym" and txt == ")" and depth == 0:
                close = i
                break
        if close is None:
            break
        lines.append(inner + "".join(t for _, t in rest[:close + 1]).strip())
        cut += close + 1

    # What is left splits after each depth-0 `:` -- the colon stays at the
    # end of its line, which is how the file being matched writes it.
    rest = body_tokens[cut:]
    piece: list[str] = []
    for i, kind, txt, depth in _depth_split(rest):
        piece.append(txt)
        if depth == 0 and kind == "sym" and txt == ":":
            lines.append(inner + "".join(piece).strip())
            piece = []
    if piece:
        lines.append(inner + "".join(piece).strip())

    return lines if len(lines) > 1 else None


def _break_function_bodies(text: str, width: int, indent_size: int) -> str:
    """Break every over-long function declaration at its own structure.

    Runs before `_wrap_long_lists`, so wrapping an argument list stays the
    last resort rather than the first thing tried."""
    out: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        pieces = _split_function_line(line, indent_size)
        out.extend(pieces if pieces else [line])
    joined = "\n".join(out)
    return joined if _same_shape(text, joined) else text
