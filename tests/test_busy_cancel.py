"""The busy overlay's close box (issue #530).

"In addition to pressing the ESC key to cancel a render, it would be nice
to have a close icon as part of the execution time counter."

Escape and the close box are the same action, so `MainWindow._cancel_busy`
is the single implementation both use -- it cancels a render if one is
running, otherwise stops a debug session. The viewport only reports the
click; it does not know what cancelling means.

Subprocess driver: a Viewport needs a real QApplication and a GL context.
"""
import json
import os
import subprocess
import sys

import pytest


_DRIVER = '''
import json, sys
from PySide6.QtGui import QSurfaceFormat
f = QSurfaceFormat(); f.setVersion(3, 3)
f.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
f.setDepthBufferSize(24); QSurfaceFormat.setDefaultFormat(f)
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication([])
from belfryscad.window.viewport import Viewport

vp = Viewport(); vp.resize(700, 300); vp.show()
app.processEvents()

fired = []
vp.busy_cancel_requested.connect(lambda: fired.append(1))
out = {}

out["hidden_when_idle"] = vp._busy_cancel.isVisible()

# A render shows both halves of the overlay.
vp.set_render_busy(True); app.processEvents()
out["render_label"] = vp._busy_label.isVisible()
out["render_cancel"] = vp._busy_cancel.isVisible()
lab, can = vp._busy_label.geometry(), vp._busy_cancel.geometry()
out["side_by_side"] = can.left() >= lab.right()
out["same_top"] = lab.top() == can.top()
out["pair_centred"] = abs(((lab.left() + can.right()) // 2) - vp.width() // 2) <= 2
out["hand_cursor"] = vp._busy_cancel.cursor().shape() == Qt.CursorShape.PointingHandCursor
out["has_tooltip"] = bool(vp._busy_cancel.toolTip())

vp._busy_cancel.click()
out["click_emits"] = bool(fired)

vp.set_render_busy(False); app.processEvents()
out["after_render_label"] = vp._busy_label.isVisible()
out["after_render_cancel"] = vp._busy_cancel.isVisible()

# And a debug session, which the request did not mention but which uses
# the same overlay -- the close box has to work there too.
vp.set_debug_busy(True); app.processEvents()
out["debug_cancel"] = vp._busy_cancel.isVisible()
vp.set_debug_busy(False); app.processEvents()
out["after_debug_cancel"] = vp._busy_cancel.isVisible()

print(json.dumps(out))
'''


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    driver = tmp_path_factory.mktemp("busycancel") / "driver.py"
    driver.write_text(_DRIVER)
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run([sys.executable, str(driver)],
                       capture_output=True, text=True, env=env, timeout=180)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip(f"no Qt/GL available here: {(r.stderr or '').strip()[-200:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_absent_until_something_is_running(out):
    assert out["hidden_when_idle"] is False


def test_appears_with_the_counter_during_a_render(out):
    assert out["render_label"] is True
    assert out["render_cancel"] is True


def test_sits_beside_the_counter_without_shifting_it_off_centre(out):
    """The pair is centred, not the label -- otherwise the countdown jumps
    sideways the moment the close box appears."""
    assert out["side_by_side"] is True
    assert out["same_top"] is True
    assert out["pair_centred"] is True


def test_says_it_is_clickable(out):
    """The whole window is under a wait cursor while busy; a pointing hand
    here is what tells the user this one thing still responds -- which is
    what the request asked for."""
    assert out["hand_cursor"] is True
    assert out["has_tooltip"] is True


def test_clicking_reports_it(out):
    assert out["click_emits"] is True


def test_both_halves_go_away_together(out):
    assert out["after_render_label"] is False
    assert out["after_render_cancel"] is False


def test_a_debug_session_gets_one_too(out):
    assert out["debug_cancel"] is True
    assert out["after_debug_cancel"] is False
