"""#395 (save is not a render), #389 (undo cursor), #388 (error squiggle grows).

Qt widgets, so each check runs in a subprocess with the offscreen platform --
the same pattern as test_editor_annoyances.py.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import Qt, QEvent
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.editor import CodeEditor
from belfryscad.window.main_window import MainWindow
out = {}

def key(widget, k, text):
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier, text))

# --- #388: the squiggle must not outlive the next edit ----------------
ed = CodeEditor()
ed.setPlainText('text("Hi", 10, font="");')
ed.set_error_location(1, 20)
out["squiggle_before"] = len(ed._error_selections)
c = ed.textCursor(); c.setPosition(21); ed.setTextCursor(c)
key(ed, Qt.Key.Key_A, "a")
out["squiggle_after_typing"] = len(ed._error_selections)

# --- MainWindow for #389 and #395 -------------------------------------
w = MainWindow(); w.skip_unsaved_prompts = True
renders = []
w._render = lambda tab, *a, **k: renders.append(tab)
d = tempfile.mkdtemp()
path = os.path.join(d, "t.scad")
open(path, "w").write("abc\\ndef\\n")
w.open_file_by_path(path)
tab = w._current_tab()
ed = tab.editor
renders.clear()

# #389: edit at the end of line 1, then move the cursor (no edit) to the
# start and edit there. Undo must put the cursor where the SECOND edit
# began (0), not where the first one ended.
c = ed.textCursor(); c.setPosition(3); ed.setTextCursor(c)
key(ed, Qt.Key.Key_X, "X")                      # "abcX\\ndef\\n"
c = ed.textCursor(); c.setPosition(0); ed.setTextCursor(c)   # a plain move
key(ed, Qt.Key.Key_Y, "Y")                      # "YabcX\\ndef\\n"
out["text_after_edits"] = ed.toPlainText()
w._undo_stack.undo()
out["text_after_undo"] = ed.toPlainText()
out["cursor_after_undo"] = ed.textCursor().position()
w._undo_stack.undo()
out["cursor_after_second_undo"] = ed.textCursor().position()

# #395: a save renders only with Automatic Reload and Render on.
w._act_auto_reload.setChecked(False)
renders.clear(); w._write_file(tab, path); out["renders_on_save_auto_off"] = len(renders)
w._act_auto_reload.setChecked(True)
renders.clear(); w._write_file(tab, path); out["renders_on_save_auto_on"] = len(renders)
w._act_auto_reload.setChecked(False)
print(json.dumps(out)); sys.stdout.flush()
# MainWindow owns threads that abort Qt's teardown; the answers are out, skip it.
os._exit(0)
'''


def _run():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_editor_trio():
    out = _run()
    # #388
    assert out["squiggle_before"] == 1
    assert out["squiggle_after_typing"] == 0
    # #389
    assert out["text_after_edits"] == "YabcX\ndef\n"
    assert out["text_after_undo"] == "abcX\ndef\n"
    assert out["cursor_after_undo"] == 0, "cursor must be where the undone edit began, not where the previous edit ended"
    assert out["cursor_after_second_undo"] == 3
    # #395
    assert out["renders_on_save_auto_off"] == 0
    assert out["renders_on_save_auto_on"] == 1
