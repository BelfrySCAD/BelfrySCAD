"""The path editor's corners= Copy button belongs to NURBS Interpolated with
corners only (#803). It used to show in every mode until the mode was first
changed, since the first load never synced it (#861).

Qt widgets, so it runs in a subprocess (offscreen)."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.data_viewer_path import PathViewer
pts = [[0, 0], [10, 15], [25, 5], [40, 20]]
plain = PathViewer("p", pts, editable=True, is_2d=True)
interp = PathViewer("p", pts, editable=True, is_2d=True, corners=[1], preset={"mode": "nurbs_interp"})
print(json.dumps({"plain": not plain._corners_copy.isHidden(),
                  "interp": not interp._corners_copy.isHidden(),
                  "label": interp._corners_label.text()}))
sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_copy_button_shows_only_for_interpolated_corners():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"plain": False, "interp": True, "label": "corners=[1]"}
