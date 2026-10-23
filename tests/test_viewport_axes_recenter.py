"""#881: OpenSCAD's corner reference axes, chosen against the orientation
cube in Preferences, and its double-click to centre the view on the model.
Qt widgets: a subprocess, offscreen (no GL, so the hit test is stubbed)."""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
import numpy as np
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.axis_indicator import AxisIndicator
from belfryscad.engine.renderer import Camera
from belfryscad.window.main_window import MainWindow
out = {}

# Front view (camera on -Y): X points right, Z up, Y straight away.
cam = Camera(); cam.azimuth, cam.elevation = 270.0, 0.0
ind = AxisIndicator()
ind.set_orientation(cam.view_matrix())
tips = {i: (round(x, 3), round(y, 3)) for i, x, y, _ in ind.screen_axes()}
out["front"] = tips
out["farthest"] = ind.screen_axes()[0][0]

w = MainWindow(); w.skip_unsaved_prompts = True
vp = w._viewport
out["default"] = [vp._orientation_cube.isVisibleTo(vp), vp._axis_indicator.isVisibleTo(vp)]
from belfryscad.window.preferences import save_preferences
save_preferences({"viewport/orientationIndicator": "axes"})
w._apply_preferences()
out["axes"] = [vp._orientation_cube.isVisibleTo(vp), vp._axis_indicator.isVisibleTo(vp)]

# Double-click: the hit point becomes the camera target; a miss changes nothing.
hits = [(np.array([5.0, 6.0, 7.0]), None, None, 0), None]
vp._renderer.ray_cast_point = lambda o, d: hits.pop(0)
cam = vp._renderer.camera
dist = cam.distance
QTest.mouseDClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(50, 50))
out["target"] = [round(float(v), 3) for v in cam.target]
out["distance_kept"] = cam.distance == dist
QTest.mouseDClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 60))
out["miss_kept"] = [round(float(v), 3) for v in cam.target]
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_reference_axes_preference_and_double_click_recentre():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    L = 30
    assert out["front"]["0"] == [L, 0]
    assert out["front"]["2"] == [0, -L]       # Qt's y grows downward
    assert out["front"]["1"] == [0, 0]
    assert out["farthest"] == 1               # +Y points away from a front view
    assert out["default"] == [True, True]
    assert out["axes"] == [False, True]
    assert out["target"] == [5.0, 6.0, 7.0]
    assert out["distance_kept"] is True
    assert out["miss_kept"] == [5.0, 6.0, 7.0]
