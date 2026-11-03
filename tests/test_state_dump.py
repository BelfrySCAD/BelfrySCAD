"""#703: `kill -USR1 <pid>` reports what Qt thinks is blocking input."""
import os
import subprocess
import sys

import pytest

DRIVER = r'''
import os, subprocess
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QWidget
app = QApplication([])
from belfryscad import main as m
w = QWidget(); w.show()
d = QDialog(w); d.setWindowModality(Qt.WindowModality.ApplicationModal); d.show()
m._install_state_dump(app)
if os.environ.get("CLICK"):
    from PySide6.QtTest import QTest
    QTest.mouseClick(w, Qt.MouseButton.LeftButton)
    QTest.keyClick(w, Qt.Key.Key_Q)
if os.environ.get("PAINT_FAIL"):
    # A 3D view whose drawing fails on every frame, without a GL context.
    from types import SimpleNamespace
    from belfryscad.window.viewport import Viewport
    def paint(**_):
        raise RuntimeError("context lost")
    fake = SimpleNamespace(defaultFramebufferObject=lambda: 0, _paint_extra=None,
                           _renderer=SimpleNamespace(paint=paint))
    for _ in range(3):
        Viewport.paintGL(fake)
# Sent from outside while the loop is idle: the handler still has to run.
subprocess.Popen(["sh", "-c", f"sleep 1; kill -USR1 {os.getpid()}"])
QTimer.singleShot(4000, lambda: os._exit(0))
app.exec()
'''


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGUSR1 on Windows")
def test_sigusr1_names_the_modal_while_idle():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert "SIGUSR1 state dump" in proc.stderr, proc.stderr[-2000:]
    assert "activeModalWidget=QDialog" in proc.stderr
    assert "modality=ApplicationModal" in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGUSR1 on Windows")
def test_sigusr1_reports_exposure_and_recent_window_events():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert "exposed=True" in proc.stderr, proc.stderr[-2000:]
    assert "screens: " in proc.stderr
    assert "3D view drawing errors: none" in proc.stderr
    assert "recent input events:\n  none" in proc.stderr
    assert "Expose QWindow" in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGUSR1 on Windows")
def test_sigusr1_lists_presses_and_releases_but_never_the_key():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "CLICK": "1"}, timeout=60)
    err = proc.stderr
    assert "MouseButtonPress LeftButton on QWidget" in err, err[-2000:]
    assert "MouseButtonRelease LeftButton on QWidget" in err
    assert "KeyPress on QWidget" in err and "Key_Q" not in err
    if sys.platform == "darwin":
        assert "AppKit:" in err and "main run loop mode=" in err


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGUSR1 on Windows")
def test_sigusr1_carries_the_whole_paint_error_printed_once():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PAINT_FAIL": "1"}, timeout=60)
    assert proc.stdout.count("paintGL error:") == 1, proc.stdout[-2000:]
    err = proc.stderr
    assert "3D view drawing errors: 1" in err, err[-2000:]
    assert "  3x, first " in err
    assert "    RuntimeError: context lost" in err


def test_animation_state_names_a_job_that_never_left():
    # #1038: a leftover render job blocks every animation frame.
    from types import SimpleNamespace as NS
    from belfryscad.main import _describe_animation
    win = NS(_animate_pane=NS(_playing=True, _dumping=False, _pending_advance="next",
                              _timer=NS(isActive=lambda: True), _step=3, _steps=20),
             _render_jobs=[(None, None, NS(isRunning=lambda: False))], _render_id=41)
    out = "\n".join(_describe_animation(win))
    assert "playing=True dumping=False pending_advance='next' timer_active=True step=3/20" in out
    assert "render_jobs=1 running=0 render_id=41" in out


REPORT_DRIVER = r'''
import io, sys
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from belfryscad import main as m
log = io.StringIO()
m._crash_stream = log
path = m.write_state_dump(app, "Diagnostic report")
print(path.endswith("crash.log"), "=== Diagnostic report " in log.getvalue(),
      "applicationState=" in log.getvalue())
'''


def test_diagnostic_report_writes_the_dump_to_the_crash_log():
    # Help > Write Diagnostic Report: the only trigger on Windows (#1038).
    proc = subprocess.run([sys.executable, "-c", REPORT_DRIVER], capture_output=True, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert proc.stdout.split() == ["True", "True", "True"], proc.stderr[-2000:]
