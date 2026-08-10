#!/usr/bin/env python3
"""A string continued with a backslash keeps going on the next line.

The editor scanned each line on its own, so the second line of a
continued string was read as code: its brackets counted towards the depth
colours and could be flagged as unmatched, in bright red, inside what is
actually text.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

RED = "#ff2d2d"
STRING = "#ce9178"
failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.editor import OpenSCADHighlighter, _scan_line

    view = QPlainTextEdit()
    hl = OpenSCADHighlighter(view.document())  # noqa: F841
    view.show()
    app.processEvents()
    doc = view.document()

    def painted(src, colour):
        view.setPlainText(src)
        app.processEvents()
        out = []
        block = doc.firstBlock()
        while block.isValid():
            for r in block.layout().formats():
                if r.format.foreground().color().name().lower() == colour:
                    out += [(block.blockNumber(), c)
                            for c in range(r.start, r.start + r.length)]
            block = block.next()
        return sorted(out)

    def lines_of(hits):
        return sorted({line for line, _ in hits})

    # --- the scanner itself ----------------------------------------------
    _, _, brackets, _, in_string = _scan_line('s = "a \\', False, False)
    check("a line ending in a backslash inside a string stays open", in_string)
    check("and no bracket is reported from inside it", brackets == [], str(brackets))

    _, _, brackets, _, in_string = _scan_line('( ( ( b";', False, True)
    check("the next line resumes inside the string", not in_string)
    check("its brackets are text, not nesting", brackets == [], str(brackets))

    _, _, _, _, in_string = _scan_line('s = "unclosed;', False, False)
    check("a plain unclosed quote does not continue", not in_string)

    _, _, brackets, _, in_string = _scan_line(r's = "a\\";  (', False, False)
    check("a string ending in an escaped backslash is closed, not continued",
          not in_string)
    check("so the ( after it is code", [c for _, c in brackets] == ["("], str(brackets))

    # --- through the highlighter -----------------------------------------
    cont = 's = "a \\\n( ( ( b";\ncube(1);\n'
    check("openers on a continued line are not flagged unmatched",
          painted(cont, RED) == [], str(painted(cont, RED)))
    check("and that line is coloured as string",
          1 in lines_of(painted(cont, STRING)), str(lines_of(painted(cont, STRING))))

    same = 's = "a ( ( ( b";\ncube(1);\n'
    check("the same string on one line behaves identically",
          painted(same, RED) == [], str(painted(same, RED)))

    # The depth after a continued string must be what it was before it.
    after = 's = "x \\\n( ( y";\n{ cube(1); }\n'
    check("brackets after a continued string are still counted from zero",
          painted(after, RED) == [], str(painted(after, RED)))

    # --- a stray quote must not swallow the file --------------------------
    stray = 's = "oops;\ncube(1);\nsphere(2);\n'
    check("an unclosed quote stops colouring at its own line",
          lines_of(painted(stray, STRING)) == [0],
          str(lines_of(painted(stray, STRING))))
    check("so the lines below it are still read as code",
          painted(stray, RED) == [], str(painted(stray, RED)))

    # --- escaped quotes, which the old regex got wrong --------------------
    esc = r'a = "say \"hi\" now"; (' + "\n"
    hits = painted(esc, STRING)
    want = esc.index(";") - esc.index('"')   # from the opening quote to the closing one
    check("an escaped quote does not end the string early",
          lines_of(hits) == [0] and len(hits) == want,
          f"{len(hits)} chars coloured, expected {want}")
    check("and the ( after it is still code, so it reads as unmatched",
          painted(esc, RED) == [(0, esc.index("(", 5))],
          str(painted(esc, RED)))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
