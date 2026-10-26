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
