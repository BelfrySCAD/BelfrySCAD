"""File ▸ Play Demo Script… and Record Demo Script (#1082)."""
import json
import subprocess
import sys

import pytest

import random

from belfryscad.window.demo_player import DemoScriptError, parse, text_steps


def test_parse():
    assert parse(
        "@new\n@rate 20 // fast\ncube(1);\n\n  \n@enter\n@@x\n@pause key\n@pause 0.5\n"
        "@preview\n@render\n@view Front\n@rotate 30 20 over 1.5s\n@zoom 2\n@zoom 0.5 over 2\n@zoom 3 at 1 -2 .5\n"
    ) == [
        ("new",), ("rate", 20.0), ("type", "cube(1);\n"), ("type", "\n"), ("type", "@x\n"),
        ("pause", None), ("pause", 0.5), ("render", True), ("render", False),
        ("view", "front"), ("rotate", 30.0, 20.0, 1500), ("zoom", 2.0, 500, None), ("zoom", 0.5, 2000, None),
        ("zoom", 3.0, 500, (1.0, -2.0, 0.5)),
    ]


def test_parse_editing_directives():
    assert parse("@type  a // b\n@goto 3 5\n@delete 4\n@speed 1.5\n@tab 2 // x.scad\n@scroll 40\n"
                 "@scroll 1 over 2s\n@goto start\n@goto END\n") == [
        ("type", " a // b"), ("goto", 3, 5), ("delete", 4), ("speed", 1.5), ("tab", 2),
        ("scroll", 40, 300), ("scroll", 1, 2000), ("goto", 1, 1), ("goto", None, None)]


@pytest.mark.parametrize("bad", ["@bogus", "@rate", "@rate 0", "@view sideways", "@pause soon", "@zoom -1",
                                 "@speed 0", "@goto 0 1", "@goto 1", "@delete 0",
                                 "@tab 0", "@tab", "@scroll 0", "@goto middle", "@zoom 2 at 1 2", "@zoom 2 to 1 2 3"])
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
open(p, "w").write("@new\\n@rate 200\\ncube(1);\\n@pause key\\n@preview\\n@zoom 2 over 0.1s\\n@view top\\n@goto start\\n@type // \\n@goto end\\nsphere(2);\\n")
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
                   "text": "// cube(1);\nsphere(2);\n", "renders": [True], "zoomed": 2.0,
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
    # #1108: a Return is written where it can be seen, not as a blank line
    assert text_steps("a\n", "a\n\n  \nb\n", 2) == (["@enter", "@type   ", "@enter", "b"], 8)


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
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window import demo_player
from belfryscad.window.main_window import MainWindow
demo_player.PAUSE_MIN_S = 0.2
demo_player.SETTLE_MS = 50
w = MainWindow(); w.show(); w.resize(900, 600); app.processEvents()
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
first = w._current_editor()
w._record_demo()                           # records here: no new tab
tabs_recording = w._tabs.count()
first.textCursor().insertText("cube(1);\\n" + "//\\n" * 120)
w._demo_recorder.note_render(True)
run(300)                                   # a pause
w._file_new()
w._current_editor().textCursor().insertText("sphere(2);")
w._viewport.set_view_preset("top")
run(300)
w._tabs.setCurrentIndex(0)                 # back to the first tab
c = first.textCursor(); c.setPosition(3); first.setTextCursor(c)
run(300)
first.verticalScrollBar().setValue(40)     # scrolled by hand
run(300)
cam = w._viewport._renderer.camera
cam.azimuth = 100.0; cam.elevation = 10.0; w._viewport.camera_changed.emit()
run(200)
import numpy as np
cam.distance /= 2; cam.target = np.array([3.0, -1.0, 2.0], dtype=cam.target.dtype)
w._viewport.camera_changed.emit()
run(200)
cam.target = np.array([0.0, 0.0, 0.0], dtype=cam.target.dtype); w._viewport.set_distance(cam.distance * 2)
run(200)
w._record_demo()                           # stop: the script opens in a tab
script = w._current_editor().toPlainText()
first.setPlainText("")
first.verticalScrollBar().setValue(0)
renders = []
MainWindow._render = lambda self, *a, **k: renders.append(k.get("preview", True))
w._play_demo()                             # plays the tab in front
w._demo_player.set_speed(4)
t0 = time.monotonic()
while w._demo_player is not None and time.monotonic() - t0 < 60:
    run(50)
