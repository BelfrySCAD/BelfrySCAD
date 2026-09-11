"""Blank out `/* ... */` block comments before the docs parser sees them.

The parser recognises a documentation comment by the `//` that starts its
line and knows nothing about block comments, so commenting a chunk of a
library out with `/* ... */` left every `// Function:` inside it still
documented -- and, worse, still validated and still rendering example
images (BelfrySCAD #415). Upstream openscad_docsgen has the same gap; this
is a deliberate divergence from the otherwise-vendored parser.

Blanking rather than deleting: every line stays, so line numbers in error
messages still point at the source, and `//` inside a block comment stops
being a `//` at all.
"""

__all__ = ["strip_block_comments"]


def strip_block_comments(lines: list[str]) -> list[str]:
    """`lines` with the contents of every block comment replaced by spaces.

    Follows the three things that decide whether a `/*` really opens one:
    it is inert inside a string literal and inside a `//` comment, and the
    first `*/` closes it (OpenSCAD's block comments do not nest, C-style).
    String state carries across lines because a string literal may contain
    a raw newline.
    """
    out: list[str] = []
    in_block = False
    in_string = False
    for line in lines:
        res: list[str] = []
        i = 0
        while i < len(line):
            c = line[i]
            nxt = line[i + 1:i + 2]
            if in_block:
                if c == "*" and nxt == "/":
                    in_block = False
                    res.append("  ")
                    i += 2
                    continue
                # Keep the newline so the line count cannot drift.
                res.append("\n" if c == "\n" else " ")
                i += 1
            elif in_string:
                res.append(c)
                if c == "\\" and nxt:
                    res.append(nxt)     # an escaped quote does not end it
                    i += 2
                    continue
                if c == '"':
                    in_string = False
                i += 1
            elif c == '"':
                in_string = True
                res.append(c)
                i += 1
            elif c == "/" and nxt == "/":
                # The rest of the line is a line comment, quotes and all --
                # taking it whole is what keeps a `"` in prose from being
                # read as the start of a string.
                res.append(line[i:])
                break
            elif c == "/" and nxt == "*":
                in_block = True
                res.append("  ")
                i += 2
            else:
                res.append(c)
                i += 1
        out.append("".join(res))
    return out
