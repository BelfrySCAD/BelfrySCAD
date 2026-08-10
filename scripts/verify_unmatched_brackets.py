#!/usr/bin/env python3
"""An opener the document never closes is shown in bright red.

Whether a bracket is unmatched is not a property of its own line -- the
closer can be far below -- so the interesting cases are the ones a
per-line highlighter cannot see: an opener closed pages later, and one
never closed at all.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QColor, QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

RED = "#ff2d2d"
failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def colour_at(doc, line, col):
    """The foreground the highlighter painted at one character, as #rrggbb."""
    block = doc.findBlockByNumber(line)
    assert block.isValid(), f"no line {line}"
    hit = None
    for r in block.layout().formats():
        if r.start <= col < r.start + r.length:
            hit = r  # later ranges win, as they do on screen
    if hit is None or not hit.format.foreground().color().isValid():
        return None
    return hit.format.foreground().color().name().lower()


def reds(doc):
    """Every (line, col) painted red, for asserting on the whole document."""
    out = []
    block = doc.firstBlock()
    while block.isValid():
        for r in block.layout().formats():
            if r.format.foreground().color().name().lower() == RED:
                for c in range(r.start, r.start + r.length):
                    out.append((block.blockNumber(), c))
        block = block.next()
    return sorted(out)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.editor import OpenSCADHighlighter

    # A real widget, not a bare QTextDocument: an unattached document is
    # never laid out, so Qt's own highlight pass never runs and the only
    # formats that appear are the ones this feature repaints by hand --
    # which would pass the tests for the wrong reason and hide whether the
    # ordinary path works at all. Measured: 0 format ranges bare, 4 attached.
    view = QPlainTextEdit()
    hl = OpenSCADHighlighter(view.document())
    view.show()
    app.processEvents()
    doc = view.document()

    def load(src):
        view.setPlainText(src)
        app.processEvents()
        return doc

    # --- the palette must not collide with the error colour --------------
    import colorsys

    def hue(name):
        c = QColor(name)
        return colorsys.rgb_to_hsv(c.redF(), c.greenF(), c.blueF())[0] * 360

    red_hue = hue(RED)
    for fmt in hl._bracket_formats:
        name = fmt.foreground().color().name()
        d = abs(hue(name) - red_hue) % 360
        d = min(d, 360 - d)
        check(f"depth colour {name} is clear of red in hue ({d:.0f} deg)", d >= 40,
              f"{d:.0f} deg")

    # --- the plain cases -------------------------------------------------
    load("cube(10);")
    check("a matched pair is not red", reds(doc) == [], str(reds(doc)))
    check("and keeps its depth colour", colour_at(doc, 0, 4) == "#c4921c",
          str(colour_at(doc, 0, 4)))

    load("cube(10;")
    check("an unclosed ( is red", reds(doc) == [(0, 4)], str(reds(doc)))

    load("x = [1, 2;")
    check("an unclosed [ is red", reds(doc) == [(0, 4)], str(reds(doc)))

    load("module m() {")
    check("an unclosed { is red", reds(doc) == [(0, 11)], str(reds(doc)))
    check("the ( ) above it, being closed, is not",
          colour_at(doc, 0, 8) == "#c4921c", str(colour_at(doc, 0, 8)))

    # --- what a per-line highlighter cannot see --------------------------
    load("module m() {\n" + "    cube(1);\n" * 40 + "}\n")
    check("an opener closed 40 lines later is not red", reds(doc) == [], str(reds(doc))[:80])

    load("module m() {\n" + "    cube(1);\n" * 40)
    check("the same opener with the closer deleted is red",
          reds(doc) == [(0, 11)], str(reds(doc))[:80])

    # The red must land on the OUTER bracket, not the innermost one: both
    # are open at end of file, but only the unclosed one is the error.
    load("module m() {\n    cube(1);\n")
    check("only the never-closed bracket is red", reds(doc) == [(0, 11)], str(reds(doc)))

    # Three unclosed across two lines: the { on line 0, and both the ( and
    # the { on line 1.
    load("module m() {\n    if (x {\n")
    check("every unclosed opener is marked",
          reds(doc) == [(0, 11), (1, 7), (1, 10)], str(reds(doc)))

    # ...while a closed pair on a line that also has an unclosed one keeps
    # its depth colour.
    load("module m() {\n    if (x) {\n")
    check("the closed ( ) on that line is left alone",
          colour_at(doc, 1, 7) != RED and colour_at(doc, 1, 9) != RED,
          f"( is {colour_at(doc, 1, 7)}, ) is {colour_at(doc, 1, 9)}")
    check("but the two unclosed { are red",
          reds(doc) == [(0, 11), (1, 11)], str(reds(doc)))

    # --- brackets that are not brackets ----------------------------------
    load('// cube(\ncube(1);\n')
    check("an opener inside a line comment is not counted", reds(doc) == [], str(reds(doc)))

    load('/* (\n   ( */\ncube(1);\n')
    check("openers inside a block comment are not counted", reds(doc) == [], str(reds(doc)))

    load('s = "(";\ncube(1);\n')
    check("an opener inside a string is not counted", reds(doc) == [], str(reds(doc)))

    load('s = "(";\ncube(1;\n')
    check("a real unclosed one is still found past a string",
          reds(doc) == [(1, 4)], str(reds(doc)))

    # --- depth colouring must survive ------------------------------------
    load("f( [1, 2] ;")
    check("the unclosed ( is red", colour_at(doc, 0, 1) == RED, str(colour_at(doc, 0, 1)))
    check("the pair inside it still matches itself",
          colour_at(doc, 0, 3) == colour_at(doc, 0, 8) != RED,
          f"{colour_at(doc, 0, 3)} vs {colour_at(doc, 0, 8)}")
    check("and is coloured for depth 1, not depth 0 -- the red one still counts",
          colour_at(doc, 0, 3) == "#59cad7", str(colour_at(doc, 0, 3)))

    # --- stray closers ---------------------------------------------------
    load("cube(1));\ncube(2);\n")
    check("a stray closer does not crash or recolour the rest",
          reds(doc) == [] and colour_at(doc, 1, 4) == "#c4921c",
          f"reds={reds(doc)} next-line={colour_at(doc, 1, 4)}")

    # --- it updates as you edit ------------------------------------------
    load("module m() {\n    cube(1);\n")
    check("unclosed to begin with", reds(doc) == [(0, 11)], str(reds(doc)))

    cur = doc.rootFrame().lastCursorPosition()
    cur.insertText("}\n")
    check("typing the closer clears the red", reds(doc) == [], str(reds(doc)))

    for _ in range(2):  # the "}" and its newline
        cur.deletePreviousChar()
    check("deleting it again brings the red back", reds(doc) == [(0, 11)], str(reds(doc)))

    # Inserting a line ABOVE must move the mark with it -- this is what a
    # debounced scan would get wrong, leaving red on a stale line number.
    top = doc.findBlockByNumber(0).position()
    cur.setPosition(top)
    cur.insertText("// header\n")
    check("inserting a line above moves the red down with the bracket",
          reds(doc) == [(1, 11)], str(reds(doc)))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
