"""Automatic Reload and Render must not loop.

Every render writes a temporary .scad beside the source (scad_temp) and
deletes it, so the watched directory reports a change after every render,
and each one queues a reload check of the open files there. That check
compared the file on disk with the editor's text -- and some text cannot
round-trip through the editor (a U+2028 line separator becomes a newline,
so the two never match). Then every check "found" a change, reloaded,
rendered, wrote another temp file, and so on, forever, even with the file
closed in the outside editor. Reported on Ubuntu.

A reload must happen only when the file on disk is different from what was
last read from or written to it. Qt widgets: a subprocess, offscreen.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad import scad_temp
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
path = os.path.join(d, "t.scad")
with open(path, "w", encoding="utf-8") as f:
    f.write("a = 1;\\u2028cube(a);\\n")       # text the editor cannot hold as-is
renders = []
def fake_render(tab, *a, **k):               # what a real render does to the directory
    renders.append(time.monotonic())
    scad_temp.remove(scad_temp.write_temp_scad(tab.editor.toPlainText(), near=path))
w._render = fake_render
w.open_file_by_path(path, render=False)
w._act_auto_reload.setChecked(True)

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)

spin(1.0); renders.clear()
with open(path, "w", encoding="utf-8") as f:   # an outside editor saves a real change
    f.write("a = 2;\\u2028cube(a);\\n")
spin(4.0)
out = {"renders_after_external_change": len(renders)}
renders.clear()
spin(2.0)
out["renders_while_idle"] = len(renders)
print(json.dumps(out)); sys.stdout.flush()
try:
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.close()
except Exception:
    pass
os._exit(0)
'''


def test_auto_reload_renders_once_per_change_and_does_not_loop():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["renders_after_external_change"] == 1
    assert out["renders_while_idle"] == 0
