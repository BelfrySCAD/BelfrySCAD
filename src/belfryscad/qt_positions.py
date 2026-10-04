"""Converting between Qt text positions and Python string indices.

Qt counts positions in UTF-16 code units; Python indexes strings by code
point. They agree until a character outside the Basic Multilingual Plane --
an emoji, say -- which is one Python character but two Qt units. Every Qt
position used to index a Python string (`text[cursor.position()]`), and
every Python index handed to Qt (`setPosition(match.start())`), is off by
one per such character before it: bracket matching raised IndexError with
the cursor after an emoji, and anything placed by offset landed short.

Both directions have a fast path: a string with no such character -- nearly
every OpenSCAD file -- converts as the identity, at the cost of one regex
scan.
"""
import functools
import re

_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")


def to_index(text: str, pos: int) -> int:
    """The Python index in `text` of Qt position `pos`.

    A position inside a surrogate pair (between an emoji's two units) maps
    to the character after it. Positions past the end clamp to len(text)."""
    if not _ASTRAL.search(text):
        return min(pos, len(text))
    units = 0
    for i, ch in enumerate(text):
        if units >= pos:
            return i
        units += 2 if ch >= "\U00010000" else 1
    return len(text)


def to_qt(text: str, index: int) -> int:
    """The Qt position of Python index `index` in `text`."""
    return index + len(_ASTRAL.findall(text, 0, max(0, index)))


def qt_length(text: str) -> int:
    """`text`'s length in Qt units."""
    return to_qt(text, len(text))


def byte_column_to_qt(line: str, byte_column: int) -> int:
    """The Qt offset within `line` of a UTF-8 BYTE offset into it -- what
    the parser reports as a column. An accented letter is two bytes and one
    Qt unit, an emoji four bytes and two, so a byte column taken as a Qt
    offset lands to the right of the error after either."""
    return qt_length(line.encode("utf-8")[:max(0, byte_column)].decode("utf-8", "ignore"))


@functools.lru_cache(maxsize=8)
def byte_to_index(text: str):
    """A callable mapping a UTF-8 byte offset into `text` -- the unit of
    every span the evaluator reports -- to the Python index of the same
    place; None when the two coincide (ASCII text). Without it a single
    em dash in a comment shifted every span after it by two."""
    if text.isascii():
        return None
    starts = []                          # byte offset where each character starts
    pos = 0
    for ch in text:
        starts.append(pos)
        pos += len(ch.encode("utf-8"))
    starts.append(pos)
    from bisect import bisect_right
    return lambda offset: bisect_right(starts, offset) - 1


def parse_ast_string(text: str, include_comments: bool = False) -> list:
    """openscad_cpp_evaluator.parse_ast_string, with every node's
    start_offset/end_offset turned from UTF-8 bytes into Python indices
    into `text`, which is how every caller slices with them."""
    from openscad_cpp_evaluator import parse_ast_string as parse
    nodes = parse(text, include_comments)
    convert = byte_to_index(text)
    if convert is not None:
        def walk(node):
            if isinstance(node, dict):
                for key in ("start_offset", "end_offset"):
                    if isinstance(node.get(key), int):
                        node[key] = convert(node[key])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(nodes)
    return nodes
