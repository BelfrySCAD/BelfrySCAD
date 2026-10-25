"""The drawing editors open at 90% of the screen, centred, with a maximize
button (#861). Qt widgets: a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.data_viewer_path import PathViewer
from belfryscad.window.data_viewer_region import RegionViewer
from belfryscad.window.data_viewer_turtle import TurtleViewer, Turtle3DViewer
area = app.primaryScreen().availableGeometry()
out = {"area": [area.x(), area.y(), area.width(), area.height()]}
for name, dlg in (("path", PathViewer("p", [[0, 0], [10, 5]], editable=True, is_2d=True)),
                  ("region", RegionViewer("r", [[[0, 0], [10, 0], [0, 10]]], editable=True)),
                  ("turtle", TurtleViewer("t", ["move", 10], editable=True)),
                  ("turtle3d", Turtle3DViewer("t", ["move", 10], editable=True))):
    g = dlg.geometry()
    out[name] = {"geom": [g.x(), g.y(), g.width(), g.height()],
                 "max": bool(dlg.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)}
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_drawing_editors_open_at_most_of_the_screen_with_maximize():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    ax, ay, aw, ah = out["area"]
    for name in ("path", "region", "turtle", "turtle3d"):
        x, y, w, h = out[name]["geom"]
        assert (w, h) == (int(aw * 0.9), int(ah * 0.9)), name
        assert abs((x - ax) - (ax + aw - x - w)) <= 1, name     # centred
        assert abs((y - ay) - (ay + ah - y - h)) <= 1, name
        assert out[name]["max"], name
