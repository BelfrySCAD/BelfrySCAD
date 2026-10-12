"""Automatic Reload and Render watches what the open file uses/includes.

Editing a library in an outside editor must re-render the model that
includes it -- once, and not again while idle. Qt widgets: a subprocess,
offscreen.
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
os.mkdir(os.path.join(d, "lib"))
inc = os.path.join(d, "lib", "inc.scad")      # its own directory: no directory watch
used = os.path.join(d, "lib", "used.scad")    # reached only through inc.scad
for p, text in [(inc, "use <used.scad>\\nmodule m() cube(1);\\n"),
                (used, "function f() = 1;\\n")]:
    with open(p, "w") as f: f.write(text)
path = os.path.join(d, "t.scad")
with open(path, "w") as f: f.write("include <lib/inc.scad>\\nm();\\n")
renders = []
w._render = lambda tab, *a, **k: renders.append(tab.file_path)
w.open_file_by_path(path, render=False)
w._act_auto_reload.setChecked(True)

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)

out = {}
for name, p in [("inc", inc), ("used", used)]:
    spin(1.0); renders.clear()
    with open(p, "a") as f: f.write("// edited\\n")
    spin(2.0)
    out[name] = renders[:]
renders.clear(); spin(1.5)
out["idle"] = len(renders)
out["path"] = path
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_editing_a_used_or_included_file_renders_the_model_once():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["inc"] == [out["path"]]
    assert out["used"] == [out["path"]]
    assert out["idle"] == 0
