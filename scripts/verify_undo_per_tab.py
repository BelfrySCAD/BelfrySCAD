#!/usr/bin/env python3
"""Undo is scoped to the tab you are looking at (#540).

One QUndoStack per FileTab, gathered in the window's QUndoGroup. Before
this, every tab's edits went onto one window-wide stack, so undoing past
the start of one tab's history carried on undoing another tab's text --
with nothing on screen to say it had happened.

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

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.settings import use_scratch_settings
    use_scratch_settings(tempfile.mkdtemp(prefix="undo-tab-"), seed=False)
    from belfryscad.window.main_window import MainWindow

    win = MainWindow()
    win.show()

    def pump(seconds=0.25):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    pump(1.0)

    def type_into(tab, text):
        """An edit that lands as its own undo step, like a typing burst."""
        tab.editor.setPlainText(text)
        pump(0.2)

    # Two tabs, each with its own text.
    tab1 = win._current_tab()
    type_into(tab1, "cube(1);")
    win._new_document()
    pump(0.3)
    tab2 = win._current_tab()
    check("a new tab is a different tab", tab2 is not tab1)
    type_into(tab2, "sphere(2);")

    check("each tab has its own stack", tab1.undo_stack is not tab2.undo_stack)
    check("both stacks are in the window's group",
          set(win._undo_group.stacks()) >= {tab1.undo_stack, tab2.undo_stack})
    check("the visible tab's stack is the active one",
          win._undo_group.activeStack() is tab2.undo_stack,
          str(win._undo_group.activeStack()))

    # Back to the first tab and undo past the end of its own history.
    win._tabs.setCurrentWidget(tab1)
    pump(0.3)
    check("switching tabs switches the active stack",
          win._undo_group.activeStack() is tab1.undo_stack)

    for _ in range(8):
        win._act_undo.trigger()
        pump(0.1)

    check("the current tab's text is undone", tab1.editor.toPlainText() == "",
          repr(tab1.editor.toPlainText()))
    check("THE OTHER TAB IS UNTOUCHED (#540)",
          tab2.editor.toPlainText() == "sphere(2);",
          repr(tab2.editor.toPlainText()))

    # Redo still works, and still only here.
    win._act_redo.trigger()
    pump(0.2)
    check("redo restores the current tab", tab1.editor.toPlainText() == "cube(1);",
          repr(tab1.editor.toPlainText()))
    check("redo left the other tab alone", tab2.editor.toPlainText() == "sphere(2);",
          repr(tab2.editor.toPlainText()))

    # The other tab's own history is intact after all that.
    win._tabs.setCurrentWidget(tab2)
    pump(0.3)
    win._act_undo.trigger()
    pump(0.2)
    check("the other tab kept its own history",
          tab2.editor.toPlainText() == "" and tab1.editor.toPlainText() == "cube(1);",
          f"{tab2.editor.toPlainText()!r} {tab1.editor.toPlainText()!r}")

    # Closing a tab takes its stack out of the group.
    stack2 = tab2.undo_stack
    win._close_tab(win._tabs.indexOf(tab2))
    pump(0.4)
    check("a closed tab's stack leaves the group",
          stack2 not in win._undo_group.stacks())
    check("undo still works after a close",
          win._undo_group.activeStack() is not None,
          "no active stack")

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    import os
    os._exit(1 if failures else 0)


main()
