"""The orientation cube's right-click menu of views, showing the View menu's
shortcuts, and its double-click to isometric. Qt widgets: a subprocess,
offscreen."""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.main_window import MainWindow

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)

def settle(timeout=10.0):
    """Until the view swing has landed: a fixed wait was too short on the
    slow macOS runner, which read the camera 0.3 degrees short of Right."""
    spin(0.1)
    end = time.monotonic() + timeout
    while vp._view_anim is not None and time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)

w = MainWindow(); w.skip_unsaved_prompts = True; w.resize(1000, 700); w.show()
spin(0.3)
vp = w._viewport
cube = vp._orientation_cube
cam = vp._renderer.camera
out = {}

# The menu, caught while it is up (QMenu.exec cannot be patched).
seen = {}
def grab():
    menu = QApplication.activePopupWidget()
    if menu is None:
        return
    acts = [a for a in menu.actions() if not a.isSeparator()]
    seen["items"] = [[a.text().replace("&", ""), a.shortcut().toString()] for a in acts]
    next(a for a in acts if a.text().replace("&", "") == "Right").trigger()
    menu.close()
QTimer.singleShot(100, grab)
c = QPoint(cube.width() // 2, cube.height() // 2)
cube.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, c, cube.mapToGlobal(c)))
settle()
out["items"] = seen.get("items")
out["after_right"] = [round(cam.azimuth, 3), round(cam.elevation, 3)]

# A right press on the cube is the cube's: the viewport must not start a
# right-drag pan that the menu then takes the release of, leaving it
# panning on every later move with no button down.
QTest.mousePress(cube, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, c)
out["viewport_button_after_right_press"] = str(vp._mouse_button)
QTest.mouseRelease(cube, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, c)

# Double-click on the cube: isometric, after the first click's snap.
QTest.mouseDClick(cube, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c)
settle()
out["after_double"] = [round(cam.azimuth, 3), round(cam.elevation, 3)]
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_cube_menu_lists_views_with_shortcuts_and_double_click_goes_isometric():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    names = [n for n, _ in out["items"]]
    assert names == ["Top", "Bottom", "Left", "Right", "Front", "Back",
                     "Isometric", "View All", "Center"]
    keys = dict(out["items"])
    assert keys["Top"] == "Ctrl+4"           # the View menu's, as rebound or not
    assert keys["Isometric"] == "Ctrl+0"
    assert keys["View All"] == "Ctrl+Shift+V"
    assert out["after_right"] == [0.0, 0.0]
    assert out["after_double"] == [295.0, 35.0]
    assert out["viewport_button_after_right_press"] == "None"
