"""Coverage in the GUI: capture, overlay, toggle, and Run Tests with Coverage.

MainWindow is a widget, so this drives it in a subprocess with scratch
settings, like the other GUI tests.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import Qt, QEvent
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
lib = os.path.join(d, "lib.scad")
open(lib, "w").write("function f(x) = x > 0 ? 1 : 2;\\nmodule used() { cube(1); }\\nmodule unused() { sphere(1); }\\n")
main = os.path.join(d, "main.scad")
open(main, "w").write("include <lib.scad>\\na = f(1);\\nif (a == 2) cube(1); else used();\\n")

def pump(pred, timeout=60):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.01)
    return pred()

w._act_capture_coverage.setChecked(False)
w.open_file_by_path(main)             # renders on open, without coverage
tab = w._current_tab()
pump(lambda: not w._render_busy())
out["no_coverage_after_plain_render"] = w._coverage is None
out["show_unchecked_initially"] = not w._act_show_coverage.isChecked()

w._render(tab, coverage=True)         # Design > Render with Coverage
pump(lambda: w._coverage is not None)
out["coverage_arrived"] = w._coverage is not None
out["show_auto_checked"] = w._act_show_coverage.isChecked()
sels = tab.editor._coverage_selections
out["main_tab_selections"] = len(sels)
colors = {s.format.background().color().name() for s in sels}
out["two_tints"] = len(colors) == 2   # covered green + the uncovered cube arm red

w.open_file_by_path(lib)              # a library tab lights up from its real path
libtab = w._current_tab()
pump(lambda: not w._render_busy())
out["lib_tab_selections"] = len(libtab.editor._coverage_selections)
out["lib_has_uncovered"] = any(s["hits"] == 0 for s in w._coverage_spans_for_tab(libtab))

w._act_show_coverage.setChecked(False)
out["hidden_when_toggled_off"] = (len(tab.editor._coverage_selections), len(libtab.editor._coverage_selections))
w._act_show_coverage.setChecked(True)
out["back_when_toggled_on"] = len(libtab.editor._coverage_selections) > 0
libtab.editor.setReadOnly(False)
c = libtab.editor.textCursor(); c.setPosition(0); libtab.editor.setTextCursor(c)
libtab.editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_X, Qt.KeyboardModifier.NoModifier, "X"))
out["cleared_by_edit"] = len(libtab.editor._coverage_selections) == 0

# Run Tests with Coverage: replaces the report with the tests' merged one.
test = os.path.join(d, "test_lib.scadtest")
open(test, "w").write('[[test]]\\nname = "test_f"\\nscript = """\\ninclude <lib.scad>\\nassert(f(-1) == 2);\\n"""\\n')
logged = []
w.log = lambda m: logged.append(m)
w._coverage = None
w._run_tests_with_coverage(files=[test])
pump(lambda: w._coverage is not None)
out["tests_ran"] = any("1 of 1 tests passed" in m for m in logged)
origins = {os.path.basename(o) for (o, *_rest) in w._coverage.spans}
out["test_origins"] = sorted(origins)
by = {(s["line"], s["kind"]): s["hits"] for s in w._coverage.spans.values() if os.path.basename(s["origin"]) == "lib.scad"}
out["false_arm_hit_by_test"] = by.get((1, "branch"), None)
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_coverage_capture_overlay_toggle_and_test_run():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["no_coverage_after_plain_render"] and out["show_unchecked_initially"]
    assert out["coverage_arrived"] and out["show_auto_checked"]
    assert out["main_tab_selections"] > 0 and out["two_tints"]
    assert out["lib_tab_selections"] > 0 and out["lib_has_uncovered"]
    assert out["hidden_when_toggled_off"] == [0, 0]   # JSON turns the tuple into a list
    assert out["back_when_toggled_on"]
    assert out["cleared_by_edit"]
    assert out["tests_ran"]
    assert out["test_origins"] == ["lib.scad"]          # the snippet itself is dropped
    assert out["false_arm_hit_by_test"] == 1          # f(-1) took the arm the render never did
