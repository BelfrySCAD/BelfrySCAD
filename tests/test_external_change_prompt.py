"""#1094: an open file changed by another program is asked about when
BelfrySCAD will not reload it by itself -- Automatic Reload off, or unsaved
edits in the tab -- and the question waits while the app is in the background.
Qt widgets: a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow
state = [Qt.ApplicationState.ApplicationActive]
QApplication.applicationState = staticmethod(lambda: state[0])
answers, asked = [], []
MainWindow._confirm_external_reload = lambda self, tab: (asked.append(tab.is_modified), answers.pop(0))[1]
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
d = tempfile.mkdtemp()
path = os.path.join(d, "t.scad")
with open(path, "w") as f: f.write("cube(1);\\n")
w.open_file_by_path(path, render=False)
w._act_auto_reload.setChecked(False)
w._refresh_watched_files()
ed = w._current_editor()

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)
def outside(text):
    spin(0.5)
    with open(path, "w") as f: f.write(text)
    spin(1.5)

out = {}
answers.append(False); outside("cube(2);\\n")              # kept
out["kept"] = (asked[:], ed.toPlainText())
outside("cube(2);\\n")                                      # same change: not asked again
out["not_again"] = len(asked)
answers.append(True); outside("cube(3);\\n")               # reloaded
out["reloaded"] = (len(asked), ed.toPlainText(), w._current_tab().is_modified)
state[0] = Qt.ApplicationState.ApplicationInactive
outside("cube(4);\\n")
out["background"] = len(asked)
answers.append(True); state[0] = Qt.ApplicationState.ApplicationActive
app.applicationStateChanged.emit(state[0]); spin(0.5)
out["on_return"] = (len(asked), ed.toPlainText())
w._act_auto_reload.setChecked(True); w._refresh_watched_files()
outside("cube(5);\\n")                                      # clean tab + auto: no question
out["auto_clean"] = (len(asked), ed.toPlainText())
ed.insertPlainText("// mine\\n")
answers.append(False); outside("cube(6);\\n")              # unsaved edits: asked even with auto
out["auto_dirty"] = (asked[-1], len(asked), ed.toPlainText())
print(json.dumps(out)); sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_external_change_is_asked_about():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {
        "kept": [[False], "cube(1);\n"],
        "not_again": 1,
        "reloaded": [2, "cube(3);\n", False],
        "background": 2,
        "on_return": [3, "cube(4);\n"],
        "auto_clean": [3, "cube(5);\n"],
        "auto_dirty": [True, 4, "// mine\ncube(5);\n"],
    }, out
