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

# A second run reuses the same rows rather than swapping fresh items in --
# destroying an item the view still points at dangles its current-item
# pointer, and a later setCurrentItem segfaults.
pane._tree.setCurrentItem(more.child(0))
w._run_tests()
pump(lambda: not pane.is_running())
out["file_rows_after_rerun"] = tree.topLevelItemCount()
again = {tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())}
out["row_names_after_rerun"] = sorted(again)
pane._tree.setCurrentItem(tree.topLevelItem(0).child(0))   # must not crash
out["survived_reselect"] = True

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
    assert out["file_rows_after_rerun"] == 2, "a re-run reuses rows, it does not duplicate them"
    assert out["row_names_after_rerun"] == out["row_names"]
    assert out["survived_reselect"]

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


# Double-click a TEST to edit it; Add Test… writes a new one with the same
# dialog; New File… makes a .scadtest. Double-clicking a FILE does nothing.
EDIT_DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QDialog
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
from belfryscad.window import test_editor
from belfryscad.scadtest import parse_scadtest_file
# A modal QMessageBox blocks forever offscreen, and the duplicate-name case
# below raises one on purpose.
from PySide6.QtWidgets import QMessageBox
_boxes = []
QMessageBox.warning = staticmethod(lambda *a, **k: _boxes.append(a[-1] if a else ""))
QMessageBox.critical = staticmethod(lambda *a, **k: _boxes.append(a[-1] if a else ""))
out = {}
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp()
open(os.path.join(d, "lib.scad"), "w").write("module used() { cube(1); }\\n")
suite = os.path.join(d, "t.scadtest")
open(suite, "w").write(
    "# a comment that must survive\\n[config]\\ntimeout = 30\\n\\n"
    '[[test]]\\nname = "alpha"\\nscript = \\'\\'\\'\\ninclude <lib.scad>\\nused();\\n\\'\\'\\'\\n\\n'
    '[[test]]\\nname = "beta"\\nscript = \\'\\'\\'\\ninclude <lib.scad>\\nused();\\n\\'\\'\\'\\n')

def pump(pred, timeout=60):
    t0 = time.time()
    while not pred() and time.time() - t0 < timeout:
        app.processEvents(); time.sleep(0.01)
    return pred()

w._open_testing_pane(directory=d)
tree = w._testing_pane._tree
out["rows_before_running"] = tree.topLevelItemCount()      # listed without a run

# Double-clicking the FILE row does nothing.
fired = []
w._testing_pane.edit_requested.connect(lambda p, n: fired.append((p, n)))
tree.itemDoubleClicked.emit(tree.topLevelItem(0), 0)
app.processEvents()
out["file_row_inert"] = fired == []

w._run_tests()
pump(lambda: not w._testing_pane.is_running())
file_item = tree.topLevelItem(0)
beta = next(file_item.child(i) for i in range(file_item.childCount())
            if file_item.child(i).text(0) == "beta")

# Edit beta: rename it and change the script, accepting the dialog headlessly.
def fake_exec(self):
    self._name.setText("beta_renamed")
    self._script.setPlainText("include <lib.scad>\\nused();\\nused();")
    self._timeout.setValue(99)
    self._add_var_row("size", "10")
    self._add_var_row("label", '"cap"')
    self._accept()
    return QDialog.DialogCode.Accepted if self.result_test() else QDialog.DialogCode.Rejected
test_editor.TestEditDialog.exec = fake_exec
tree.itemDoubleClicked.emit(beta, 0)
app.processEvents()
text = open(suite).read()
out["comment_survived"] = "a comment that must survive" in text
out["config_survived"] = "timeout = 30" in text
tests = {t.name: t for t in parse_scadtest_file(suite)}
out["names_after_edit"] = sorted(tests)
out["renamed_timeout"] = tests["beta_renamed"].timeout
out["renamed_vars"] = tests["beta_renamed"].set_vars
out["renamed_script_lines"] = tests["beta_renamed"].script.strip().splitlines()
out["alpha_untouched"] = tests["alpha"].script.strip().splitlines()

