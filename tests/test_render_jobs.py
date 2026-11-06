"""Every render's job leaves `MainWindow._render_jobs` once its thread ends
(#1038). Animation waits for that list to empty before the next frame, so
one job left behind stopped playback for good while F5/F6 still worked.
It was lost when the thread's own deleteLater, a second connection to
thread.finished, ran before the job's removal, which Qt then dropped with the
thread: 1 job in 3,000 renders on macOS's own platform, up to 132 in 3,000 in
a stress test of the bare wiring, rarer offscreen. Too rare offscreen for a
test to catch, so this checks the wiring: one receiver, which removes the
job and only then deletes the thread. Qt widgets: a subprocess, offscreen."""
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
src = os.path.join(tempfile.mkdtemp(), "c.scad")
open(src, "w").write("cube(1);")
w.open_file_by_path(src, render=False)
from PySide6.QtCore import SIGNAL
w._render()
receivers = w._render_jobs[-1][2].receivers(SIGNAL("finished()"))
for i in range(1500):        # overlapping, as animation playback starts them
    w._render()
    if i % 3 == 0:
        app.processEvents()
deadline = time.time() + 60
while w._render_jobs and time.time() < deadline:
    app.processEvents(); time.sleep(0.005)
print(json.dumps({"left": len(w._render_jobs), "receivers": receivers}), flush=True)
hard_exit(0)
'''


def test_no_render_job_outlives_its_thread(tmp_path):
    driver = tmp_path / "_jobs.py"
    driver.write_text(DRIVER)
    r = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                       timeout=180, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"left": 0, "receivers": 1}
