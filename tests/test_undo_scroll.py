"""Undo and redo leave the scroll alone when the cursor is already on
screen (issue #531).

The logic was already there, added for #389: `_TextEditCmd._set_cursor`
asks whether the cursor is visible and only recentres when it is not. It
never worked. `setPlainText()` replaces the whole document and Qt resets
the scrollbars to the top when it does, so by the time the visibility test
ran the viewport had already jumped, the answer was always "not visible",
and every undo recentred -- which is exactly what the reporter described:
"usually positions the cursor in the middle, forcing me to re-orient
myself".

Driven in a subprocess, as the other Qt tests here are: these need a real
QApplication and a shown widget with a working viewport, and conftest's
session fixture is a QGuiApplication -- creating a QApplication on top of
it takes the whole pytest process down.
"""
import json
import os
import subprocess
import sys

import pytest


_DRIVER = '''
import json, sys
from PySide6.QtWidgets import QApplication, QPlainTextEdit
app = QApplication([])

from belfryscad.window.main_window import _replace_text_keeping_scroll

LONG = "\\n".join("line %d" % i for i in range(500))

def fresh():
    ed = QPlainTextEdit(); ed.setPlainText(LONG); ed.resize(400, 200); ed.show()
    app.processEvents()
    return ed

out = {}

# 1. The premise: does setPlainText really reset the scroll?
ed = fresh(); bar = ed.verticalScrollBar()
bar.setValue(bar.maximum() // 2); mid = bar.value()
ed.setPlainText(LONG)
out["premise_mid"] = mid
out["premise_after_plain"] = bar.value()

# 2. The fix keeps it.
ed = fresh(); bar = ed.verticalScrollBar()
bar.setValue(bar.maximum() // 2); mid = bar.value()
_replace_text_keeping_scroll(ed, LONG)
out["kept_mid"] = mid
out["kept_after"] = bar.value()

# 3. Shorter text clamps rather than failing.
ed = fresh(); bar = ed.verticalScrollBar()
bar.setValue(bar.maximum())
_replace_text_keeping_scroll(ed, "one\\ntwo\\nthree")
out["short_value"] = bar.value()
out["short_max"] = bar.maximum()

# 4. What the bug cost: a cursor that was on screen still reads as on
#    screen afterwards, so _set_cursor's test means something.
ed = fresh(); bar = ed.verticalScrollBar()
bar.setValue(bar.maximum() // 2)
cur = ed.textCursor(); cur.setPosition(len(ed.toPlainText()) // 2)
ed.setTextCursor(cur); app.processEvents()
out["visible_before"] = ed.viewport().rect().contains(ed.cursorRect(cur))
pos = cur.position()

# Mirror what _set_cursor actually does: a QTextCursor belongs to the
# document it came from, and setPlainText() replaces that -- so the real
# code takes a FRESH cursor from the editor and positions it. Reusing the
# old handle measures the wrong thing (it reads as position 0).
def visible_at(ed, pos):
    c = ed.textCursor(); c.setPosition(pos)
    return ed.viewport().rect().contains(ed.cursorRect(c))

_replace_text_keeping_scroll(ed, LONG)
app.processEvents()
out["visible_after_fix"] = visible_at(ed, pos)

ed2 = fresh(); bar2 = ed2.verticalScrollBar()
bar2.setValue(bar2.maximum() // 2)
cur2 = ed2.textCursor(); cur2.setPosition(len(ed2.toPlainText()) // 2)
ed2.setTextCursor(cur2); app.processEvents()
ed2.setPlainText(LONG)
app.processEvents()
out["visible_after_plain"] = visible_at(ed2, pos)

print(json.dumps(out))
'''


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    driver = tmp_path_factory.mktemp("undoscroll") / "driver.py"
    driver.write_text(_DRIVER)
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    r = subprocess.run([sys.executable, str(driver)],
                       capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip(f"no Qt available here: {(r.stderr or '').strip()[-200:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_setplaintext_alone_resets_the_scroll(out):
    """The premise. If Qt ever stops doing this, the fix is unnecessary --
    this says so rather than leaving it to be guessed at."""
    assert out["premise_mid"] > 0
    assert out["premise_after_plain"] == 0


def test_the_replacement_keeps_the_scroll(out):
    assert out["kept_after"] == out["kept_mid"]


def test_shorter_text_clamps_instead_of_failing(out):
    """Undoing a paste removes lines, so the saved offset routinely no
    longer exists."""
    assert out["short_value"] == out["short_max"]
    assert out["short_value"] >= 0


def test_an_on_screen_cursor_still_reads_as_on_screen(out):
    """The whole point: `_set_cursor` only recentres when the cursor is off
    screen, and that question is meaningless if the viewport moved first."""
    assert out["visible_before"] is True
    assert out["visible_after_fix"] is True
    # And the bug itself, still reproducible through the raw call.
    assert out["visible_after_plain"] is False