# Add Test… on the same file.
def add_exec(self):
    self._name.setText("gamma")
    self._script.setPlainText("include <lib.scad>\\nused();")
    self._accept()
    return QDialog.DialogCode.Accepted if self.result_test() else QDialog.DialogCode.Rejected
test_editor.TestEditDialog.exec = add_exec
tree.setCurrentItem(tree.topLevelItem(0))
w._testing_pane._request_add()
app.processEvents()
out["names_after_add"] = sorted(t.name for t in parse_scadtest_file(suite))

# Delete: only enabled for a TEST row, confirmed, and it removes just that
# one block.
tree.setCurrentItem(tree.topLevelItem(0))          # a file row
out["delete_off_for_file_row"] = not w._testing_pane._delete_btn.isEnabled()
file_item = tree.topLevelItem(0)
alpha = next(file_item.child(i) for i in range(file_item.childCount())
             if file_item.child(i).text(0) == "alpha")
tree.setCurrentItem(alpha)
out["delete_on_for_test_row"] = w._testing_pane._delete_btn.isEnabled()

answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
QMessageBox.question = staticmethod(lambda *a, **k: answers.pop(0))
w._testing_pane._request_delete()                  # answered No
app.processEvents()
out["names_after_refusing"] = sorted(t.name for t in parse_scadtest_file(suite))
tree.setCurrentItem(alpha)
w._testing_pane._request_delete()                  # answered Yes
app.processEvents()
out["names_after_deleting"] = sorted(t.name for t in parse_scadtest_file(suite))
out["comment_still_there"] = "a comment that must survive" in open(suite).read()

# A duplicate name is refused rather than silently overwriting.
dlg = test_editor.TestEditDialog(None, existing_names=["alpha"])
dlg._name.setText("alpha"); dlg._script.setPlainText("x();")
dlg._result = None
try:
    dlg._accept()
except Exception:
    pass
