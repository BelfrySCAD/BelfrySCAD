"""Window ▸ Move Tab to New Window, including a window's last tab (#703).

Runs in a subprocess with the offscreen platform, like test_drop_files.py.
"""
import json
import subprocess
import sys

DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
asked = []
QMessageBox.question = lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.Cancel
QMessageBox.warning = QMessageBox.question
MainWindow._render = lambda *a, **k: None
out = {}
d = os.path.realpath(tempfile.mkdtemp())
scad = os.path.join(d, "main.scad")
open(scad, "w").write("cube(1);\\n")

def windows():
    return [w for w in app.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()]

w = MainWindow(); w.show()
w.open_file_by_path(scad)
w._current_tab().editor.setPlainText("cube(2);\\n")      # unsaved edit
out["tabs_before"] = w._tabs.count()
w._tear_off_tab()
app.processEvents()
wins = windows()
out["windows"] = len(wins)
out["old_visible"] = w.isVisible()
nw = wins[0]
t = nw._current_tab()
out["new"] = [t.file_path == scad, t.editor.toPlainText(), t.is_modified, nw._tabs.count()]
out["asked"] = len(asked)
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
for x in wins:
    try:
        x.persist_settings = False; x.skip_unsaved_prompts = True; x.close()
    except Exception:
        pass
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_last_tab_moves_and_old_window_closes():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out["tabs_before"] == 1
    assert out["windows"] == 1 and out["old_visible"] is False
    assert out["new"] == [True, "cube(2);\n", True, 1]
    assert out["asked"] == 0
