#!/usr/bin/env python3
"""Undo/redo never asks Qt for a negative cursor position.

`setPlainText()` replaces the whole document, and a `contentsChanged`
handler running across that replacement can read a cursor still attached to
the old one -- a null cursor, whose `position()` is -1. That -1 was stored
in the undo command and handed back to `QTextCursor.setPosition` on the
matching redo, which Qt refused with

    QTextCursor::setPosition: Position '-1' out of range

leaving the cursor wherever it happened to be. It also reached
`_TextEditCmd.mergeWith`, which compares stored positions to decide whether
a burst of edits is one undo step.

Qt widgets abort pytest in this project, so this runs standalone.
"""
import functools
import sys
import tempfile
import time
from pathlib import Path

print = functools.partial(print, flush=True)   # os._exit skips the flush

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QEventLoop, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []
messages = []


def handler(mode, context, message):
    messages.append(message)


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    qInstallMessageHandler(handler)
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.settings import use_scratch_settings
    use_scratch_settings(tempfile.mkdtemp(prefix="undo-clamp-"), seed=False)
    from belfryscad.window.main_window import MainWindow, _cursor_position

    win = MainWindow()
    win.show()

    def pump(seconds=0.25):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    pump(1.0)
    tab = win._current_tab()

    # The capture helper is the fix's real site; a detached cursor is what
    # it exists to absorb.
    from PySide6.QtGui import QTextCursor
    check("a null cursor still reports -1 to Qt", QTextCursor().position() == -1)

    class _Detached:
        def textCursor(self):
            return QTextCursor()

    check("the helper never returns a negative position",
          _cursor_position(_Detached()) == 0)
    check("the helper passes a real position through",
          _cursor_position(tab.editor) >= 0)

    # The path that produced it: a wholesale setPlainText, then redo.
    messages.clear()
    tab.editor.setPlainText("cube(1);")
    pump(0.3)
    for _ in range(6):
        win._act_undo.trigger()
        pump(0.1)
    win._act_redo.trigger()
    pump(0.3)

    bad = [m for m in messages if "out of range" in m]
    check("no out-of-range warning from undo/redo", not bad, "; ".join(bad[:3]))
    check("the text came back", tab.editor.toPlainText() == "cube(1);",
          repr(tab.editor.toPlainText()))
    check("the cursor is somewhere valid",
          0 <= tab.editor.textCursor().position() <= len("cube(1);"),
          str(tab.editor.textCursor().position()))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    import os
    os._exit(1 if failures else 0)


main()
