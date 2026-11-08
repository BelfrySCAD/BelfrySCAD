"""#1081: the grid editor keeps its gear-menu settings, per grid for the
session and the last used across runs."""
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
from belfryscad.window.data_viewer_grid import GridViewer
grid = [[[x, y, 0] for x in range(4)] for y in range(4)]
def state(d):
    return [d._face_mode_combo.currentText(), d._wrap_combo.currentIndex(),
            d._show_verts_cb.isChecked(), d._snap_combo.currentText(),
            d._degree_spins[0].value()]
out = {}
d = GridViewer("g", grid, editable=True)
out["fresh"] = state(d)
d._face_mode_combo.setCurrentText("NURBS Surface"); d._degree_spins[0].setValue(2)
d._wrap_combo.setCurrentIndex(1); d._show_verts_cb.setChecked(False); d._snap_combo.setCurrentText("1")
d.done(0)
d = GridViewer("h", grid, editable=True)
out["other_grid_gets_last"] = state(d)
d._face_mode_combo.setCurrentText("Bezier Patch"); d._wrap_combo.setCurrentIndex(0); d.done(0)
d = GridViewer("g", grid, editable=True); out["g_again"] = state(d); d.done(0)
GridViewer._by_grid.clear()          # as in a new run
d = GridViewer("h", grid, editable=True); out["new_run"] = state(d); d.done(0)
small = [[[0, 0, 0], [1, 0, 0]], [[0, 1, 0], [1, 1, 0]]]
GridViewer._by_grid["s"] = {"mode": "Bezier Patch", "degrees": [3, 3], "wrap": 9}
d = GridViewer("s", small, editable=True); out["unavailable"] = state(d)
d.done(0)
d = GridViewer("g", grid, editable=True); d.show(); app.processEvents()
d.setGeometry(100, 120, 640, 400); app.processEvents(); d.done(0)
d = GridViewer("g", grid, editable=True); d.show(); app.processEvents()
out["geometry"] = list(d.geometry().getRect())
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_grid_editor_keeps_its_settings():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out["fresh"][:3] == ["Grid Faces", 0, True]
    assert out["other_grid_gets_last"] == ["NURBS Surface", 1, False, "1", 2]
    assert out["g_again"] == ["NURBS Surface", 1, False, "1", 2]      # g's own, not h's
    assert out["new_run"] == ["NURBS Surface", 1, False, "1", 2]      # the last closed: g
    assert out["unavailable"][:2] == ["Grid Faces", 0]                # a 2x2 has no Bezier patch
    assert out["geometry"] == [100, 120, 640, 400]                  # reopens where it was closed
