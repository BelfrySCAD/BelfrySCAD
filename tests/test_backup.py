"""Rendering a buffer with unsaved edits backs it up, as OpenSCAD does, and
closing the tab removes the backup (#864). MainWindow in a subprocess."""
import json
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings, backups_dir
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never the real store
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
path = os.path.join(d, "part.scad")
open(path, "w").write("cube(1);")
w.open_file_by_path(path, render=False)
tab = w._current_tab()
out = {}
w._render(); out["unmodified"] = os.listdir(backups_dir()) if os.path.isdir(backups_dir()) else []
tab.editor.setPlainText("cube(2);")
w._render()
names = os.listdir(backups_dir())
out["names"] = [n.startswith("part-backup-") and n.endswith(".scad") for n in names]
out["text"] = open(os.path.join(backups_dir(), names[0])).read()
tab.editor.setPlainText("cube(3);")
w._render()
out["rewritten"] = [open(os.path.join(backups_dir(), n)).read() for n in os.listdir(backups_dir())]
w._close_tab(w._tabs.indexOf(tab))
out["after_close"] = os.listdir(backups_dir())
print(json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_a_modified_buffer_is_backed_up_at_render_until_the_tab_closes():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "unmodified": [], "names": [True], "text": "cube(2);",
        "rewritten": ["cube(3);"], "after_close": []}, r.stderr
