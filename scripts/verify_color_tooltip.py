#!/usr/bin/env python3
"""Hovering a colour literal in the editor shows a swatch tooltip.

The matching is covered by tests/test_color_literals.py; what needs a real
widget is the wiring -- that the tooltip event reaches the VIEWPORT (an
`event()` override never sees one), that the hovered pixel maps back to the
right character, and that Qt's rich text really paints the swatch.

`QToolTip.showText` is intercepted rather than read back through
`QToolTip.text()`: Qt hides a tooltip whose widget is not under the actual
mouse pointer, so reading it back passes or fails on where the pointer
happens to be sitting -- which is what made the first version of this
script pass once and then fail unchanged. What the editor asked to show is
the part that is ours. Whether Qt can paint it is settled separately, by
rendering that same HTML and sampling the swatch's pixel.

Qt widgets abort pytest in this project, so this runs standalone.
"""
import functools
import sys
import time
from pathlib import Path

print = functools.partial(print, flush=True)   # os._exit skips the flush

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QEvent, QEventLoop, QPoint  # noqa: E402
from PySide6.QtGui import (QColor, QHelpEvent, QImage, QPainter,  # noqa: E402
                           QTextCursor, QTextDocument)
from PySide6.QtWidgets import QApplication, QToolTip  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


SRC = '''color("SteelBlue") cube(10);
color([1, 0, 0, 0.5]) sphere(4);
thecolor = [0, 0.5, 1];
stroke(path, c="gold");
translate([0.5, 0, 0.2]) cube(1);
p1 = [0.5, 0.75, 1.0];
color("nope") cube(1);
'''


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.editor import CodeEditor

    shown = []
    real_show = QToolTip.showText

    def spy(pos, text, widget=None, *a, **kw):
        shown.append(text)
        return real_show(pos, text, widget, *a, **kw)

    QToolTip.showText = spy

    ed = CodeEditor()
    ed.setPlainText(SRC)
    ed.resize(700, 300)
    ed.show()
    ed.raise_()

    def pump(seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    pump(1.0)

    def hover(needle, off=2):
        """Send a real tooltip event at the character `off` into `needle`."""
        shown.clear()
        c = QTextCursor(ed.document())
        c.setPosition(SRC.index(needle) + off)
        ed.setTextCursor(c)
        r = ed.cursorRect(c)
        p = QPoint(r.center().x(), r.center().y())
        QApplication.sendEvent(
            ed.viewport(),
            QHelpEvent(QEvent.Type.ToolTip, p, ed.viewport().mapToGlobal(p)))
        pump(0.3)
        return shown[-1] if shown else ""

    text = hover('"SteelBlue"')
    check("a colour name tooltips", bool(text), "nothing shown")
    check("the swatch carries the colour", "#4682b4" in text.lower(), text)
    check("the name is spelled out", "steelblue" in text.lower(), text)
    check("the 0-1 components are given", "0.275" in text, text)
    swatch_html = text

    text = hover('[1, 0, 0, 0.5]')
    check("a 4-vector in color() tooltips", "#ff0000" in text.lower(), text)
    check("alpha is shown", "0.5" in text, text)

    check("a colour-named assignment tooltips",
          "#0080ff" in hover('[0, 0.5, 1]').lower())
    check("a c= argument tooltips", "#ffd700" in hover('"gold"').lower())

    check("a translate vector does not tooltip", hover('[0.5, 0, 0.2]') == "")
    check("a point assignment does not tooltip", hover('[0.5, 0.75, 1.0]') == "")
    check("an unknown name does not tooltip", hover('"nope"') == "")

    # Qt's rich text has to actually paint the swatch: render the tooltip's
    # own HTML and sample the cell.
    doc = QTextDocument()
    doc.setHtml(swatch_html)
    img = QImage(int(doc.idealWidth()) + 8, 40, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    painter = QPainter(img)
    doc.drawContents(painter)
    painter.end()
    want = QColor("#4682b4").rgb()
    hit = any(img.pixel(x, y) == want
              for x in range(img.width()) for y in range(img.height()))
    check("the swatch paints as a block of the colour", hit,
          "no #4682b4 pixel in the rendered tooltip")

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    import os
    os._exit(1 if failures else 0)


main()
