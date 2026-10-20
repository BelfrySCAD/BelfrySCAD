"""#770: Design > Reload and Render (F4) reloads the tab from disk and renders,
even when only a file it includes changed.

MainWindow is a widget, so this runs in a subprocess with scratch settings,
like the other GUI-driving tests.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMenu
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
d = tempfile.mkdtemp()
main = os.path.join(d, "main.scad")
open(main, "w").write("cube(1);\\n")
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
w.open_file_by_path(main, render=False)
tab = w._current_tab()
renders = []
w._render = lambda *a, **k: renders.append(tab.editor.toPlainText())
out["in_menu"] = any(a.text().replace("&", "") == "Reload and Render" and a.shortcut().toString() == "F4"
                     for m in w.menuBar().findChildren(QMenu)
                     for a in m.actions())
open(main, "w").write("cube(2);\\n")
w._reload_and_render()
out["picked_up_disk"] = renders[-1:] == ["cube(2);\\n"]
w._reload_and_render()          # unchanged on disk: still renders
out["renders_when_unchanged"] = len(renders) == 2
# Unsaved edits, disk unchanged: nothing to ask about, the edits render.
asked = []
import belfryscad.window.main_window as mw
real_question = mw.QMessageBox.question
mw.QMessageBox.question = lambda *a, **k: asked.append(1) or mw.QMessageBox.StandardButton.Cancel
w.skip_unsaved_prompts = False
tab.editor.setPlainText("cube(3);\\n"); tab.is_modified = True
w._reload_and_render()
out["edits_render_unasked"] = renders[-1:] == ["cube(3);\\n"] and not asked
# Disk changes under unsaved edits: Automatic Reload only warns, and F4
# must still see that change -- and ask, since it would discard the edits.
open(main, "w").write("cube(4);\\n")
w._reload_one(main)
w._reload_and_render()
out["asks_when_disk_moved"] = len(asked) == 1 and tab.editor.toPlainText() == "cube(3);\\n"
mw.QMessageBox.question = real_question
w.skip_unsaved_prompts = True
w._reload_and_render()
out["then_reloads"] = renders[-1:] == ["cube(4);\\n"]
print(json.dumps(out)); sys.stdout.flush()
try:
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.close()
except Exception:
    pass
os._exit(0)
'''


def test_reload_and_render_reloads_only_a_changed_disk_and_always_renders():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"in_menu": True, "picked_up_disk": True, "renders_when_unchanged": True,
                   "edits_render_unasked": True, "asks_when_disk_moved": True,
                   "then_reloads": True}
