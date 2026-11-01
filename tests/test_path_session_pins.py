"""Pinned points survive closing the path editor, for the session (#947):
reopening the points Save wrote brings their pins back; other points don't.
Qt widgets: a subprocess, offscreen."""
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
from belfryscad.main import hard_exit
from belfryscad.window.data_viewer_path import PathViewer
pts = [[0, 0], [10, 0.3333333], [20, 0], [30, 5]]
saved = []
dlg = PathViewer("p", [list(p) for p in pts], editable=True, is_2d=True)
dlg.committed.connect(saved.append)
dlg._toggle_pin(2)
dlg._on_save()
reread = json.loads(saved[0])                    # the points as the script now has them
again = PathViewer("p", reread, editable=True, is_2d=True)
other = PathViewer("p", [[0, 0], [10, 1], [20, 0], [30, 5]], editable=True, is_2d=True)
out = {"text": saved[0], "pins_back": sorted(again._pins), "vp_pins": sorted(again._vp.pins),
       "other": sorted(other._pins)}
print(json.dumps(out)); sys.stdout.flush(); hard_exit(0)
'''


def test_pins_survive_reopening_the_saved_points():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["pins_back"] == [2] and out["vp_pins"] == [2], out
    assert out["other"] == [], out
