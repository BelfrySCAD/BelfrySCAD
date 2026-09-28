"""Alt/Option + mouse wheel steps the number at the text cursor.

OpenSCAD's "number scroll" (ScintillaEditor::modifyNumber), ported: the digit
just LEFT of the cursor is the one that moves, so the step is 1 in the units
place, 0.1 in the tenths, 10 in the tens. Decimals, leading zeros and the sign
are kept, and the cursor stays after the same digit.

One departure: a leading sign belongs to the number only when it is unary.
OpenSCAD takes `2-3` as `2` and `-3`, so stepping it up gives `2-2`.

Qt-free, so it tests without a widget.
"""
import re

_NUM_BEFORE = re.compile(r"[-+]?\d*\.?\d*$")
_NOT_NUM = re.compile(r"[^0-9.]")
_NUMBER = re.compile(r"[-+]?\d*\.?\d+")


def step_number(line: str, col: int, delta: int):
    """`line` with the number whose digit sits just left of `col` moved by
    `delta` steps of that digit's place, and the new cursor column -- or
    None when there is no number there to step."""
    begin = _NUM_BEFORE.search(line[:col]).start()
    if line[begin:begin + 1] in "+-" and begin < col:
        # Binary, not a sign: the operand before it owns the character.
        prev = line[:begin].rstrip()
        if prev and (prev[-1].isalnum() or prev[-1] in "_)]"):
            begin += 1
    if begin > 0 and (line[begin - 1].isalpha() or line[begin - 1] == "_"):
        return None                      # part of a name, like x2
    m = _NOT_NUM.search(line, col)
    end = m.start() if m else len(line)
    nr = line[begin:end]
    if not _NUMBER.fullmatch(nr):
        return None
    signed = nr[0] in "+-"
    curpos = col - begin
    if curpos == 0 or (curpos == 1 and signed):
        return None                      # no digit to the cursor's left
    dot = nr.find(".")
    decimals = 0 if dot < 0 else len(nr) - dot - 1
    number = int(nr.replace(".", ""))
    tail = len(nr) - curpos
    exponent = tail - (1 if dot >= curpos else 0)
    number += delta * 10 ** exponent

    negative = number < 0
    digits = str(abs(number))
    if decimals:
        digits = digits.rjust(decimals + 1, "0")
        digits = digits[:-decimals] + "." + digits[-decimals:]
    digits = digits.rjust(tail, "0")
    new = ("-" if negative else "+" if nr[0] == "+" else "") + digits
    return line[:begin] + new + line[end:], begin + len(new) - tail
