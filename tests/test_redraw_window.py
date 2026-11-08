"""Window ▸ Redraw Window (#703): a new native window, nothing else lost."""
import json
import subprocess
import sys

DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
MainWindow._render = lambda *a, **k: None
d = os.path.realpath(tempfile.mkdtemp())
for n in ("a.scad", "b.scad"):
    open(os.path.join(d, n), "w").write("cube(1);\\n")
w = MainWindow(); w.show(); app.processEvents()
w.open_file_by_path(os.path.join(d, "a.scad")); w.open_file_by_path(os.path.join(d, "b.scad"))
w._current_tab().editor.setPlainText("cube(2);\\n")
w.setGeometry(100, 120, 700, 500); app.processEvents()
before = [int(w.winId()), list(w.geometry().getRect())]
w._redraw_window(); app.processEvents()
out = {"new_window": int(w.winId()) != before[0], "geometry": list(w.geometry().getRect()) == before[1],
       "visible": w.isVisible(), "tabs": w._tabs.count(),
       "text": w._current_tab().editor.toPlainText(), "modified": w._current_tab().is_modified}
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
try:
    w.persist_settings = False; w.skip_unsaved_prompts = True; w.close()
except Exception:
    pass
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_redraw_window_keeps_everything():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out == {"new_window": True, "geometry": True, "visible": True, "tabs": 2,
                   "text": "cube(2);\n", "modified": True}
