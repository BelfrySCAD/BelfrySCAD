"""#1046: Edit as Grid saves one row per line, indented to its line, as
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
text = "module m() {\n    g = [[[0, 0, 0], [1, 0, 0]], [[0, 1, 0], [1, 1, 0.1234567]]];\n}\n"
offset = text.index("[[[") + 1
saved = []
menu = QMenu()
build_editor_menu(menu, text, find_editable_literals(text, offset),
                  lambda t, s, e: saved.append(text[:s] + t + text[e:]))
[a for a in menu.actions() if a.text().startswith("Edit as Grid")][0].trigger()
dlg = [w for w in app.topLevelWidgets() if isinstance(w, GridViewer)][0]
dlg._on_save()
print(json.dumps(saved))
'''


def test_grid_saves_one_row_per_line():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    [out] = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == ("module m() {\n"
                   "    g = [\n"
                   "        [[0, 0, 0], [1, 0, 0]],\n"
                   "        [[0, 1, 0], [1, 1, 0.123457]]\n"
                   "    ];\n"
                   "}\n")