print("RESULT" + json.dumps({"script": script, "tabs_recording": tabs_recording,
                             "first": first.toPlainText()[:12], "cursor": first.textCursor().position(),
                             "scroll": first.verticalScrollBar().value(),
                             "last": w._tabs.widget(w._tabs.count() - 1).editor.toPlainText(),
                             "current": w._tabs.currentIndex(),
                             "renders": renders, "az": round(cam.azimuth), "el": round(cam.elevation),
                             "tabs": w._tabs.count()}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_record_then_play():
    r = subprocess.run([sys.executable, "-c", REC_DRIVER], capture_output=True, text=True, timeout=180)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    lines = [l.split("//")[0].strip() if l.startswith("@") else l for l in out["script"].splitlines()]
    assert out["tabs_recording"] == 1, out
    assert lines[0] == "@tab 1" and lines[4] == "cube(1);" and "@preview" in lines, out["script"]
    assert all(not l.startswith("@mainwin") for l in lines[4:]), out["script"]
    events = [l.split()[0] for l in lines if l.startswith("@") and not l.startswith("@pause")]
    assert events[events.index("@new"):] == ["@new", "@type", "@view", "@tab", "@goto", "@scroll", "@rotate", "@zoom", "@zoom"], out["script"]
    assert any(l.startswith("@zoom 2.00 at 3 -1 2 over") for l in lines), out["script"]
    assert lines[-1].startswith("@zoom 0.50 at 0 0 0 over"), out["script"]
    assert "@goto 1 4" in lines and "@scroll 41" in lines, out["script"]
    # first tab, the new one, the script, and the new one the demo opened
    assert (out["first"], out["cursor"], out["scroll"]) == ("cube(1);\n//\n", 3, 40), out
    assert (out["last"], out["current"], out["tabs"]) == ("sphere(2);", 0, 4), out
    assert out["renders"] == [True] and (out["az"], out["el"]) == (100, 10), out


SAVE_DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QFileDialog
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
asked = []
QFileDialog.getSaveFileName = staticmethod(lambda *a: asked.append(a[2:]) or ("", ""))
w = MainWindow(); w.show(); app.processEvents()
w._save_file_as()                          # an ordinary script
w._record_demo(); w._record_demo()         # a recording opens in a tab
w._save_file_as()
print("RESULT" + json.dumps({"asked": [[os.path.basename(a[0])] + list(a[2:]) for a in asked],
                             "label": w._current_tab().display_name()}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_save_demo_as_opens_on_all_files():
    """#1107: macOS's save panel adds .scad when that filter is selected."""
    r = subprocess.run([sys.executable, "-c", SAVE_DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out["asked"][0][1] == "" and out["asked"][1] == ["demo.demo", "All Files (*)"], out
    assert out["label"].startswith("demo.demo"), out


def test_open_names():
    assert parse('@open "my part.scad"  // the base\n@OPEN sub/b.scad\n@close\n') == [
        ("open", "my part.scad"), ("open", "sub/b.scad"), ("close",)]
    assert parse("@export out/part.stl\n@EXPORT 'a b.3mf'\n") == [
        ("export", "out/part.stl"), ("export", "a b.3mf")]
    for bad in ("@export", "@export part.scad", "@export /tmp/x.stl", "@export notes.txt"):
        with pytest.raises(DemoScriptError, match="Line 1"):
            parse(bad)
    for bad in ("@open", "@open /etc/passwd", "@open ~/x.scad", "@open C:\\x.scad", "@open \\\\srv\\x",
                "@open a\x07.scad"):
        with pytest.raises(DemoScriptError, match="Line 1"):
            parse(bad)


def test_open_path_stays_in_the_scripts_folder(tmp_path):
    import os
    from belfryscad.window.demo_player import open_path
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.scad").write_text("cube(1);\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.scad"
    outside.write_text("secret\n")
    (tmp_path / "link.scad").symlink_to(outside)
    base = str(tmp_path)
    assert open_path(base, "sub/a.scad") == os.path.realpath(tmp_path / "sub" / "a.scad")
    assert open_path(base, "sub/../sub/a.scad") == os.path.realpath(tmp_path / "sub" / "a.scad")
    for name, why in (("../" + outside.name, "outside"), ("link.scad", "outside"),
                      ("sub", "no such file"), ("nope.scad", "no such file"), ("/etc/hosts", "relative")):
        with pytest.raises(DemoScriptError, match=why):
            open_path(base, name)
    with pytest.raises(DemoScriptError, match="save the script first"):
        open_path(None, "sub/a.scad")


def test_merge_lines():
    from belfryscad.window.demo_player import merge_lines
    assert merge_lines(["@goto 1 2", "@goto start", "@scroll 3", "@scroll 9", "@pause 1", "@pause 0.5",
                        "@pause key", "@pause 2", "@type ab", "@type  c", "x", "@type d"]) == [
        "@goto start", "@scroll 9", "@pause 1.5", "@pause key", "@pause 2", "@type ab c", "x", "@type d"]


OPEN_DRIVER = '''
import json, os, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
MainWindow._render = lambda *a, **k: None
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
d = os.path.realpath(tempfile.mkdtemp())
os.mkdir(os.path.join(d, "parts"))
for n in ("start.scad", "parts/gear.scad"):
    open(os.path.join(d, n), "w").write("cube(1);\\n")
w = MainWindow(); w.show(); app.processEvents()
w.open_file_by_path(os.path.join(d, "start.scad"))
w._record_demo()                           # records relative to start.scad's folder
w.open_file_by_path(os.path.join(d, "parts/gear.scad"))
run(100)
w._record_demo()
script = w._current_editor().toPlainText()
p = os.path.join(d, "t.demo")
open(p, "w").write("@open parts/gear.scad\\n@goto end\\n@type !\\n@open ../escape.scad\\n@type never\\n")
w._play_demo(p)
t0 = time.monotonic()
while w._demo_player is not None and time.monotonic() - t0 < 20:
    run(50)
tab = w._current_tab()
print("RESULT" + json.dumps({"script": script, "file": os.path.relpath(tab.file_path, d).replace(os.sep, "/"),
                             "text": tab.editor.toPlainText()}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_open_records_and_plays():
    r = subprocess.run([sys.executable, "-c", OPEN_DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert "@open parts/gear.scad" in out["script"].splitlines(), out["script"]
    assert "@tab" not in out["script"].split("@open", 1)[1], out["script"]
    # opened and typed into, then stopped at the path that leaves the folder
    assert (out["file"], out["text"]) == ("parts/gear.scad", "cube(1);\n!"), out


CLOSE_DRIVER = '''
import json, os, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
MainWindow._render = lambda *a, **k: None
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
d = os.path.realpath(tempfile.mkdtemp())
for n in ("a.scad", "b.scad", "c.scad", "mine.scad"):
    open(os.path.join(d, n), "w").write("cube(1);\\n")
w = MainWindow(); w.show(); app.processEvents()
def names(): return [os.path.basename(w._tabs.widget(i).file_path or "-") for i in range(w._tabs.count())]
for n in ("a.scad", "b.scad", "c.scad"):
    w.open_file_by_path(os.path.join(d, n))
w._record_demo()
w._close_tab(2)                            # the current one, c
w._close_tab(0)                            # another, a; b stays in front
run(100)
w._record_demo()
script = [l.split("//")[0].strip() for l in w._current_editor().toPlainText().splitlines()]
w._close_tab(w._tabs.currentIndex(), confirm=False)
w.open_file_by_path(os.path.join(d, "mine.scad"))
w._current_editor().textCursor().insertText("// mine\\n")        # your own edit
p = os.path.join(d, "t.demo")
open(p, "w").write("@close\\n@open a.scad\\n@type x\\n@close\\n")
w._play_demo(p)
t0 = time.monotonic()
while w._demo_player is not None and time.monotonic() - t0 < 20:
    run(50)
print("RESULT" + json.dumps({"script": script, "tabs": names()}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_close_records_and_plays():
    r = subprocess.run([sys.executable, "-c", CLOSE_DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out["script"][4:] == ["@close", "@tab 1", "@close", "@tab 1"], out["script"]
    # mine.scad, with your edit, stays; a.scad, edited only by the demo, closes
    assert out["tabs"] == ["b.scad", "mine.scad"], out


def test_export_path_stays_in_the_scripts_folder(tmp_path):
    import os
    from belfryscad.window.demo_player import open_path
    (tmp_path / "out").mkdir()
    (tmp_path / "dir.stl").mkdir()
    (tmp_path / "link").symlink_to(tmp_path.parent)
    base = str(tmp_path)
    assert open_path(base, "out/a.stl", "export") == os.path.realpath(tmp_path / "out" / "a.stl")
    for name, why in (("../a.stl", "outside"), ("link/a.stl", "outside"), ("none/a.stl", "no such folder"),
                      ("dir.stl", "not a file"), ("a.scad", "not a format"), ("a.STL.txt", "not a format")):
        with pytest.raises(DemoScriptError, match=why):
            open_path(base, name, "export")


EXPORT_DRIVER = '''
import json, os, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
from belfryscad.window.preferences import load_preference
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
d = os.path.realpath(tempfile.mkdtemp())
open(os.path.join(d, "a.scad"), "w").write("cube(1);\\n")
w = MainWindow(); w.show(); app.processEvents()
w.open_file_by_path(os.path.join(d, "a.scad"))
t0 = time.monotonic()
while w._render_busy() and time.monotonic() - t0 < 30:
    run(50)
p = os.path.join(d, "t.demo")
open(p, "w").write("@export out.stl\\n@export none/x.stl\\n@goto end\\n@type !\\n")
w._play_demo(p)
t0 = time.monotonic()
while w._demo_player is not None and time.monotonic() - t0 < 30:
    run(50)
out = {"stl": os.path.getsize(os.path.join(d, "out.stl")) > 0, "text": w._current_editor().toPlainText(),
       "last_format": load_preference("export/lastFormat", str)}
w._record_demo()
other = os.path.realpath(tempfile.mkdtemp())
w.write_export(os.path.join(d, "rec.3mf"))
w.write_export(os.path.join(other, "far.stl"))
w._record_demo()
out["script"] = w._current_editor().toPlainText().splitlines()[4:]
out["far"] = os.path.join(other, "far.stl")
print("RESULT" + json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_export_plays_and_records():
    r = subprocess.run([sys.executable, "-c", EXPORT_DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    # a refused @export is reported and the demo goes on
    assert (out["stl"], out["text"]) == (True, "cube(1);\n!"), out
    assert out["last_format"] != ".stl", out        # a demo's export isn't remembered
    assert out["script"] == ["@export rec.3mf", f"@export far.stl   // was {out['far']}"], out


def test_pane_and_set_directives():
    assert parse("@show Customizer\n@hide console\n@scroll customizer 40%\n@scroll docs end over 1s\n"
                 "@set width 30 // mm\n@set label \"a // b\"\n@set on true\n@set v [1, 2.5]\n") == [
        ("pane", "customizer", True, None), ("pane", "console", False, None), ("pane_scroll", "customizer", 40.0, 300),
        ("pane_scroll", "docs", 100.0, 1000), ("set", "width", 30), ("set", "label", "a // b"),
        ("set", "on", True), ("set", "v", [1, 2.5])]
    assert parse("@show customizer 300\n@mainwin size 1280 800\n@mainwin Position -20 100\n") == [
        ("pane", "customizer", True, 300), ("mainwin", "size", 1280, 800), ("mainwin", "position", -20, 100)]
    for bad in ("@hide customizer 300", "@show customizer 0", "@mainwin size 0 800", "@mainwin size 1280",
                "@mainwin where 1 2", "@show sidebar", "@scroll customizer 40", "@scroll customizer 140%", "@set width",
                "@set 9x 1", "@set w foo"):
        with pytest.raises(DemoScriptError, match="Line 1"):
            parse(bad)
    from belfryscad.window.demo_player import merge_lines
    assert merge_lines(["@set w 1", "@set w 2", "@set h 3", "@scroll 4", "@scroll customizer 5%",
                        "@scroll customizer 9%"]) == ["@set w 2", "@set h 3", "@scroll 4", "@scroll customizer 9%"]


PANE_DRIVER = '''
import json, os, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
from belfryscad.window.demo_player import pane_scrollbar
MainWindow._render = lambda *a, **k: None
def run(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()
w = MainWindow(); w.resize(1200, 700); w.show(); app.processEvents()
src = "".join(f"p{i} = {i}; // [0:100]\\n" for i in range(40))
w._current_editor().setPlainText(src)
w._customizer_dock.show(); w._console_dock.show(); app.processEvents(); run(300)
pane = w._customizer_pane
w.resize(1300, 760); app.processEvents(); run(150)
w._record_demo()
w._console_dock.hide(); app.processEvents()
pane._widgets["p3"][0].set_value(42); pane._on_widget_changed("p3", 42)
pane._on_widget_changed("p3", 43)
run(100)
w._record_demo()
script = [l.split("//")[0].strip() for l in w._current_editor().toPlainText().splitlines()]
w._close_tab(w._tabs.currentIndex(), confirm=False)
w._console_dock.show()
p = os.path.join(tempfile.mkdtemp(), "t.demo")
open(p, "w").write("@set p7 70\\n@set nope 1\\n@hide console\\n@mainwin size 1400 680\\n"
                   "@show customizer 333\\n@scroll customizer end over 0.1s\\n")
w._play_demo(p)
t0 = time.monotonic()
while w._demo_player is not None and time.monotonic() - t0 < 20:
    run(50)
bar = pane_scrollbar(w._customizer_dock)
print("RESULT" + json.dumps({"script": script, "text": w._current_editor().toPlainText().splitlines()[7],
                             "console_hidden": w._console_dock.isHidden(),
                             "scrolled": bar.maximum() > 0 and bar.value() == bar.maximum(),
                             "size": [w.width(), w.height()], "cust_w": w._customizer_dock.width()}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_panes_and_set_record_and_play():
    r = subprocess.run([sys.executable, "-c", PANE_DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    # the write-back into the editor is not recorded as typing
    # the window as it was when recording began, once, then what was done
    assert out["script"][2] == "@mainwin size 1300 760" and out["script"][3].startswith("@mainwin position"), out
    assert out["script"][4:] == ["@hide console", "@set p3 43"], out
    assert out == {**out, "text": "p7 = 70; // [0:100]", "console_hidden": True, "scrolled": True,
                   "size": [1400, 680]}, out
    assert abs(out["cust_w"] - 333) <= 2, out
