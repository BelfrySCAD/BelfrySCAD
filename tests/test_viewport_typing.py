"""Text typed in the main viewport goes to the code editor (#841).

Qt widgets, so it runs in a subprocess (offscreen)."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
import belfryscad.window.main_window as mw
w = mw.MainWindow(); w.skip_unsaved_prompts = True
w.show(); app.processEvents()
e, vp = w._current_editor(), w._viewport
e.setPlainText("cube(1);\nsphere(2);")
c = e.textCursor(); c.setPosition(9); e.setTextCursor(c)    # start of line 2
vp.setFocus(); app.processEvents()
QTest.keyClicks(vp, "x=1;")
out = {"typed": e.toPlainText(), "caret": e.textCursor().position(), "focus": e.hasFocus()}
# A Cmd/Ctrl key is not text for the editor.
vp.setFocus(); app.processEvents()
QTest.keyClick(vp, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
out["ctrl"] = e.toPlainText()
# With nothing selected, an arrow key moves the editor's caret and takes the
# focus there, as a way back to the editor.
vp.setFocus(); app.processEvents()
QTest.keyClick(vp, Qt.Key.Key_Right)
out["arrow"] = [e.textCursor().position(), e.hasFocus()]
# With a selection, the arrows nudge it instead, and the editor is left alone.
nudges = []
vp.translate_committed.connect(lambda *d: nudges.append(d))
vp._renderer.selected_id = 1
before = e.textCursor().position()
vp.setFocus(); app.processEvents()
QTest.keyClick(vp, Qt.Key.Key_Right)
out["selected_arrow"] = [len(nudges), e.textCursor().position() == before, vp.hasFocus()]
vp._renderer.selected_id = None
# A read-only editor takes nothing, and keeps the focus where it was.
e.setReadOnly(True); vp.setFocus(); app.processEvents()
QTest.keyClicks(vp, "y")
out["readonly"] = [e.toPlainText(), vp.hasFocus()]
# ...except caret keys, which still take the focus back to it.
QTest.keyClick(vp, Qt.Key.Key_Left)
out["readonly_arrow"] = [e.toPlainText(), e.hasFocus()]
print(json.dumps(out)); sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_typing_in_the_viewport_goes_to_the_editor():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["typed"] == "cube(1);\nx=1;sphere(2);"
    assert out["caret"] == 13 and out["focus"]
    assert out["ctrl"] == out["typed"]
    assert out["readonly"] == [out["typed"], True]
    assert out["arrow"] == [14, True]
    assert out["selected_arrow"] == [1, True, True]
    assert out["readonly_arrow"] == [out["typed"], True]
