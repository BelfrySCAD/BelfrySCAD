"""The orientation cube and axis indicator join anaglyph mode: each eye
drawn turned by its toe-in and combined with the renderer's matrices."""
import json
import os
import subprocess
import sys

import numpy as np

from belfryscad.engine.renderer import Camera
from belfryscad.window.anaglyph_overlay import eye_rotations


def test_a_near_point_is_crossed_so_it_sits_in_front():
    left, right = eye_rotations(Camera(), np.eye(3))
    near = np.array([0.0, 0.0, 1.0])            # view space: +z is toward the viewer
    assert (left @ near)[0] > 0 > (right @ near)[0]


DRIVER = r'''
import json, os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
import numpy as np
from belfryscad.engine.renderer import Camera
from belfryscad.window.axis_indicator import AxisIndicator
from belfryscad.window.orientation_cube import OrientationCube
cam = Camera(); cam.anaglyph = True; cam.azimuth, cam.elevation = 30, 25
out = {}
def pixels(w):
    img = w.grab().toImage().convertToFormat(w.grab().toImage().Format.Format_RGB888)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())
    return a[:, :img.width() * 3].reshape(img.height(), img.width(), 3).astype(int)
for cls in (AxisIndicator, OrientationCube):
    w = cls(); w.set_orientation(cam.view_matrix())
    plain = pixels(w)
    w.stereo = lambda: (cam, (0.82, 0.82, 0.82))
    ana = pixels(w)
    n = cls.__name__
    out[n + "_plain_green"] = bool(((plain[..., 1] - plain[..., 0] > 60) & (plain[..., 1] - plain[..., 2] > 60)).any())
    out[n + "_cyan_is_gray"] = int(np.abs(ana[..., 1] - ana[..., 2]).max())
    out[n + "_fringes"] = int((np.abs(ana[..., 0] - ana[..., 1]) > 40).sum())
    out[n + "_hits"] = len(getattr(w, "_drawn", [1]))
print(json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_overlays_are_composited_per_eye():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    o = json.loads(proc.stdout.strip().splitlines()[-1])
    assert o["AxisIndicator_plain_green"]            # the Y axis, drawn plain
    for n in ("AxisIndicator", "OrientationCube"):
        assert o[n + "_cyan_is_gray"] <= 2, o        # grayscale: the right eye feeds G and B alike
        assert o[n + "_fringes"] > 10, o             # the eyes differ: there is depth
    assert o["OrientationCube_hits"] > 0             # hit-testing still has the centre view
