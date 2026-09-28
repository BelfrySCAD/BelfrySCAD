"""Edit ▸ Toggle Bookmark / Next Bookmark / Previous Bookmark.

Qt widgets, so it runs in a subprocess with the offscreen platform -- the
same pattern as test_editor_trio.py.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QTextCursor
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow
from belfryscad.window.ui_colors import bookmark_pill_colors
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
tab = w._current_tab(); ed = tab.editor
ed.setPlainText("\\n".join(f"line{i:02d} = {i};" for i in range(20)))
out = {}

def goto(line, col=0):
    c = ed.textCursor(); c.setPosition(ed.document().findBlockByNumber(line).position() + col); ed.setTextCursor(c)
def here():
    c = ed.textCursor(); return [c.blockNumber(), c.positionInBlock()]

w._jump_to_bookmark(True)
out["none_msg"] = w.statusBar().currentMessage()

for n in (3, 9, 15):
    goto(n); w._toggle_bookmark()
out["marked"] = ed.bookmark_lines()

goto(5, 4)
w._jump_to_bookmark(True);  out["next"] = here()          # 9, column kept
w._jump_to_bookmark(True);  out["next2"] = here()         # 15
w._jump_to_bookmark(True);  out["wrap"] = here()          # wraps to 3
w._jump_to_bookmark(False); out["prev_wrap"] = here()     # wraps back to 15

goto(9); w._toggle_bookmark(); out["unmarked"] = ed.bookmark_lines()
goto(9); w._toggle_bookmark()

# Two lines inserted above: every mark below follows its text.
goto(0); ed.textCursor().insertText("a = 1;\\nb = 2;\\n")
out["after_insert"] = ed.bookmark_lines()
out["mark_text"] = [ed.document().findBlockByNumber(n).text() for n in ed.bookmark_lines()]

# Deleting a marked line folds its mark into the neighbour, never loses the rest.
c = QTextCursor(ed.document().findBlockByNumber(11))
c.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor); c.removeSelectedText()
out["after_delete"] = ed.bookmark_lines()

# A whole-buffer replace (reload, Convert Tabs) keeps the lines, not line 1.
before = ed.bookmark_lines()
ed.setPlainText(ed.toPlainText().replace("= ", "=\\t"))
out["after_set_plain"] = ed.bookmark_lines() == before
c = ed.textCursor(); c.select(QTextCursor.SelectionType.Document); c.insertText(ed.toPlainText().replace("\\t", " "))
out["after_select_all"] = ed.bookmark_lines() == before

# The pill is painted: its fill appears in the gutter at a marked line only.
w.resize(900, 700); w.show(); app.processEvents()
img = ed._line_number_area.grab().toImage()
fill = QColor(bookmark_pill_colors()[0]).rgb()
lh = ed.fontMetrics().height()
def row_has_fill(line):
    y = round(ed.blockBoundingGeometry(ed.document().findBlockByNumber(line)).translated(ed.contentOffset()).top()) + lh // 2
    return any(img.pixel(x, y) == fill for x in range(img.width()))
out["pill_on_mark"] = row_has_fill(ed.bookmark_lines()[0])
out["pill_off_mark"] = row_has_fill(ed.bookmark_lines()[0] + 1)
print(json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def test_bookmarks():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["none_msg"].startswith("No bookmarks in this tab")
    assert out["marked"] == [3, 9, 15]
    assert out["next"] == [9, 4] and out["next2"] == [15, 4]
    assert out["wrap"] == [3, 4] and out["prev_wrap"] == [15, 4]
    assert out["unmarked"] == [3, 15]
    assert out["after_insert"] == [5, 11, 17]
    assert out["mark_text"] == ["line03 = 3;", "line09 = 9;", "line15 = 15;"]
    assert out["after_delete"] == [5, 11, 16]
    assert out["after_set_plain"] and out["after_select_all"]
    assert out["pill_on_mark"] and not out["pill_off_mark"]
