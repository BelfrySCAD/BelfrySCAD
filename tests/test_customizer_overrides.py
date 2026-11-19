"""#1146: a Customizer value is an override, as in OpenSCAD -- applied at
render, never written into the source unless Write to Source is pressed.

Qt widgets, so it runs in a subprocess (offscreen)."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
import belfryscad.window.main_window as mw
SRC = "size = 10; // [1:50]\nh = size * 2;\necho(size=size, h=h);\ncube(size);\n"
d = tempfile.mkdtemp(); p = os.path.join(d, "box.scad"); open(p, "w").write(SRC)
w = mw.MainWindow(); w.skip_unsaved_prompts = True
pane = w._customizer_pane
w.open_file_by_path(p, render=False)
tab = w._current_tab()

def wait(cond, secs=60):
    end = time.time() + secs
    while not cond() and time.time() < end:
        app.processEvents(); time.sleep(0.02)

def render():
    w._console.clear(); w._render()
    wait(lambda: "ECHO:" in w._console.toPlainText())
    return [l for l in w._console.toPlainText().splitlines() if l.startswith(("ECHO", "WARNING"))]

out = {}
pane.set_parameter("size", 20)
out["set"] = [tab.editor.toPlainText() == SRC, tab.is_modified, dict(tab.customizer_overrides)]
out["render"] = render()
pane.set_parameter("size", 10)                       # the source's own value again
out["back_to_default"] = dict(tab.customizer_overrides)
pane.set_parameter("size", 25); pane._on_reset()
out["reset"] = dict(tab.customizer_overrides)
pane._apply_preset_values({"size": 15})
out["preset"] = dict(tab.customizer_overrides)
w._act_new.trigger(); app.processEvents()             # another tab has its own
out["other_tab"] = dict(w._current_tab().customizer_overrides)
w._tabs.setCurrentWidget(tab); app.processEvents()
out["back"] = dict(tab.customizer_overrides)
pane._on_write_to_source(); app.processEvents()
out["written"] = [tab.editor.toPlainText().splitlines()[0], dict(tab.customizer_overrides)]
print("RESULT" + json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_customizer_values_are_overrides_until_written():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                       env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=180)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr[-3000:]
    out = json.loads(line[6:])
    assert out["set"] == [True, False, {"size": 20}], out          # source and modified mark untouched
    assert out["render"] == ["ECHO: size = 20, h = 40"], out         # applied, and no "overwritten" warning
    assert out["back_to_default"] == {}, out
    assert out["reset"] == {}, out
    assert out["preset"] == {"size": 15}, out
    assert out["other_tab"] == {} and out["back"] == {"size": 15}, out
    assert out["written"] == ["size = 15; // [1:50]", {}], out
