#!/usr/bin/env python3
"""Option-Up/Down moves the current line, or the selected lines, by one.

Driven through real key events rather than by calling _move_lines(), so
the binding itself is covered -- including that Option-Down on the last
line moves it rather than appending a blank line, which the editor's own
Key_Down handling would otherwise do.

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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []
ALT = Qt.KeyboardModifier.AltModifier


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.editor import CodeEditor

    ed = CodeEditor()
    ed.show()
    app.processEvents()
    doc = ed.document()

    def load(lines, first=None, last=None, col=0, end_col=None):
        """Set text, then put the cursor on `first` (selecting to `last`)."""
        ed.setPlainText("\n".join(lines))
        app.processEvents()
        if first is None:
            return
        c = QTextCursor(doc)
        c.setPosition(doc.findBlockByNumber(first).position() + col)
        if last is not None:
            target = doc.findBlockByNumber(last)
            c.setPosition(target.position() + (len(target.text()) if end_col is None
                                               else end_col),
                          QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(c)

    def lines():
        return ed.toPlainText().split("\n")

    def press(key):
        QTest.keyClick(ed, key, ALT)
        app.processEvents()

    UP, DOWN = Qt.Key.Key_Up, Qt.Key.Key_Down

    def selected_line_numbers():
        c = ed.textCursor()
        first, last = ed._selected_block_range(c)
        return list(range(first, last + 1))

    # --- the case from the request ---------------------------------------
    # 1,2,3,4,5 with lines 3 and 4 selected, Option-Up -> 1,3,4,2,5
    load(["1", "2", "3", "4", "5"], first=2, last=3)
    press(UP)
    check("selected lines 3-4 move above line 2",
          lines() == ["1", "3", "4", "2", "5"], str(lines()))
    check("and the selection follows them", selected_line_numbers() == [1, 2],
          str(selected_line_numbers()))
    check("still selecting the same text",
          ed.textCursor().selectedText().replace(" ", "\n") == "3\n4",
          repr(ed.textCursor().selectedText()))

    # --- a single line, no selection -------------------------------------
    load(["a", "b", "c"], first=1)
    press(UP)
    check("one line moves up", lines() == ["b", "a", "c"], str(lines()))
    press(DOWN)
    check("and back down again", lines() == ["a", "b", "c"], str(lines()))

    load(["a", "b", "c"], first=1, col=1)
    press(DOWN)
    check("a line moves down", lines() == ["a", "c", "b"], str(lines()))
    c = ed.textCursor()
    check("the cursor rides along, keeping its column",
          c.blockNumber() == 2 and c.positionInBlock() == 1,
          f"line {c.blockNumber()} col {c.positionInBlock()}")

    # --- the document edges ----------------------------------------------
    load(["a", "b", "c"], first=0)
    press(UP)
    check("the first line will not move up", lines() == ["a", "b", "c"], str(lines()))

    load(["a", "b", "c"], first=2)
    press(DOWN)
    check("the last line will not move down", lines() == ["a", "b", "c"], str(lines()))
    check("and no blank line is appended -- the editor's own Key_Down would",
          doc.blockCount() == 3, str(doc.blockCount()))

    load(["a", "b", "c"], first=2)
    press(UP)
    check("but the last line can move up", lines() == ["a", "c", "b"], str(lines()))

    load(["a", "b", "c"], first=1, last=2)
    press(DOWN)
    check("a selection already at the bottom does not move",
          lines() == ["a", "b", "c"], str(lines()))

    # --- a selection that stops at column 0 of the next line --------------
    # It highlights no text there, so that line must not travel with it.
    load(["1", "2", "3", "4"], first=1, last=2, end_col=0)
    press(UP)
    check("a selection ending at the next line's column 0 leaves it behind",
          lines() == ["2", "1", "3", "4"], str(lines()))
    check("and the selection still ends where it did",
          ed.textCursor().selectionEnd()
          == doc.findBlockByNumber(1).position(),
          str(ed.textCursor().selectionEnd()))

    # --- content is moved verbatim ---------------------------------------
    load(["module m() {", "    cube(1);", "    sphere(2);", "}"], first=1)
    press(DOWN)
    check("indentation is carried, not recomputed",
          lines() == ["module m() {", "    sphere(2);", "    cube(1);", "}"],
          str(lines()))
    press(UP)
    check("and the opposite move puts it back exactly",
          lines() == ["module m() {", "    cube(1);", "    sphere(2);", "}"],
          str(lines()))

    load(["a", "", "c"], first=0)
    press(DOWN)
    check("an empty line is a line like any other",
          lines() == ["", "a", "c"], str(lines()))

    # --- the block count must never drift --------------------------------
    load(["1", "2", "3", "4", "5"], first=2)
    for _ in range(6):
        press(DOWN)
    check("pressed past the bottom, the line ends up last and stops",
          lines() == ["1", "2", "4", "5", "3"], str(lines()))
    for _ in range(6):
        press(UP)
    check("and pressed past the top it ends up first",
          lines() == ["3", "1", "2", "4", "5"], str(lines()))
    check("with no line added or dropped along the way",
          doc.blockCount() == 5, str(doc.blockCount()))

    # --- other Alt combinations are left alone ---------------------------
    load(["a", "b", "c"], first=1)
    QTest.keyClick(ed, Qt.Key.Key_Up, ALT | Qt.KeyboardModifier.ShiftModifier)
    app.processEvents()
    check("Shift-Option-Up does not move lines", lines() == ["a", "b", "c"], str(lines()))

    load(["a", "b", "c"], first=1)
    QTest.keyClick(ed, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    app.processEvents()
    check("a plain Up does not move lines", lines() == ["a", "b", "c"], str(lines()))

    # --- one undo step ----------------------------------------------------
    # A move must be a single contentsChanged, not a delete plus an insert:
    # the app builds its undo stack from those.
    load(["1", "2", "3"], first=1)
    seen = []
    doc.contentsChanged.connect(lambda: seen.append(1))
    press(UP)
    check("a move is one document change", len(seen) == 1, f"{len(seen)} changes")

    # ...and the property that actually matters, through the app's own undo
    # stack rather than inferred from the change count.
    from belfryscad.window.main_window import MainWindow
    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()
    app.processEvents()

    stack = w._undo_stack
    tab_ed = w._current_tab().editor
    tab_ed.setPlainText("1\n2\n3\n4")
    app.processEvents()

    def age_out_last_edit():
        """Push the top command's timestamp back past _TextEditCmd's merge
        window, exactly as waiting would. The app folds edits made within
        three seconds of each other into one undo step -- a real user
        pauses, a script does not, and sleeping three seconds per case
        would only make the same point slower."""
        top = stack.command(stack.count() - 1)
        top._t -= 10.0

    age_out_last_edit()
    before = tab_ed.toPlainText()
    depth = stack.count()

    c = QTextCursor(tab_ed.document())
    c.setPosition(tab_ed.document().findBlockByNumber(2).position())
    tab_ed.setTextCursor(c)
    QTest.keyClick(tab_ed, UP, ALT)
    app.processEvents()
    check("the move took effect in a real window",
          tab_ed.toPlainText() == "1\n3\n2\n4", repr(tab_ed.toPlainText()))
    check("and added exactly one undo step",
          stack.count() == depth + 1, f"{stack.count()} vs {depth + 1}")

    w._act_undo.trigger()
    app.processEvents()
    check("one undo puts every line back",
          tab_ed.toPlainText() == before, repr(tab_ed.toPlainText()))

    w._act_redo.trigger()
    app.processEvents()
    check("and redo moves it again",
          tab_ed.toPlainText() == "1\n3\n2\n4", repr(tab_ed.toPlainText()))

    # Two moves in a row DO merge, like consecutive typing: that is the
    # app's rule for text edits, and a line move is one. Pinned so it reads
    # as a decision rather than an accident.
    age_out_last_edit()
    depth = stack.count()
    QTest.keyClick(tab_ed, UP, ALT)
    app.processEvents()
    QTest.keyClick(tab_ed, UP, ALT)
    app.processEvents()
    check("two moves in quick succession are one undo step, as typing is",
          stack.count() == depth + 1, f"{stack.count()} vs {depth + 1}")
    w.close()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
