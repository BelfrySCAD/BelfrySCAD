"""#433: Tab typed at the end of a line indented the line instead.

`Key_Tab` called `_indent_lines()` unconditionally, and that works on whole
blocks whether or not anything is selected -- so pressing Tab after `x=0;`
moved the statement right rather than inserting anything at the cursor.
Indenting lines is what a SELECTION means.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtCore import Qt, QEvent
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor

TAB = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier, "\\t")
BACKTAB = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier, "")
out = {}

def editor(text, pos=None, sel=None):
    ed = CodeEditor()
    ed.setPlainText(text)
    c = ed.textCursor()
    if sel:
        c.setPosition(sel[0]); c.setPosition(sel[1], QTextCursor.MoveMode.KeepAnchor)
    else:
        c.setPosition(len(text) if pos is None else pos)
    ed.setTextCursor(c)
    return ed

# The report: cursor at the end of "x=0;", press Tab.
ed = editor("x=0;")
ed.keyPressEvent(TAB)
out["end_of_line"] = ed.toPlainText()

# Column 0 of an empty line gets a full level.
ed = editor("", pos=0)
ed.keyPressEvent(TAB)
out["empty_line"] = ed.toPlainText()

# Off-grid: column 5 with size 4 goes to 8, so three spaces, not four.
ed = editor("abcde")
ed.keyPressEvent(TAB)
out["off_grid"] = ed.toPlainText()

# Mid-line: inserts at the cursor, does not move the line.
ed = editor("ab|cd".replace("|", ""), pos=2)
ed.keyPressEvent(TAB)
out["mid_line"] = ed.toPlainText()

# A selection still indents whole lines. 0..5 covers lines 1-2 only: a
# selection ending at a block's end does not reach into the next one.
ed = editor("a;\\nb;\\nc;", sel=(0, 5))
ed.keyPressEvent(TAB)
out["selection"] = ed.toPlainText()

# Shift+Tab still unindents, selection or not.
ed = editor("    x=0;", pos=8)
ed.keyPressEvent(BACKTAB)
out["backtab"] = ed.toPlainText()
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_tab_types_unless_lines_are_selected():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # The reported case: four spaces AFTER the statement, not before it.
    assert out["end_of_line"] == "x=0;    ", repr(out["end_of_line"])
    assert out["empty_line"] == "    "
    # Column 5 -> next stop is 8, so three spaces. Not a blind four.
    assert out["off_grid"] == "abcde   ", repr(out["off_grid"])
    assert out["mid_line"] == "ab  cd", repr(out["mid_line"])
    # Selected lines are still indented as a block.
    assert out["selection"] == "    a;\n    b;\nc;", repr(out["selection"])
    assert out["backtab"] == "x=0;"
