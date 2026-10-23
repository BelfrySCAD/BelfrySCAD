"""Insert Template from the right-click menu opens its popup at the click and
inserts there, not at a caret the click may not have moved -- Windows leaves
the caret where it was (#911). Qt widgets: a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor, QContextMenuEvent
from PySide6.QtCore import QTimer, QEventLoop
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.editor import CodeEditor

lines = [f"x{i} = {i};" for i in range(40)]
ed = CodeEditor(); ed.resize(700, 900); ed.setPlainText("\n".join(lines) + "\n"); ed.show()
c = ed.textCursor(); c.movePosition(QTextCursor.MoveOperation.End); ed.setTextCursor(c)   # caret at the bottom
click_block = ed.document().findBlockByNumber(2)
pos = ed.cursorRect(QTextCursor(click_block)).center()
gpos = ed.viewport().mapToGlobal(pos)
out = {}

def pick_template():
    m = QApplication.activePopupWidget()
    out["popup_y"] = m.geometry().top()
    acts = [a for a in m.actions() if a.text() and a.isEnabled()]
    out["template"] = acts[0].text()
    acts[0].trigger(); m.close()

def pick_insert():
    m = QApplication.activePopupWidget()
    act = next(a for a in m.actions() if a.text().startswith("Insert Template"))
    QTimer.singleShot(150, pick_template)
    m.close(); act.trigger()

QTimer.singleShot(120, pick_insert)
ed.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, gpos))
loop = QEventLoop(); QTimer.singleShot(800, loop.quit); loop.exec()
out["click_y"] = gpos.y()
out["caret_y"] = ed.viewport().mapToGlobal(ed.cursorRect(ed.textCursor()).bottomLeft()).y()
text = ed.toPlainText().split("\n")
out["inserted_near_click"] = text[0:2] == lines[0:2] and text[2] != lines[2]
out["bottom_untouched"] = text[-2] == lines[-1]
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_template_popup_and_insert_follow_the_right_click():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "template" in out, out
    assert abs(out["popup_y"] - out["click_y"]) < 40, out     # at the click, not down at the caret
    assert out["inserted_near_click"] is True, out
    assert out["bottom_untouched"] is True, out
