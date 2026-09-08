"""Three reported editor annoyances (#376, #377, #378).

The shortcut list is plain data and is tested directly. The two key-event
behaviours need a real CodeEditor, and widget instantiation crashes the
pytest runner here, so they are driven through a throwaway script in a
subprocess -- the same shape test_editor_indent.py uses.
"""
import json
import subprocess
import sys

import pytest


def test_redo_is_bound_to_both_conventions():
    """#377: Ctrl+Shift+Z did nothing on Windows.

    setShortcut() binds only the FIRST of a standard key's per-platform
    bindings, and Windows lists Ctrl+Y first.
    """
    from PySide6.QtGui import QKeySequence
    from belfryscad.window.main_window import redo_shortcuts

    keys = {k.toString() for k in redo_shortcuts()}
    assert "Ctrl+Shift+Z" in keys       # Cmd+Shift+Z on macOS
    assert "Ctrl+Y" in keys
    # Whatever the platform itself considers Redo is still in there.
    for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Redo):
        assert k.toString() in keys


DRIVER = '''
import json, sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import Qt, QEvent

app = QApplication([])
from belfryscad.window.editor import CodeEditor, FindBar

out = {}

def key(widget, k, mods=Qt.KeyboardModifier.NoModifier, text=""):
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, text))

# --- #378: down arrow at the last line --------------------------------
ed = CodeEditor()
ed.setPlainText("one\\ntwo\\nthree")
cur = ed.textCursor()
cur.movePosition(cur.MoveOperation.End)
ed.setTextCursor(cur)

# Default (preference off): stops, document untouched.
key(ed, Qt.Key.Key_Down)
out["default_text"] = ed.toPlainText()

# Preference on: the old convenience is still available.
ed.set_append_line_on_down(True)
key(ed, Qt.Key.Key_Down)
out["opt_in_text"] = ed.toPlainText()

# ... but never while extending a selection.
ed2 = CodeEditor()
ed2.set_append_line_on_down(True)
ed2.setPlainText("one\\ntwo\\nthree")
c = ed2.textCursor()
c.movePosition(c.MoveOperation.Start)
c.movePosition(c.MoveOperation.Down, c.MoveMode.KeepAnchor)
c.movePosition(c.MoveOperation.End, c.MoveMode.KeepAnchor)
ed2.setTextCursor(c)
key(ed2, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
out["shift_text"] = ed2.toPlainText()
out["shift_kept_selection"] = ed2.textCursor().hasSelection()

# --- #376: typing with the find bar open ------------------------------
ed3 = CodeEditor()
ed3.setPlainText("foo bar\\nfoo baz\\nfoo qux")
bar = FindBar(ed3)
bar.show()
bar._find_input.setText("foo")
bar._on_search_changed()
out["matches"] = len(bar._matches)

# Put the cursor somewhere of the user's choosing and type there.
c = ed3.textCursor()
c.setPosition(len("foo bar\\n") + 4)   # just before "baz"
ed3.setTextCursor(c)
before = ed3.textCursor().position()
ed3.insertPlainText("X")
bar._on_doc_changed()
out["cursor_moved"] = ed3.textCursor().position() != before + 1
out["selection_after_typing"] = ed3.textCursor().hasSelection()
out["text_after_typing"] = ed3.toPlainText()
# Highlights still track the edit.
out["matches_after"] = len(bar._matches)

print(json.dumps(out))
'''


@pytest.fixture(scope="module")
def driven(tmp_path_factory):
    path = tmp_path_factory.mktemp("ed") / "_drv.py"
    path.write_text(DRIVER)
    res = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                          env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                               "HOME": str(path.parent)})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_down_arrow_stops_at_the_end_by_default(driven):
    """#378: the document must not grow while you navigate it."""
    assert driven["default_text"] == "one\ntwo\nthree"


def test_the_append_convenience_is_still_available(driven):
    """Kept as a preference rather than removed -- it was deliberate."""
    assert driven["opt_in_text"] == "one\ntwo\nthree\n"


def test_shift_down_never_edits_and_keeps_the_selection(driven):
    """#378's actual complaint: the inserted newline dropped the selection
    the user was building. True even with the preference ON."""
    assert driven["shift_text"] == "one\ntwo\nthree"
    assert driven["shift_kept_selection"] is True


def test_typing_with_the_find_bar_open_does_not_jump(driven):
    """#376: find-as-you-type re-ran on every document change and selected
    the next match, so the following keystroke replaced it -- typing a word
    silently overwrote a different occurrence per letter."""
    assert driven["matches"] == 3
    assert driven["cursor_moved"] is False
    assert driven["selection_after_typing"] is False
    assert driven["text_after_typing"] == "foo bar\nfoo Xbaz\nfoo qux"


def test_the_find_bar_still_tracks_the_edit(driven):
    """Highlights refresh even though the cursor stays put."""
    assert driven["matches_after"] == 3
