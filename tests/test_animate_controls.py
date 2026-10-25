"""The animation pane's controls (#922): Play and Pause show which is in
effect, Dump Pictures dumps one cycle then plays on with the box unticked,
as OpenSCAD's does, and playback never holds the wait cursor. Qt widgets:
a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.animate import AnimatePane
from belfryscad.window.main_window import MainWindow

out = {}
a = AnimatePane()
frames = []
a.frame_changed.connect(lambda t: frames.append(round(t, 3)))
buttons = lambda: [a._btn_play.isChecked(), a._btn_pause.isChecked()]
out["idle"] = buttons()
QTest.mouseClick(a._btn_play, Qt.MouseButton.LeftButton)
out["playing"] = buttons()
QTest.mouseClick(a._btn_play, Qt.MouseButton.LeftButton)   # again: stays down
out["play_twice"] = buttons()
QTest.mouseClick(a._btn_pause, Qt.MouseButton.LeftButton)
out["paused"] = buttons()

a._steps = 4
a._dump_check.setChecked(True)
del frames[:]
a.play()
for _ in range(4):           # MainWindow advances a dump after each frame lands
    a.advance_frame()
out["dump_frames"] = frames
out["after_dump"] = {"playing": a.is_playing(), "dumping": a.is_dumping(),
                     "dump_ticked": a._dump_check.isChecked(), "timer": a._timer.isActive()}
a.pause()

w = MainWindow(); w.skip_unsaved_prompts = True
w._set_render_busy(True)
out["cursor_render"] = QApplication.overrideCursor() is not None
w._set_render_busy(False)
w._animate_pane._playing = True
w._set_render_busy(True)
out["cursor_playing"] = QApplication.overrideCursor() is not None
w._set_render_busy(False)
w._animate_pane._playing = False
w._set_render_busy(False)    # a cancel then the render's own end: no stray pop
out["cursor_end"] = QApplication.overrideCursor() is not None
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_animation_controls_show_state_dump_plays_on_and_no_wait_cursor():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["idle"] == [False, True]
    assert out["playing"] == [True, False]
    assert out["play_twice"] == [True, False]
    assert out["paused"] == [False, True]
    assert out["dump_frames"] == [0.0, 0.25, 0.5, 0.75, 0.0]
    assert out["after_dump"] == {"playing": True, "dumping": False, "dump_ticked": False, "timer": True}
    assert out["cursor_render"] is True
    assert out["cursor_playing"] is False
    assert out["cursor_end"] is False
