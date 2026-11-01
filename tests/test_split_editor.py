"""View ▸ Split: several views of one file (#506).

Qt widgets, so it runs in a subprocess with the offscreen platform -- the
same pattern as test_bookmarks.py.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QSplitter
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
w.resize(1200, 800); w.show(); app.processEvents()
tab = w._current_tab(); p = tab.editor
p.setPlainText("\\n".join(f"line{i:02d} = {i};" for i in range(40)) + "\\nmodule m() {\\n  cube(1);\\n}\\n")
out = {}

def focus(v):
    v.setFocus(); app.processEvents()
    tab.views.active = v; w._on_view_focused(tab, v)
def goto(v, line, col=0):
    c = v.textCursor(); c.setPosition(v.document().findBlockByNumber(line).position() + col); v.setTextCursor(c)
def same():
    return all(v.toPlainText() == p.toPlainText() for v in tab.views)

focus(p)
s = w._split_view(Qt.Orientation.Horizontal) or tab.views[-1]
out["two_views"] = len(tab.views) == 2 and tab.views.active is s
out["split_starts_equal"] = same()
out["close_enabled_on_split"] = w._act_close_split.isEnabled()

# Typing in the split reaches the primary, which keeps its scroll.
p.verticalScrollBar().setValue(10); scroll = p.verticalScrollBar().value()
focus(s); goto(s, 20, 0)
QTest.keyClicks(s, "abc")
out["typed_synced"] = same() and p.document().findBlockByNumber(20).text().startswith("abc")
out["primary_scroll_kept"] = p.verticalScrollBar().value() == scroll

# Undo after typing in the split: text back everywhere, caret in the split.
w._undo_group.undo()
out["undo_synced"] = same() and p.document().findBlockByNumber(20).text() == "line20 = 20;"
out["undo_caret_in_split"] = s.textCursor().blockNumber() == 20
w._undo_group.redo()

# A select-all replace and a setPlainText on the primary reach the split.
c = QTextCursor(s.document()); c.select(QTextCursor.SelectionType.Document); c.insertText(s.toPlainText() + "x = 1;\\n")
out["select_all_synced"] = same()
p.setPlainText(p.toPlainText().replace("line05", "LINE05"))
out["set_plain_synced"] = same()
focus(p); goto(p, 2, 0); c = p.textCursor(); c.insertText("\\u00e9\\U0001F600"); p.setTextCursor(c)
out["emoji_synced"] = same()

# Typing in the primary, then in the split, is two undo steps.
stack = tab.undo_stack
focus(p); goto(p, 3, 0); QTest.keyClicks(p, "P")
focus(s); goto(s, 30, 0); QTest.keyClicks(s, "S")
n = stack.index()
w._undo_group.undo()
out["two_steps"] = "S" not in s.document().findBlockByNumber(30).text()[:1] and p.document().findBlockByNumber(3).text().startswith("P")
w._undo_group.redo()

# Breakpoints: toggled in the split, shown in every view and collected.
s.toggle_breakpoint(12)
out["bp_all_views"] = all(12 in v.breakpoint_lines() for v in tab.views)
focus(p); goto(p, 0, 0); QTest.keyClicks(p, "y = 2;"); QTest.keyClick(p, Qt.Key.Key_Return)
out["bp_moved_everywhere"] = all(v.breakpoint_lines() == {13} for v in tab.views)
s.toggle_breakpoint(13)
out["bp_cleared_everywhere"] = all(not v.breakpoint_lines() for v in tab.views)

# The paused line: highlighted in every view, only the active one moves.
focus(s); goto(p, 0, 0); goto(s, 0, 0)
p.set_execution_line(35)
out["exec_all_views"] = all(len(v._exec_selection) == 1 and v._exec_selection[0].cursor.blockNumber() == 34 for v in tab.views)
out["exec_caret_active_only"] = s.textCursor().blockNumber() == 34 and p.textCursor().blockNumber() == 0
p.clear_execution_line()
out["exec_cleared"] = all(not v._exec_selection for v in tab.views)

# Highlights: an error and a viewport selection appear in every view.
p.set_error_location(8, 1)
out["error_all_views"] = all(len(v._error_selections) == 1 for v in tab.views)
p.set_selection(5, 9)
out["selection_all_views"] = all(len(v._selection_extra) == 1 for v in tab.views)

# Find in the split highlights its matches in the primary too.
s.show_find(); s._find_bar._find_input.setText("line1"); app.processEvents()
out["find_all_views"] = len(s._find_selections) > 0 and len(p._find_selections) == len(s._find_selections)
s._find_bar.close_bar()

# Folding in the split leaves the primary's lines visible.
fold_line = next(i for i in range(p.document().blockCount()) if p.document().findBlockByNumber(i).text().startswith("module"))
s.toggle_fold_at(fold_line); app.processEvents()
out["fold_per_view"] = (not s.document().findBlockByNumber(fold_line + 1).isVisible()) and p.document().findBlockByNumber(fold_line + 1).isVisible()

# Read-only, as an Edit-as dialog's lock sets it, covers every view.
s.setReadOnly(True)
out["read_only_all"] = all(v.isReadOnly() for v in tab.views)
s.setReadOnly(False)

# Nested: split the split below, then close the middle view.
focus(s)
b = w._split_view(Qt.Orientation.Vertical) or tab.views[-1]
out["three_views"] = len(tab.views) == 3 and isinstance(b.parentWidget(), QSplitter) and b.parentWidget().orientation() == Qt.Orientation.Vertical
focus(s); w._close_split()
out["closed_middle"] = len(tab.views) == 2 and s not in tab.views
out["collapsed"] = isinstance(b.parentWidget(), QSplitter) and b.parentWidget().orientation() == Qt.Orientation.Horizontal
focus(p); w._close_split()
out["primary_stays"] = len(tab.views) == 2 and not w._act_close_split.isEnabled()
focus(b); w._close_split(); app.processEvents()
out["back_to_one"] = len(tab.views) == 1 and p.parentWidget() is tab and tab.views.active is p
focus(p); QTest.keyClicks(p, "z")
out["edits_after_close"] = p.toPlainText().startswith("z")
print(json.dumps(out)); sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_split_editor():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    failed = [k for k, v in out.items() if v is not True]
    assert not failed, (failed, proc.stderr[-2000:])
