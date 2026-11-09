"""File ▸ Play Demo Script… and Record Demo Script (#1082)."""
import json
import subprocess
import sys

import pytest

import random

from belfryscad.window.demo_player import DemoScriptError, parse, text_steps


def test_parse():
    assert parse(
        "@new\n@rate 20 // fast\ncube(1);\n\n@@x\n@pause key\n@pause 0.5\n"
        "@preview\n@render\n@view Front\n@rotate 30 20 over 1.5s\n@zoom 2\n@zoom 0.5 over 2\n"
    ) == [
        ("new",), ("rate", 20.0), ("type", "cube(1);\n"), ("type", "\n"), ("type", "@x\n"),
        ("pause", None), ("pause", 0.5), ("render", True), ("render", False),
        ("view", "front"), ("rotate", 30.0, 20.0, 1500), ("zoom", 2.0, 500), ("zoom", 0.5, 2000),
    ]


def test_parse_editing_directives():
    assert parse("@type  a // b\n@goto 3 5\n@delete 4\n@speed 1.5\n") == [
        ("type", " a // b"), ("goto", 3, 5), ("delete", 4), ("speed", 1.5)]


@pytest.mark.parametrize("bad", ["@bogus", "@rate", "@rate 0", "@view sideways", "@pause soon", "@zoom -1",
                                 "@speed 0", "@goto 0 1", "@goto 1", "@delete 0"])
def test_parse_rejects(bad):
    with pytest.raises(DemoScriptError, match="Line 2"):
        parse("ok\n" + bad)


DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEvent, QEventLoop, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
renders = []
MainWindow._render = lambda self, *a, **k: renders.append(k.get("preview", True))
w = MainWindow(); w.show(); app.processEvents()
d = tempfile.mkdtemp()
p = os.path.join(d, "t.demo")
open(p, "w").write("@new\\n@rate 200\\ncube(1);\\n@pause key\\n@preview\\n@zoom 2 over 0.1s\\n@view top\\nsphere(2);\\n")
w._play_demo(p)
dist0 = w._viewport._renderer.camera.distance
def key(k):
    app.sendEvent(w._current_editor(), QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
run(300)
from PySide6.QtTest import QTest
for k in (Qt.Key.Key_F5, Qt.Key.Key_B, Qt.Key.Key_Period):  # a remote's other buttons
    QTest.keyClick(w._current_editor(), k)
run(50)
waiting = w._current_editor().toPlainText()
key(Qt.Key.Key_Space)          # @pause key continues
run(40)
key(Qt.Key.Key_Space)          # pause mid-demo
paused_at = w._current_editor().toPlainText()
run(300)
still = w._current_editor().toPlainText() == paused_at
key(Qt.Key.Key_Space)          # resume
run(1000)
out = {"tabs": w._tabs.count(), "waiting": waiting, "still": still,
       "text": w._current_editor().toPlainText(), "renders": renders,
       "zoomed": round(dist0 / w._viewport._renderer.camera.distance, 3),
       "done": w._demo_player is None}
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_play_demo():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out == {"tabs": 2, "waiting": "cube(1);\n", "still": True,
                   "text": "cube(1);\nsphere(2);\n", "renders": [True], "zoomed": 2.0,
                   "done": True}


def _apply(text, cursor, steps):
    """What the player does to the text, without Qt."""
    for step in steps:
        if step[0] == "type":
            text = text[:cursor] + step[1] + text[cursor:]
            cursor += len(step[1])
        elif step[0] == "goto":
            lines = text.split("\n")
            line = min(step[1], len(lines)) - 1
            cursor = sum(len(l) + 1 for l in lines[:line]) + min(step[2] - 1, len(lines[line]))
        elif step[0] == "delete":
            text = text[:cursor] + text[cursor + step[1]:]
    return text, cursor


def test_text_steps_simple_cases():
    assert text_steps("", "cube(1);\nsph", 0) == (["cube(1);", "@type sph"], 12)
    assert text_steps("a\n", "a\n@x\n", 2) == (["@@x"], 5)
    assert text_steps("cube(1);\n", "cube(2);\n", 9) == (["@goto 1 6", "@delete 1", "@type 2"], 6)
    assert text_steps("same", "same", 4) == ([], 4)


def test_recorded_edits_replay_to_the_same_text():
    rng = random.Random(1082)
    alphabet = "ab @\n/"
    text, cursor, played, pc = "", 0, "", 0
    for _ in range(300):
        i = rng.randint(0, len(text))
        j = min(len(text), i + rng.choice([0, 0, 1, 3]))
        new = text[:i] + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 4))) + text[j:]
        lines, cursor = text_steps(text, new, cursor)
        played, pc = _apply(played, pc, parse("\n".join(lines) + "\n") if lines else [])
        assert (played, pc) == (new, cursor)
        text = new


REC_DRIVER = '''
import json, os, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window import demo_player
from belfryscad.window.main_window import MainWindow
demo_player.PAUSE_MIN_S = 0.2
demo_player.SETTLE_MS = 50
w = MainWindow(); w.show(); app.processEvents()
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
w._record_demo()
ed = w._current_editor()
ed.textCursor().insertText("cube(1);\\n")
w._demo_recorder.note_render(True)
run(300)                                   # a pause
ed.textCursor().insertText("sphere(2);")
w._viewport.set_view_preset("top")
run(500)
cam = w._viewport._renderer.camera
cam.azimuth = 100.0; cam.elevation = 10.0; w._viewport.camera_changed.emit()
run(200)
w._record_demo()                           # stop: the script opens in a tab
script = w._current_editor().toPlainText()
renders = []
MainWindow._render = lambda self, *a, **k: renders.append(k.get("preview", True))
w._play_demo()                             # plays the tab in front
w._demo_player.set_speed(4)
t0 = time.monotonic()
while w._demo_player is not None and time.monotonic() - t0 < 20:
    run(50)
print("RESULT" + json.dumps({"script": script, "text": w._current_editor().toPlainText(),
                             "renders": renders, "az": round(cam.azimuth), "el": round(cam.elevation),
                             "tabs": w._tabs.count(), "secs": round(time.monotonic() - t0, 1)}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_record_then_play():
    r = subprocess.run([sys.executable, "-c", REC_DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    lines = out["script"].splitlines()
    assert lines[0] == "@new" and lines[2:4] == ["cube(1);", "@preview"], out["script"]
    assert lines[4].startswith("@pause "), out["script"]
    assert "@type sphere(2);" in lines and "@view top" in lines, out["script"]
    assert lines[-1].startswith("@rotate 100 10 over"), out["script"]
    # recorded tab, script tab, and the tab the demo typed into
    assert out["text"] == "cube(1);\nsphere(2);" and out["renders"] == [True], out
    assert (out["az"], out["el"], out["tabs"]) == (100, 10, 4), out
    assert out["secs"] < 5, out                # 4x: the 0.5 s pause and the typing are quick
