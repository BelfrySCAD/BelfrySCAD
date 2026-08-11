#!/usr/bin/env python3
"""An indent guide covers its own line and no further.

The caret used to drag guides along with it: _draw_vline_avoiding_cursor
notched the caret out of any guide sharing its *column*, without checking
the caret was on that line at all, so a guide high in the file drew
straight down to a caret dozens of lines below it. Reported as "the
innermost indent line is extending down to the bottom of the window".

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtGui import QFontMetricsF, QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

GUIDE = (224, 224, 224)          # _IndentGuides' pen, #E0E0E0
failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


SRC = """\
module outer() {
    intersection() {
        union() {
            difference() {
                square(10, center=true);
                circle(d=4);
            }
        }
    }
    circle(d=2);
}

// A long unindented tail, with the caret parked in it at the same
// column as the deepest guide above -- the exact shape of the bug.
outer();
x = 1;          // padded out past the deepest guide column above,
y = 2;          // so the caret can actually be parked in that column
z = 3;
w = 4;
v = 5;
u = 6;
t = 7;          // <- the caret goes here
"""


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.editor import CodeEditor

    ed = CodeEditor()
    ed.resize(700, 620)
    ed.setPlainText(SRC)
    ed.show()

    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    lines = SRC.split("\n")
    indent = ed._indent_size
    fm = QFontMetricsF(ed.font())
    home = QTextCursor(ed.document())
    home.movePosition(QTextCursor.MoveOperation.Start)
    x0 = ed.cursorRect(home).x()
    char_w = fm.horizontalAdvance("0")

    # Park the caret on the last line, in the deepest guide's column. That
    # column is 12 here (three levels in), and it is where the bug fired.
    deep_col = max(
        (len(t) - len(t.lstrip(" "))) // indent * indent
        for t in lines if t.strip()) - indent
    cur = QTextCursor(ed.document())
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.movePosition(QTextCursor.MoveOperation.StartOfLine)
    cur.movePosition(QTextCursor.MoveOperation.Up)          # last line with text
    cur.movePosition(QTextCursor.MoveOperation.Right,
                     QTextCursor.MoveMode.MoveAnchor, deep_col)
    ed.setTextCursor(cur)
    for _ in range(30):
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    caret_line = cur.blockNumber() + 1
    check(f"the caret is parked on line {caret_line} at column {deep_col}",
          cur.positionInBlock() == deep_col and lines[caret_line - 1].strip() != "")

    # --- what the screen actually shows ---------------------------------
    img = ed.viewport().grab().toImage()
    seen = {}
    for x in range(img.width()):
        ys = [y for y in range(img.height())
              if (lambda c: (c.red(), c.green(), c.blue()))(img.pixelColor(x, y)) == GUIDE]
        if len(ys) > 8:
            seen[x] = (min(ys), max(ys))

    # --- what it should show --------------------------------------------
    # Every column strictly inside a line's indentation, and nothing else.
    def y_of(line_no):
        blk = ed.document().findBlockByNumber(line_no - 1)
        g = ed.blockBoundingGeometry(blk).translated(ed.contentOffset())
        return g.top(), g.bottom()

    for col in range(indent, deep_col + 1, indent):
        owners = [i + 1 for i, t in enumerate(lines)
                  if t.strip() and (len(t) - len(t.lstrip(" "))) > col]
        x = round(x0 + col * char_w)
        check(f"column {col} has a guide at x={x}", x in seen, str(sorted(seen)))
        if x not in seen or not owners:
            continue
        top, _ = y_of(owners[0])
        _, bot = y_of(owners[-1])
        lo, hi = seen[x]
        check(f"the column {col} guide starts on line {owners[0]}",
              abs(lo - top) <= 3, f"drawn from y={lo}, line starts at y={top:.0f}")
        check(f"the column {col} guide stops on line {owners[-1]}, "
              f"not at the caret on line {caret_line}",
              abs(hi - bot) <= 3,
              f"drawn to y={hi}, line {owners[-1]} ends at y={bot:.0f} "
              f"(line {caret_line} is at y={y_of(caret_line)[0]:.0f})")

    # No guide anywhere in the unindented tail.
    first_tail = next(i + 1 for i, t in enumerate(lines)
                      if t.strip() and i + 1 > 11)
    tail_top, _ = y_of(first_tail)
    strays = {x: v for x, v in seen.items() if v[1] > tail_top + 3}
    check("nothing is drawn below the last indented line",
          not strays, f"guide pixels at {strays}")

    ed.close()

    # --- the helper itself, line by line ---------------------------------
    # It only ever calls drawLine, so a recorder stands in for the painter
    # and the three cases can be stated exactly.
    from belfryscad.window.editor import _draw_vline_avoiding_cursor
    from PySide6.QtCore import QRect

    class Recorder:
        def __init__(self): self.lines = []
        def drawLine(self, x1, y1, x2, y2): self.lines.append((x1, y1, x2, y2))

    def drawn(caret):
        r = Recorder()
        _draw_vline_avoiding_cursor(r, 100, 200, 260, caret)
        return r.lines

    off_column = QRect(40, 205, 1, 14)
    check("a caret in another column leaves the segment whole",
          drawn(off_column) == [(100, 200, 100, 260)], str(drawn(off_column)))

    far_below = QRect(100, 900, 1, 14)
    check("a caret far below in the same column leaves the segment whole",
          drawn(far_below) == [(100, 200, 100, 260)], str(drawn(far_below)))

    far_above = QRect(100, 20, 1, 14)
    check("a caret far above in the same column leaves the segment whole",
          drawn(far_above) == [(100, 200, 100, 260)], str(drawn(far_above)))

    on_it = QRect(100, 220, 1, 14)
    got = drawn(on_it)
    check("a caret sitting on the segment is notched out of it",
          len(got) == 2 and got[0][3] <= 220 and got[1][1] >= 233, str(got))
    check("and the notched pieces stay inside the segment",
          all(200 <= y1 <= 260 and 200 <= y2 <= 260 for _, y1, _, y2 in got),
          str(got))

    at_top = QRect(100, 195, 1, 14)
    check("a caret overlapping the top edge only trims the top",
          drawn(at_top) == [(100, 208, 100, 260)], str(drawn(at_top)))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
