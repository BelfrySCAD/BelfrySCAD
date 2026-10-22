"""Automatic Reload and Render picks up every way an outside editor saves.

In place (truncate and write), replace-by-rename (write a temp file, then
os.replace it over the original -- what many Windows editors do), and
delete-then-recreate. Each must render the tab exactly once. Run on every
CI platform, since the file watcher is a different engine on each.
Qt widgets: a subprocess, offscreen.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
path = os.path.join(d, "t.scad")
with open(path, "w") as f: f.write("cube(1);\\n")
renders = []
w._render = lambda tab, *a, **k: renders.append(1)
w.open_file_by_path(path, render=False)
w._act_auto_reload.setChecked(True)

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)

def retrying(op):
    # Windows refuses a rename or delete while anything has the file open --
    # BelfrySCAD reading it on the last change, or a virus scanner on the new
    # temp file -- and editors that save this way retry. So does this.
    for _ in range(100):
        try:
            return op()
        except PermissionError:
            spin(0.05)
    return op()

def in_place(text):
    with open(path, "w") as f: f.write(text)
def by_rename(text):
    tmp = os.path.join(d, "t.scad.tmp")
    with open(tmp, "w") as f: f.write(text)
    retrying(lambda: os.replace(tmp, path))
def recreate(text):
    retrying(lambda: os.remove(path)); spin(0.5)
    with open(path, "w") as f: f.write(text)

out = {}
for n, (name, save) in enumerate([("in_place", in_place), ("by_rename", by_rename),
                                  ("recreate", recreate), ("in_place_after", in_place)]):
    spin(1.0); renders.clear()
    save(f"cube({n + 2});\\n")
    spin(3.0)
    out[name] = len(renders)
print(json.dumps(out)); sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_every_save_style_renders_once():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"in_place": 1, "by_rename": 1, "recreate": 1, "in_place_after": 1}, out
