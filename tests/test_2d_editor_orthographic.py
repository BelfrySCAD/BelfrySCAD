"""A 2D editor's view is orthographic for good (#882): no perspective button,
and neither that button's handler nor View > Perspective can turn it off.
Every 3D editor keeps the button, in the viewport's upper-left corner.

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
from belfryscad.window.main_window import MainWindow
out = {}
for name, pts, is_2d in (("2d", [[0, 0], [10, 5]], True), ("3d", [[0, 0, 0], [10, 5, 3]], False)):
    vp = PathViewer("p", pts, editable=True, is_2d=is_2d)._vp
    vp._on_perspective_button_clicked()
    after_button = vp._renderer.camera.orthographic
    vp._renderer.camera.orthographic = True
    host = type("Host", (), {"_target_viewport": lambda self: vp,
                             "_act_perspective": type("A", (), {"blockSignals": lambda s, b: None,
                                                                "setChecked": lambda s, c: None})()})()
    MainWindow._toggle_perspective(host, True)
    out[name] = {"button": not vp._persp_btn.isHidden(), "after_button": after_button,
                 "after_menu": vp._renderer.camera.orthographic}
from belfryscad.window.data_viewer_grid import GridViewer
from belfryscad.window.data_viewer_vnf import VNFViewer
from belfryscad.window.data_viewer_vnf_tile import VNFTileViewer
from belfryscad.window.data_viewer_affine import AffineMatrixViewer
from belfryscad.window.data_viewer_heightfield import HeightfieldViewer
from belfryscad.window.data_viewer_turtle import Turtle3DViewer, TurtleViewer
from belfryscad.window.data_viewer_region import RegionViewer
cube = [[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]],
        [[0,1,2,3],[4,7,6,5],[0,4,5,1],[1,5,6,2],[2,6,7,3],[3,7,4,0]]]
editors = {
    "path3d": lambda e: PathViewer("p", [[0,0,0],[10,5,3]], editable=e, is_2d=False),
    "grid3d": lambda e: GridViewer("g", [[[0,0,0],[1,0,0]],[[0,1,0],[1,1,1]]], editable=e),
    "vnf": lambda e: VNFViewer("v", cube, editable=e),
    "vnf_tile": lambda e: VNFTileViewer("v", cube, editable=e),
    "affine3d": lambda e: AffineMatrixViewer("a", [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], editable=e),
    "heightfield": lambda e: HeightfieldViewer("h", [[0,1],[1,2]], editable=e),
    "turtle3d": lambda e: Turtle3DViewer("t", ["move", 10, "up", 90, "move", 5], editable=e),
    "path2d": lambda e: PathViewer("p", [[0,0],[10,5]], editable=e, is_2d=True),
    "grid2d": lambda e: GridViewer("g", [[[0,0],[1,0]],[[0,1],[1,1]]], editable=e),
    "affine2d": lambda e: AffineMatrixViewer("a", [[1,0,0],[0,1,0],[0,0,1]], editable=e),
    "turtle2d": lambda e: TurtleViewer("t", ["move", 10, "left", 90, "move", 5], editable=e),
    "region": lambda e: RegionViewer("r", [[[0,0],[10,0],[10,10]]], editable=e),
}
for name, make in editors.items():
    for e in (False, True):
        w = make(e)
        b = w._vp._persp_btn
        out[f"{name}{'/edit' if e else ''}"] = [not b.isHidden(), b.x(), b.y()]
print(json.dumps(out))
sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_2d_editors_stay_orthographic():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["2d"] == {"button": False, "after_button": True, "after_menu": True}
    # 3D starts perspective: the button makes it orthographic, the menu back.
    assert out["3d"] == {"button": True, "after_button": True, "after_menu": False}
    for name, (shown, x, y) in out.items():
        if not isinstance(shown, bool):
            continue
        assert shown == ("2d" not in name and "region" not in name), name
        assert (x, y) == (12, 12), name     # upper-left corner
