"""Ctrl+Up/Down scrolls the editor a line, keeping the caret on screen (#951).

Real key presses into a shown CodeEditor, in a subprocess (Qt widgets, offscreen).
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
CTRL = Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin" else Qt.KeyboardModifier.ControlModifier
e = CodeEditor(); e.resize(400, 200); e.show()
e.setPlainText("\n".join(f"x{i} = {i};" for i in range(200)))
bar = e.verticalScrollBar()
def state():
    return [bar.value(), e.textCursor().blockNumber()]
def put(top, line):
    bar.setValue(top)
    c = e.textCursor(); c.setPosition(e.document().findBlockByNumber(line).position()); e.setTextCursor(c)
    bar.setValue(top); app.processEvents()
def press(key, mods=CTRL):
    QTest.keyClick(e, key, mods); app.processEvents()
out = {}
put(50, 55); press(Qt.Key.Key_Down); out["down_mid"] = state()
put(50, 55); press(Qt.Key.Key_Up); out["up_mid"] = state()
put(50, 50); press(Qt.Key.Key_Down); out["down_caret_on_top_line"] = state()
put(0, 0); press(Qt.Key.Key_Up); out["up_at_top"] = state()
put(50, 55); press(Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier); out["plain_down"] = state()
last = bar.value()
put(50, 50 + (e.viewport().height() // e.fontMetrics().lineSpacing()) - 1)
out["visible_lines_bottom"] = e.textCursor().blockNumber()
press(Qt.Key.Key_Up); out["up_caret_on_bottom_line"] = state()
print(json.dumps(out)); sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_ctrl_arrows_scroll_a_line_and_keep_the_caret_in_view():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["down_mid"] == [51, 55]          # scrolled; caret stays put
    assert out["up_mid"] == [49, 55]
    assert out["down_caret_on_top_line"] == [51, 51]   # pushed along
    assert out["up_at_top"] == [0, 0]
    assert out["plain_down"] == [50, 56]        # a plain arrow is untouched
    bottom = out["visible_lines_bottom"]
    assert out["up_caret_on_bottom_line"] == [49, bottom - 1]
