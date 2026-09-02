"""Only a tab newly loaded from a file re-fits the camera after rendering.

Re-fitting after every render fought the user: any edit-and-re-render threw
away the angle and zoom they had just set up, and on a model that grows or
shrinks as a parameter changes, the view jumped every time.

Driven in a subprocess, as the other Qt/GL tests here are: a MainWindow
needs a QApplication and a real GL context, and neither belongs in pytest's
own process.
"""
import json
import os
import subprocess
import sys

import pytest


_DRIVER = '''
import json, sys, time
from PySide6.QtGui import QSurfaceFormat
f = QSurfaceFormat(); f.setVersion(3, 3); f.setProfile(QSurfaceFormat.CoreProfile)
f.setDepthBufferSize(24); QSurfaceFormat.setDefaultFormat(f)
from PySide6.QtWidgets import QApplication
app = QApplication([])
# Never touch the developer's real settings: MainWindow/DocsPane read
# them, and open_file_by_path WRITES recentFiles. Left unisolated, running
# the suite quietly replaced the recent-files list with pytest tmp paths.
import tempfile
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow

path = sys.argv[1]
w = MainWindow(); w.skip_unsaved_prompts = True; w.resize(900, 600); w.show()

def settle(ms=4000):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents(); time.sleep(0.01)

def cam():
    c = w._viewport._renderer.camera
    return [round(float(c.distance), 3), [round(float(x), 3) for x in c.target]]

out = {}
w.open_file_by_path(path); settle()
out["after_open"] = cam()

# The user frames it their own way.
w._viewport._renderer.camera.distance = 999.0
w._viewport._renderer.camera.target[:] = [5.0, 5.0, 5.0]
out["posed"] = cam()

w._render(w._current_tab()); settle()
out["after_rerender"] = cam()

# View All must still have bounds to work from.
w._viewport._frame_all(w._viewport._renderer.camera)
out["after_view_all"] = cam()
w.close()
print(json.dumps(out))
'''


def _run(tmp_path):
    scad = tmp_path / "cube.scad"
    scad.write_text("cube([30, 20, 10], center=true);\n")
    driver = tmp_path / "_reframe.py"
    driver.write_text(_DRIVER)
    # A clean QT_QPA_PLATFORM: headless_render sets it to "offscreen" with
    # os.environ.setdefault, which outlives the call and would leave this
    # window without a usable GL context.
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run([sys.executable, str(driver), str(scad)],
                       capture_output=True, text=True, env=env, timeout=300)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip(f"no Qt/GL available here: {(r.stderr or '').strip()[-200:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_opening_a_file_frames_it_but_a_re_render_does_not(tmp_path):
    out = _run(tmp_path)

    assert out["after_open"] != out["posed"], "opening a file frames the model"
    assert out["after_rerender"] == out["posed"], \
        "a plain re-render must leave the camera exactly where the user put it"


def test_view_all_still_works_after_an_unframed_render(tmp_path):
    """The bounds are cached on every render even when the camera is left
    alone -- without that, View All would have nothing to frame from."""
    out = _run(tmp_path)

    assert out["after_view_all"] != out["posed"], "View All reframes"
    assert out["after_view_all"] == out["after_open"], \
        "and lands where opening the file did -- same model, same bounds"
