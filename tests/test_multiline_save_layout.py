"""#1046: Edit as Grid and Edit as Region save one row (path) per line, indented to its line, as
Replace with Value writes a grid."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json
from PySide6.QtWidgets import QApplication, QMenu
app = QApplication([])
from belfryscad.window.data_viewers import build_editor_menu, find_editable_literals
from belfryscad.window.data_viewer_grid import GridViewer
from belfryscad.window.data_viewer_region import RegionViewer
out = {}
for kind, cls, text in (
        ("Grid", GridViewer,
         "module m() {\n    g = [[[0, 0, 0], [1, 0, 0]], [[0, 1, 0], [1, 1, 0.1234567]]];\n}\n"),
        ("Region", RegionViewer,
         "module m() {\n    r = [[[0, 0], [9, 0], [0, 9]], [[1, 1], [2, 1], [1, 2]]];\n}\n")):
    saved = []
    menu = QMenu()
    build_editor_menu(menu, text, find_editable_literals(text, text.index("[[[") + 1),
                      lambda t, s, e, text=text: saved.append(text[:s] + t + text[e:]))
    [a for a in menu.actions() if a.text().startswith(f"Edit as {kind}")][0].trigger()
    dlg = [w for w in app.topLevelWidgets() if isinstance(w, cls) and w.isVisible()][0]
    dlg._on_save()
    out[kind] = saved[0]
print(json.dumps(out))
'''


def test_grid_and_region_save_one_row_per_line():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["Region"] == ("module m() {\n"
                             "    r = [\n"
                             "        [[0, 0], [9, 0], [0, 9]],\n"
                             "        [[1, 1], [2, 1], [1, 2]]\n"
                             "    ];\n"
                             "}\n")
    assert out["Grid"] == ("module m() {\n"
                   "    g = [\n"
                   "        [[0, 0, 0], [1, 0, 0]],\n"
                   "        [[0, 1, 0], [1, 1, 0.123457]]\n"
                   "    ];\n"
                   "}\n")
