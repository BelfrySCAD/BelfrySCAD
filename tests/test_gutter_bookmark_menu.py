"""Right-clicking the editor's line numbers toggles a bookmark on that line.

Qt widgets, so it runs in a subprocess with the offscreen platform, like
test_bookmarks.py.
"""
import json
import os
import subprocess
import sys

DRIVER = r"""
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
ed = w._current_tab().editor
ed.setPlainText("\n".join(f"line{i:02d} = {i};" for i in range(20)))
app.processEvents()
gutter = ed._line_number_area
out = {}

def y_of(line):
    block = ed.document().findBlockByNumber(line)
    return int(ed.blockBoundingGeometry(block).translated(ed.contentOffset()).top()) + 2

def menu_texts(line):
    m = gutter.context_menu(y_of(line))
    return [a.text() for a in m.actions()], m

out["cursor_before"] = ed.textCursor().blockNumber()
texts, m = menu_texts(5)
out["first"] = texts
m.actions()[0].trigger()
out["after_add"] = ed.bookmark_lines()
out["cursor_after"] = ed.textCursor().blockNumber()          # the menu does not move the cursor
texts, m = menu_texts(5)
out["second"] = texts
m.actions()[0].trigger()
out["after_remove"] = ed.bookmark_lines()
out["below_text"] = gutter.context_menu(y_of(19) + 10000) is None

def press(button, x, line):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, y_of(line)), QPointF(x, y_of(line)),
                     button, button, Qt.KeyboardModifier.NoModifier)
    gutter.mousePressEvent(ev)

press(Qt.MouseButton.RightButton, 5, 7)                      # breakpoint column, right button
out["bp_after_right"] = sorted(ed.breakpoint_lines())
press(Qt.MouseButton.LeftButton, 5, 7)
out["bp_after_left"] = sorted(ed.breakpoint_lines())
print(json.dumps(out), flush=True)
os._exit(0)
"""


def test_the_gutter_menu_toggles_a_bookmark_on_the_clicked_line():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["first"] == ["Add Bookmark"]
    assert out["after_add"] == [5]
    assert out["cursor_after"] == out["cursor_before"]
    assert out["second"] == ["Remove Bookmark"]
    assert out["after_remove"] == []
    assert out["below_text"] is True
    # A right-click opens the menu; it must not also toggle a breakpoint.
    assert sorted(out["bp_after_right"]) == []
    assert sorted(out["bp_after_left"]) == [7]
