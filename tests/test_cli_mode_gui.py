"""`belfryscad --render FILE` / `--preview FILE` without -o (#898): the file
is rendered or previewed once it is open -- in the window that already has
it, when an external editor saves and then asks.

That save, and one to a file it includes, is also news to Automatic Reload
and Preview. Its Preview must not land after, and replace, the Render just
asked for. Qt widgets: a subprocess, offscreen.
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
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
os.mkdir(os.path.join(d, "lib"))
inc = os.path.join(d, "lib", "inc.scad")
with open(inc, "w") as f: f.write("module m() cube(1);\\n")
path = os.path.join(d, "t.scad")
with open(path, "w") as f: f.write("include <lib/inc.scad>\\nm();\\n")
other = os.path.join(d, "u.scad")
with open(other, "w") as f: f.write("cube(2);\\n")
renders = []
w._render = lambda tab=None, *a, preview=True, **k: renders.append("preview" if preview else "render")

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)

out = {}
w.open_file_by_path(path, render=False)
w._act_auto_reload.setChecked(True)
spin(1.0); renders.clear()
# An external editor saves the file and a library, then runs --render.
with open(inc, "a") as f: f.write("// edited\\n")
with open(path, "a") as f: f.write("// saved\\n")
w.open_file_by_path(path, mode="render")
spin(2.5)
out["save_then_render"] = renders[:]
out["picked_up_save"] = w._current_tab().editor.toPlainText().endswith("// saved\\n")
# Without a mode, switching to an open file renders nothing, as before.
renders.clear()
w.open_file_by_path(path)
spin(0.5)
out["no_mode"] = renders[:]
# A file not yet open, with Render a file when it is opened off.
from belfryscad.window.preferences import save_preferences
save_preferences({"render/onOpen": False})
renders.clear()
w.open_file_by_path(other, mode="preview")
spin(0.5)
out["new_tab_preview"] = renders[:]
print(json.dumps(out)); sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_render_flag_in_the_gui_is_not_followed_by_an_automatic_preview():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["save_then_render"] == ["render"]
    assert out["picked_up_save"] is True
    assert out["no_mode"] == []
    assert out["new_tab_preview"] == ["preview"]
