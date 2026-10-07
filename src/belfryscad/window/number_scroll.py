"""Alt/Option + mouse wheel steps the number at the text cursor.

The digit just left of the cursor sets the step (its place value); the
cursor keeps its distance from the end of the number. Purely textual -- a
number in a comment or string steps too. Qt-free, so it tests without a
widget.
"""
import re
from decimal import Decimal, localcontext

_DIGITS = "0123456789"
_NUMBER = re.compile(r"[+-]?[0-9]*\.?[0-9]+")


def _is_name_char(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def step_number(line: str, col: int, delta: int):
    """`line` with the number whose digit sits just left of `col` moved by
    `delta` steps of that digit's place, and the new cursor column -- or
    None when there is no number there to step."""
    # Left part: digits/dots back from the cursor, at most one dot.
    body = col
    seen_dot = False
    while body > 0 and line[body - 1] in _DIGITS + ".":
        if line[body - 1] == ".":
            if seen_dot:
                break
            seen_dot = True
        body -= 1
    if body == col:
        return None          # nothing of the number left of the cursor

    # A sign is the number's only when unary: binary after an operand.
    start = body
    if body > 0 and line[body - 1] in "+-":
        j = body - 2
        while j >= 0 and line[j].isspace():
            j -= 1
        if not (j >= 0 and (line[j].isalnum() or line[j] in "_)]")):
            start = body - 1

    if start > 0 and _is_name_char(line[start - 1]):
        return None          # part of a name (x2) or an exponent (1e5)

    end = col
    while end < len(line) and line[end] in _DIGITS + ".":
        end += 1

    token = line[start:end]
    if not _NUMBER.fullmatch(token):
        return None

    decimals = len(token.partition(".")[2])
    right = line[col:end]                    # t = len(right)
    digits_right = len(right.replace(".", ""))
    with localcontext() as ctx:
        ctx.prec = len(token) + len(str(delta)) + 10   # exact, never rounds
        new = Decimal(token) + delta * Decimal(1).scaleb(digits_right - decimals)
        mag = f"{abs(new):.{decimals}f}"
    # t + 1 keeps a digit left of the cursor (e.g. 1|00 - 1 -> 0|00).
    mag = mag.rjust(len(right) + 1, "0")
    sign = "-" if new < 0 else ("+" if token[0] == "+" else "")
    rep = sign + mag
    return line[:start] + rep + line[end:], start + len(rep) - len(right)
