"""#703: a burst of display changes rebuilds each visible window once, after
the screens settle -- the same as Window > Redraw Window."""
import subprocess
import sys

import pytest

DRIVER = '''
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad import main
from belfryscad.window.main_window import MainWindow
main.REDRAW_SETTLE_MS = 50
w = MainWindow(); w.skip_unsaved_prompts = True; w.show()
hidden = MainWindow(); hidden.skip_unsaved_prompts = True
calls = []
MainWindow._redraw_window = lambda self: calls.append(self)
timer = main._redraw_after_display_changes(app)
screen = app.primaryScreen()
for _ in range(3):
    app.screenRemoved.emit(screen); app.screenAdded.emit(screen)
from PySide6.QtCore import QEventLoop, QTimer
loop = QEventLoop(); QTimer.singleShot(300, loop.quit); loop.exec()
print(len(calls), calls[0] is w if calls else None, flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only (#703)")
def test_display_burst_redraws_each_visible_window_once():
    out = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.split()[-2:] == ["1", "True"], out.stdout + out.stderr
