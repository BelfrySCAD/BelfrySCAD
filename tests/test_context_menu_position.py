"""The editor's context menu acts on where you clicked, not where the caret is.

Reported on Windows (#467, #466): right-clicking a comment block offered no
Reflow Comment, and no Reformat Selection either. macOS moves the caret to a
right-click and Windows does not, so `self.textCursor()` was the clicked line
on one platform and some other line on the other -- which is why this was
invisible here.
"""
import json
import os
import subprocess
import sys

import pytest

DRIVER = r'''
import json, os, sys, tempfile
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor, QContextMenuEvent
from PySide6.QtCore import QTimer, QEventLoop
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor

SRC = ("wall = 3;\n"
       "// Description:\n"
       "//   Makes a widget of the given size, with a hole in it.\n"
       "//   Second body line.\n"
       "cube(10);\n")

ed = CodeEditor(); ed.resize(900, 400); ed.setPlainText(SRC); ed.show()
out = {}

def menu_at(caret, click, sel=None):
    doc = ed.document()
    c = ed.textCursor()
    if sel:
        c.setPosition(SRC.index(sel[0]))
        c.setPosition(SRC.index(sel[1]) + len(sel[1]), QTextCursor.MoveMode.KeepAnchor)
    else:
        c.setPosition(SRC.index(caret))
    ed.setTextCursor(c)
    blk = doc.findBlock(SRC.index(click))
    pos = ed.cursorRect(QTextCursor(blk)).center()
    got = []
    def shoot():
        m = QApplication.activePopupWidget()
        got.extend(a.text() for a in m.actions() if a.text())
        m.close()
    QTimer.singleShot(120, shoot)
    ed.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                          ed.mapToGlobal(pos)))
    loop = QEventLoop(); QTimer.singleShot(400, loop.quit); loop.exec()
    return got

out["caret_away"] = menu_at("wall = 3", "//   Makes a widget")
out["caret_on"] = menu_at("//   Makes a widget", "//   Makes a widget")
out["in_selection"] = menu_at("", "cube(10)", sel=("cube(10);", "cube(10);"))
out["outside_selection"] = menu_at("", "//   Makes a widget",
                                    sel=("cube(10);", "cube(10);"))
out["on_code"] = menu_at("wall = 3", "cube(10)")

ed.setReadOnly(True)
out["read_only"] = menu_at("//   Makes a widget", "//   Makes a widget")
ed.setReadOnly(False)

print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


@pytest.fixture(scope="module")
def m():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True,
                          text=True, timeout=180,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def has(items, word):
    return any(word in i for i in items)


def test_reflow_is_offered_where_you_clicked_not_where_the_caret_is(m):
    """The reported bug. Windows leaves the caret alone on a right-click."""
    assert has(m["caret_away"], "Reflow"), m["caret_away"]


def test_it_still_works_when_they_coincide(m):
    assert has(m["caret_on"], "Reflow")


def test_right_clicking_inside_a_selection_offers_to_reformat_it(m):
    """One item, not two: the plain "Reformat Selection" and the
    "Reformat Selection As" submenu beside it did the same thing (#502)."""
    assert [i for i in m["in_selection"] if "Reformat" in i] == \
        ["Reformat Selection"], m["in_selection"]


def test_right_clicking_outside_a_selection_acts_on_the_click(m):
    """It used to offer Reformat Selection for a selection somewhere else
    entirely, which would have rewritten a span you could not see."""
    assert not has(m["outside_selection"], "Reformat"), m["outside_selection"]
    assert has(m["outside_selection"], "Reflow"), m["outside_selection"]


def test_a_code_line_offers_neither(m):
    assert not has(m["on_code"], "Reflow")
    assert not has(m["on_code"], "Reformat")


def test_a_read_only_buffer_says_so(m):
    """It silently loses Use Library, Reflow, Reformat and Edit as..., and
    nothing said why -- a library file opens read-only, so this is what a
    BOSL2 developer sees all day."""
    assert any(i == "Read Only Buffer" for i in m["read_only"]), m["read_only"]
    assert not has(m["read_only"], "Reflow")


def test_an_editable_buffer_does_not_carry_the_hint(m):
    for key in ("caret_away", "caret_on", "on_code"):
        assert not any(i == "Read Only Buffer" for i in m[key]), key
