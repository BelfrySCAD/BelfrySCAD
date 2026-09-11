"""The Testing pane: Design > Run Tests…, the results tree, and the coverage
overlay it drives.

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
# Two suites, in a subdirectory too, so find_test_files' recursion is exercised.
open(os.path.join(d, "test_f.scadtest"), "w").write(
    '[[test]]\\nname = "test_f"\\nscript = """\\ninclude <lib.scad>\\nassert(f(-1) == 2);\\n"""\\n')
os.mkdir(os.path.join(d, "more"))
# run_test resolves include paths relative to the .scadtest file's own
# directory (runner.run(lines, tc.script_dir)), so a suite one level down
# reaches the library with ../.
open(os.path.join(d, "more", "test_used.scadtest"), "w").write(
    '[[test]]\\nname = "test_used"\\nscript = """\\ninclude <../lib.scad>\\nused();\\n"""\\n\\n'
    '[[test]]\\nname = "test_fails"\\nscript = """\\ninclude <../lib.scad>\\nassert(f(1) == 99);\\n"""\\n')

def pump(pred, timeout=120):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.01)
    return pred()

w.open_file_by_path(main)             # renders on open
tab = w._current_tab()
pump(lambda: not w._render_busy())
# A render never collects coverage any more -- there is no way to ask it to.
out["no_coverage_from_render"] = w._coverage is None

w._open_testing_pane(directory=d)     # Design > Run Tests…
pane = w._testing_pane
out["dock_shown"] = not w._testing_dock.isHidden()
out["dir_label"] = pane._dir_label.toolTip()

logged = []
w.log = lambda m: logged.append(m)
w._run_tests()
pump(lambda: not pane.is_running() and w._coverage is not None)
out["coverage_arrived"] = w._coverage is not None

# The tree: one top-level row per .scadtest file, expandable to its tests.
tree = pane._tree
out["file_rows"] = tree.topLevelItemCount()
rows = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
out["row_names"] = sorted(rows)
more = rows[os.path.join("more", "test_used.scadtest")]
out["more_counts"] = more.text(1)
out["more_pct"] = more.text(2)
out["more_has_cov"] = more.text(3).endswith("%")
out["more_children"] = sorted((more.child(i).text(0), more.child(i).text(1)) for i in range(more.childCount()))
out["failing_file_expanded"] = more.isExpanded()
out["totals"] = pane._total_label.text()

# Overlay: a library tab lights up from its real path.
w.open_file_by_path(lib)
libtab = w._current_tab()
pump(lambda: not w._render_busy())
out["lib_tab_selections"] = len(libtab.editor._coverage_selections)
out["lib_has_uncovered"] = any(s["hits"] == 0 for s in w._coverage_spans_for_tab(libtab))
colors = {s.format.background().color().name() for s in libtab.editor._coverage_selections}
out["two_tints"] = len(colors) == 2      # covered green + unused() never called, red

pane._overlay_box.setChecked(False)      # the pane owns the toggle now
out["hidden_when_toggled_off"] = len(libtab.editor._coverage_selections)
pane._overlay_box.setChecked(True)
out["back_when_toggled_on"] = len(libtab.editor._coverage_selections) > 0

libtab.editor.setReadOnly(False)
c = libtab.editor.textCursor(); c.setPosition(0); libtab.editor.setTextCursor(c)
libtab.editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_X, Qt.KeyboardModifier.NoModifier, "X"))
out["cleared_by_edit"] = len(libtab.editor._coverage_selections) == 0

# ../lib.scad from more/ and lib.scad from the root are ONE file: merge_spans
# normalises the origin, so this is one entry, not two half-covered ones.
out["origins"] = sorted({os.path.basename(o) for (o, *_rest) in w._coverage.spans})
out["distinct_origin_paths"] = len({o for (o, *_rest) in w._coverage.spans})
by = {(s["line"], s["kind"]): s["hits"] for s in w._coverage.spans.values()
      if os.path.basename(s["origin"]) == "lib.scad"}
out["false_arm_hit_by_test"] = by.get((1, "branch"), None)
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_run_tests_pane_reports_results_and_drives_the_overlay():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=240)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["no_coverage_from_render"], "a plain render must not produce coverage"
    assert out["dock_shown"], "Run Tests… shows the pane"
    assert "2 test files" in out["dir_label"], out["dir_label"]
    assert out["coverage_arrived"]

    # One row per file, recursed into subdirectories, named relative to the root.
    assert out["file_rows"] == 2
    assert out["row_names"] == [os.path.join("more", "test_used.scadtest"), "test_f.scadtest"]
    assert out["more_counts"] == "1/2" and out["more_pct"] == "50.0%"
    assert out["more_has_cov"]
    assert out["more_children"] == [["test_fails", "FAIL"], ["test_used", "pass"]]
    assert out["failing_file_expanded"], "a file with a failure opens itself"
    assert "2 of 3 tests passed (66.7%)" in out["totals"] and "coverage" in out["totals"]

    # The overlay, driven by the pane's checkbox.
    assert out["lib_tab_selections"] > 0 and out["lib_has_uncovered"] and out["two_tints"]
    assert out["hidden_when_toggled_off"] == 0
    assert out["back_when_toggled_on"]
    assert out["cleared_by_edit"]

    assert out["origins"] == ["lib.scad"]        # the tests' own snippets are dropped
    assert out["distinct_origin_paths"] == 1, \
        "lib.scad and ../lib.scad are the same file; merge_spans normalises the origin"
    assert out["false_arm_hit_by_test"] == 1     # f(-1) took the arm main.scad never does


# The tints sat a few characters right of the code they described, further
# and further down the file, and nested spans stacked into a muddy third
# colour. OpenSCAD source is UTF-8, so an em dash in a comment is enough.
UTF8_DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
lib = os.path.join(d, "lib.scad")
# An em dash (3 bytes) and an accented letter (2 bytes) before any code:
# byte offsets run 3 characters ahead of document positions from here down.
open(lib, "w", encoding="utf-8").write(
    "// caf\\u00e9 \\u2014 a comment\\n"
    "module outer() {\\n"
    "    cube(1);\\n"
    "    if (false) { sphere(2); }\\n"
    "}\\n")
open(os.path.join(d, "t.scadtest"), "w").write(
    '[[test]]\\nname = "t"\\nscript = """\\ninclude <lib.scad>\\nouter();\\n"""\\n')

def pump(pred, timeout=120):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.01)
    return pred()

w._open_testing_pane(directory=d)
w._run_tests()
pump(lambda: not w._testing_pane.is_running() and w._coverage is not None)
w.open_file_by_path(lib)
tab = w._current_tab()
pump(lambda: not w._render_busy())
text = tab.editor.toPlainText()
sels = tab.editor._coverage_selections
out["n_selections"] = len(sels)

# Every selection must be disjoint from the others -- nesting is flattened.
ranges = sorted((min(s.cursor.position(), s.cursor.anchor()),
                 max(s.cursor.position(), s.cursor.anchor())) for s in sels)
out["disjoint"] = all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:]))
# Whitespace between statements is untinted: no selection may start or end
# on it, and none may be blank.
out["no_edge_whitespace"] = all(
    text[a:b].strip() and not text[a].isspace() and not text[b - 1].isspace()
    for a, b in ranges)
# One selection per line: indentation and blank lines are never tinted, so
# no selection may span a line break.
out["no_newline_inside"] = all("\\n" not in text[a:b] for a, b in ranges)

# And they must land on real code, not three characters to its right.
covered_text = "".join(text[a:b] for a, b in ranges)

# The invariant the mapping exists for: the characters a mapped span covers
# are the same text as the bytes the evaluator reported.
raw = open(lib, "rb").read()
m = w._coverage_offset_map(tab)
out["n_spans"] = 0
out["all_spans_map_exactly"] = True
for s in w._coverage_spans_for_tab(tab):
    a, b = int(s["start"]), int(s["end"])
    ca, cb = (m(a), m(b)) if m is not None else (a, b)
    out["n_spans"] += 1
    if raw[a:b].decode("utf-8") != text[ca:cb]:
        out["all_spans_map_exactly"] = False
        out.setdefault("first_mismatch", [raw[a:b].decode("utf-8"), text[ca:cb]])
out["map_was_needed"] = m is not None

out["sphere_is_uncovered"] = False
for s in sels:
    a, b = sorted((s.cursor.position(), s.cursor.anchor()))
    frag = text[a:b]
    if "sphere" in frag:
        # red == the uncovered format; alpha 70 red vs alpha 55 green
        out["sphere_is_uncovered"] = s.format.background().color().red() > 200
out["cube_present"] = "cube(1);" in covered_text
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_overlay_lands_on_the_code_and_does_not_stack():
    proc = subprocess.run([sys.executable, "-c", UTF8_DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=240)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["n_selections"] > 0
    assert out["disjoint"], "nested spans must be flattened, not stacked"
    assert out["no_edge_whitespace"], "whitespace between statements stays untinted"
    assert out["no_newline_inside"], "a selection spanning a line break tints the indent column"
    assert out["cube_present"], "a tint must cover the code it describes, not the text beside it"
    assert out["map_was_needed"], "the fixture is meant to be non-ASCII"
    assert out["n_spans"] > 3
    assert out["all_spans_map_exactly"], out.get("first_mismatch")
    assert out["sphere_is_uncovered"], "the never-taken if body is red"


# Double-click in the results tree: a file row opens the .scadtest, a test
# row opens it and scrolls to that test's [[test]] block.
OPEN_DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
open(os.path.join(d, "lib.scad"), "w").write("module used() { cube(1); }\\n")
suite = os.path.join(d, "t.scadtest")
open(suite, "w").write(
    '[config]\\nname = "decoy"\\n\\n'
    '[[test]]\\nname = "alpha"\\nscript = """\\ninclude <lib.scad>\\nused();\\n"""\\n\\n'
    '[[test]]\\nname = "beta"\\nscript = """\\ninclude <lib.scad>\\nused();\\n"""\\n')

def pump(pred, timeout=120):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.01)
    return pred()

logged = []
w.log = lambda m: logged.append(m)
w._open_testing_pane(directory=d)
w._run_tests()
pump(lambda: not w._testing_pane.is_running() and w._coverage is not None)

tree = w._testing_pane._tree
file_item = tree.topLevelItem(0)
tree.itemDoubleClicked.emit(file_item, 0)          # double-click the FILE row
app.processEvents()
tab = w._current_tab()
out["opened_path"] = os.path.basename(tab.file_path or "")
out["is_the_toml"] = tab.editor.toPlainText().startswith("[config]")
# A .scadtest is TOML: it must not be rendered as OpenSCAD. A parse error
# does NOT go through w.log -- it reaches the console widget directly and
# squiggles the editor -- so watch the squiggle. The render is threaded, so
# give a failure real time to arrive instead of checking on the next
# event-loop turn and always seeing a clean editor.
pump(lambda: tab.editor._error_selections, timeout=3)
out["no_parse_error"] = not tab.editor._error_selections
out["never_parsed"] = getattr(tab, "_last_parse_path", None) is None
out["tabs_after_file"] = w._tabs.count()

def cursor_line():
    c = w._current_editor().textCursor()
    return c.blockNumber() + 1

beta = None
for i in range(file_item.childCount()):
    if file_item.child(i).text(0) == "beta":
        beta = file_item.child(i)
tree.itemDoubleClicked.emit(beta, 0)               # double-click the TEST row
app.processEvents()
out["beta_line"] = cursor_line()
out["beta_line_text"] = w._current_editor().toPlainText().splitlines()[cursor_line() - 1].strip()
out["tabs_after_test"] = w._tabs.count()           # reuses the tab, does not stack

alpha = file_item.child(0)
tree.itemDoubleClicked.emit(alpha, 0)
app.processEvents()
out["alpha_line_text"] = w._current_editor().toPlainText().splitlines()[cursor_line() - 1].strip()
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_double_click_opens_the_test_file_and_scrolls_to_the_test():
    proc = subprocess.run([sys.executable, "-c", OPEN_DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=240)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["opened_path"] == "t.scadtest" and out["is_the_toml"]
    assert out["no_parse_error"] and out["never_parsed"], "TOML must not be rendered as OpenSCAD"
    # Both test rows land on their own [[test]] header, not on [config]'s name.
    assert out["beta_line_text"] == "[[test]]"
    assert out["alpha_line_text"] == "[[test]]"
    assert out["beta_line"] > 4, "beta is the second block, not the first"
    assert out["tabs_after_test"] == out["tabs_after_file"], "one tab, reused"