out["duplicate_refused"] = dlg.result_test() is None
out["duplicate_complained"] = any("already a test" in str(b) for b in _boxes)
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_test_editor_round_trips_through_the_file():
    proc = subprocess.run([sys.executable, "-c", EDIT_DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=240)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["rows_before_running"] == 1, "suites are listed before any run, so Add Test has a target"
    assert out["file_row_inert"], "double-clicking a file row does nothing"
    # Splicing one block leaves the rest of the file alone.
    assert out["comment_survived"] and out["config_survived"]
    assert out["alpha_untouched"] == ["include <lib.scad>", "used();"]
    assert out["names_after_edit"] == ["alpha", "beta_renamed"], "a rename replaces, not duplicates"
    assert out["renamed_timeout"] == 99
    assert out["renamed_vars"] == {"size": 10, "label": "cap"}
    assert out["renamed_script_lines"] == ["include <lib.scad>", "used();", "used();"]
    assert out["names_after_add"] == ["alpha", "beta_renamed", "gamma"]
    assert out["delete_off_for_file_row"] and out["delete_on_for_test_row"]
    assert out["names_after_refusing"] == ["alpha", "beta_renamed", "gamma"], "No means no"
    assert out["names_after_deleting"] == ["beta_renamed", "gamma"]
    assert out["comment_still_there"], "deleting a block leaves the rest of the file alone"
    assert out["duplicate_refused"] and out["duplicate_complained"]


# The variable-overrides table: + / −, TOML values, and the blank-row cases.
VARS_DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
_boxes = []
QMessageBox.warning = staticmethod(lambda *a, **k: _boxes.append(a[-1] if a else ""))
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.test_editor import TestEditDialog
from belfryscad.scadtest import TestCase
out = {}

# An existing test's vars arrive as rows, values written back as TOML.
tc = TestCase(name="t", script="x();", set_vars={"size": 10, "label": "cap", "flags": [1, 2]})
dlg = TestEditDialog(tc)
out["rows"] = dlg._vars.rowCount()
out["cells"] = [[dlg._vars.item(r, 0).text(), dlg._vars.item(r, 1).text()]
                for r in range(dlg._vars.rowCount())]

# "+" appends an empty row; a wholly blank row is ignored, not an error.
dlg._add_var_row("", "")
out["rows_after_plus"] = dlg._vars.rowCount()
out["collected_ignoring_blank"] = dlg._collect_vars()

# "−" removes the selected row.
dlg._vars.setCurrentCell(0, 0)
dlg._remove_var_row()
out["rows_after_minus"] = dlg._vars.rowCount()
out["collected_after_minus"] = dlg._collect_vars()

# A name with no value, and a value with no name, are both refused.
def collect_error(name, value):
    d = TestEditDialog(TestCase(name="t", script="x();"))
    d._add_var_row(name, value)
    try:
        d._collect_vars()
        return None
    except ValueError as e:
        return str(e)
out["name_without_value"] = collect_error("size", "")
out["value_without_name"] = collect_error("", "10")

# Bad TOML is reported rather than written.
d = TestEditDialog(TestCase(name="t", script="x();"))
d._add_var_row("size", "not valid toml!!")
d._name.setText("t"); d._script.setPlainText("x();")
d._accept()
out["bad_toml_refused"] = d.result_test() is None
out["bad_toml_complained"] = any("not valid TOML" in str(b) for b in _boxes)
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_variable_overrides_table():
    proc = subprocess.run([sys.executable, "-c", VARS_DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["rows"] == 3
    assert out["cells"] == [["size", "10"], ["label", '"cap"'], ["flags", "[1, 2]"]]
    assert out["rows_after_plus"] == 4
    # A blank row left over from clicking "+" is not an error.
    assert out["collected_ignoring_blank"] == {"size": 10, "label": "cap", "flags": [1, 2]}
    assert out["rows_after_minus"] == 3
    assert out["collected_after_minus"] == {"label": "cap", "flags": [1, 2]}
    assert "no value" in out["name_without_value"]
    assert "no variable name" in out["value_without_name"]
    assert out["bad_toml_refused"] and out["bad_toml_complained"]


TABS_DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.test_editor import TestEditDialog
from belfryscad.scadtest import TestCase
out = {}
tc = TestCase(name="t", script="x();", timeout=30,
              set_vars={"size": 10}, assert_echoes=["ready"], assert_no_warnings=False)
dlg = TestEditDialog(tc)
dlg.show()
for _ in range(25): app.processEvents(); time.sleep(0.01)
out["tabs"] = [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())]
# Name is outside the tabs -- it identifies what you are editing.
out["name_outside_tabs"] = not dlg._tabs.isAncestorOf(dlg._name)
out["source_tab_has"] = [dlg._tabs.widget(0).isAncestorOf(w)
                         for w in (dlg._source, dlg._timeout, dlg._script)]
out["overrides_tab_has_table"] = dlg._tabs.widget(1).isAncestorOf(dlg._vars)
out["results_tab_has"] = [dlg._tabs.widget(2).isAncestorOf(w)
                          for w in (dlg._echoes, dlg._warnings, dlg._expect_success)]
# Every field still round-trips, from whichever tab it now lives on.
dlg._accept()
r = dlg.result_test()
out["round_trip"] = [r.name, r.script, r.timeout, r.set_vars,
                     r.assert_echoes, r.assert_no_warnings]
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_editor_is_tabbed_and_still_round_trips():
    proc = subprocess.run([sys.executable, "-c", TABS_DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["tabs"] == ["Source", "Overrides", "Results"]
    assert out["name_outside_tabs"]
    assert all(out["source_tab_has"]), "Source, Timeout and the script share the Source tab"
    assert out["overrides_tab_has_table"]
    assert all(out["results_tab_has"])
    # Splitting the dialog across tabs must not lose a field on the way out.
    assert out["round_trip"] == ["t", "x();", 30, {"size": 10}, ["ready"], False]
