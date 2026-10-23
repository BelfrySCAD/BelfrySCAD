"""Reopen the files open at the last quit (#880): recorded on close, restored
in order with the current one brought forward and the only one rendered.
Qt widgets: a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
from belfryscad.main import hard_exit
# A failure exits without Qt's teardown, which aborts on a live QThread and
# leaves a crash report behind.
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
d = tempfile.mkdtemp()
paths = [os.path.join(d, n) for n in ("a.scad", "b.scad", "gone.scad")]
for p in paths:
    with open(p, "w") as f: f.write("cube(1);\\n")

w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
for p in paths:
    w.open_file_by_path(p, render=False)
w._new_document()                # untitled: not recorded
w._tabs.setCurrentIndex(1)         # b.scad
w.close()
os.remove(paths[2])

w2 = MainWindow(); w2.skip_unsaved_prompts = True
renders = []
w2._render = lambda tab=None, *a, **k: renders.append(os.path.basename(tab.file_path))
out = {"restored": w2.restore_session(),
       "tabs": [os.path.basename(w2._tabs.widget(i).file_path) for i in range(w2._tabs.count())],
       "current": os.path.basename(w2._current_tab().file_path),
       "renders": renders}
w3 = MainWindow()
from belfryscad.settings import app_settings
app_settings().setValue("session/openFiles", [])
out["nothing_to_restore"] = w3.restore_session()
print(json.dumps(out)); sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_session_files_are_reopened_in_order_with_only_the_current_rendered():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["restored"] is True
    assert out["tabs"] == ["a.scad", "b.scad"]
    assert out["current"] == "b.scad"
    assert out["renders"] == ["b.scad"]
    assert out["nothing_to_restore"] is False
