"""Alt/Option + mouse wheel steps the number at the text cursor.

Qt-free, so it tests without a widget.
"""


def step_number(line: str, col: int, delta: int):
    """`line` with the number whose digit sits just left of `col` moved by
    `delta` steps of that digit's place, and the new cursor column -- or
    None when there is no number there to step."""
    # CLEAN-ROOM: reimplement from spec section 1
    raise NotImplementedError("CLEAN-ROOM: reimplement from spec section 1")
