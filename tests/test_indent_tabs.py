"""Indent with tabs, Preferences ▸ Editor (#840), and tab-indented lines in either mode.

Real key presses into a CodeEditor, in a subprocess (Qt widgets, offscreen).
Each case is the text with `|` for the caret (`<`...`>` for a selection),
the keys to press, and the text and caret after.
"""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor
KEYS = {"BS": Qt.Key.Key_Backspace, "RET": Qt.Key.Key_Return, "TAB": Qt.Key.Key_Tab, "STAB": Qt.Key.Key_Backtab, "SRET": Qt.Key.Key_Return}
def run(on, start, keys):
    e = CodeEditor(); e.set_indent_with_tabs(on)
    a, b = (start.index("<"), start.index(">") - 1) if "<" in start else (start.index("|"),) * 2
    e.setPlainText(start.replace("|", "").replace("<", "").replace(">", ""))
    c = e.textCursor(); c.setPosition(a); c.setPosition(b, QTextCursor.MoveMode.KeepAnchor); e.setTextCursor(c)
    for k in keys:
        if k in KEYS:
            QTest.keyClick(e, KEYS[k], Qt.KeyboardModifier.ShiftModifier if k in ("STAB", "SRET") else Qt.KeyboardModifier.NoModifier)
        else:
            QTest.keyClicks(e, k)
    c = e.textCursor(); t = e.toPlainText()
    if c.hasSelection():
        return t[:c.selectionStart()] + "<" + t[c.selectionStart():c.selectionEnd()] + ">" + t[c.selectionEnd():]
    return t[:c.position()] + "|" + t[c.position():]
cases = json.loads(sys.argv[1])
print(json.dumps([run(*c[:3]) for c in cases])); sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''

CASES = [
    # Enter copies the indent as written, in either mode (it was re-typed as one space per tab)
    (False, "\tx;|", ["RET"], "\tx;\n\t|"),
    (True, "\tx;|", ["RET"], "\tx;\n\t|"),
    (False, "\tf(|", ["RET"], "\tf(\n\t    |"),
    (True, "\tf(|", ["RET"], "\tf(\n\t\t|"),
    # Shift+Enter is a plain Enter: Qt's own typed a U+2028 line separator,
    # which looks like a line break and is a syntax error to the parser (#949)
    (False, "  x;|", ["SRET"], "  x;\n  |"),
    # Tab types a real tab, mid-line too
    (True, "|x", ["TAB"], "\t|x"),
    (True, "x = 1;|", ["TAB"], "x = 1;\t|"),
    (False, "x = 1;|", ["TAB"], "x = 1;  |"),
    # Indent and Undent rewrite the line in the mode's characters, measured in columns
    (True, "<\tx;>", ["TAB"], "<\t\tx;>"),
    (True, "<      x;>", ["TAB"], "<\t\tx;>"),
    (False, "<\tx;>", ["TAB"], "<        x;>"),
    (True, "<\t\tx;>", ["STAB"], "<\tx;>"),
    (False, "<    x;>", ["TAB"], "<        x;>"),
    # Backspace takes one tab, never four at once
    (True, "\t\t\t\t|x", ["BS"], "\t\t\t|x"),
    (False, "        |x", ["BS"], "    |x"),
]


def test_indent_with_tabs():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(CASES)],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert [(c[1], c[2], g) for c, g in zip(CASES, got)] == [(c[1], c[2], c[3]) for c in CASES]
