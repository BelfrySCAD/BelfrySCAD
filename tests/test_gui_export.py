"""File ▸ Export, end to end in a real window: render, pick a name, write.

It had no test that actually wrote a file, and from 1.55.0 to 1.58.0 every
GUI export failed with "name 'tab' is not defined" (a refactor for Export as
Image moved the line that defined it). Qt widgets, so it runs in a
subprocess with the offscreen platform.
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
import belfryscad.window.main_window as mw
w = mw.MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
src = os.path.join(d, "part.scad"); open(src, "w").write("cube(10);")
w.open_file_by_path(src)
deadline = time.time() + 60
while w._geometry is None and time.time() < deadline:
    app.processEvents(); time.sleep(0.05)
errors = []
mw.QMessageBox.critical = staticmethod(lambda parent, title, text: errors.append(text))
mw.QMessageBox.warning = staticmethod(lambda parent, title, text: errors.append(text))
from belfryscad.window.export_options import saved_values
mw.ask_export_options = lambda ext, *a, **k: saved_values(ext)   # the dialog's own defaults, OK pressed
out = {}
for ext, filt in ((".3mf", "3MF Files (*.3mf)"), (".stl", "STL Files (*.stl)")):
    path = os.path.join(d, "out" + ext)
    mw.QFileDialog.getSaveFileName = staticmethod(lambda *a, _p=path, _f=filt, **k: (_p, _f))
    w._export()
    out[ext] = os.path.exists(path) and os.path.getsize(path) > 0
out["errors"] = errors
print(json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def test_gui_export_writes_the_file():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    assert out[".3mf"] and out[".stl"]
