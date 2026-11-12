"""Preview of an empty editor clears the viewport (#1108), as OpenSCAD's
does. Qt widgets: a subprocess, offscreen."""
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
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
def settle():
    end = time.time() + 30
    while (w._render_jobs or time.time() < end - 29.8) and time.time() < end:
        app.processEvents(); time.sleep(0.01)
ed = w._current_editor()
ed.setPlainText("cube(1);"); w._render(); settle()
drawn = len(w._bodies)
ed.setPlainText("sphere(1);"); w._render()      # still in flight...
ed.setPlainText("   "); w._render(); settle()    # ...when the empty Preview clears
print(json.dumps([drawn, len(w._bodies)]), flush=True)
hard_exit(0)
'''


def test_preview_of_an_empty_editor_clears_the_viewport():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [1, 0]
