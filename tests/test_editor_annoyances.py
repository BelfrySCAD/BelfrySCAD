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

# --- #417: search is relative to the cursor ---------------------------
ed4 = CodeEditor()
ed4.setPlainText("Example one\\nExample two\\nExample three\\nExample four")
text4 = ed4.toPlainText()
starts = [i for i in range(len(text4)) if text4.startswith("Example", i)]
bar4 = FindBar(ed4)
bar4.show()
c = ed4.textCursor(); c.setPosition(0); ed4.setTextCursor(c)

# Typing the search text letter by letter must not walk down the file.
seen = []
for n in range(1, len("Example") + 1):
    bar4._find_input.setText("Example"[:n])
    seen.append((ed4.textCursor().position(), bar4._current))
out["typing_cursor_positions"] = sorted({p for p, _ in seen})
out["typing_current"] = sorted({i for _, i in seen})

# Enter steps forward, one match at a time, leaving a caret (no selection).
bar4._find_next()
out["next1"] = [bar4._current,
                ed4.textCursor().position() == starts[0] + len("Example"),
                ed4.textCursor().hasSelection()]
bar4._find_next()
out["next2_current"] = bar4._current

# Click somewhere else: Next and Prev both work from THERE.
c = ed4.textCursor(); c.setPosition(starts[3] - 2); ed4.setTextCursor(c)
bar4._find_next()
out["next_after_click"] = bar4._current          # the 4th match
c = ed4.textCursor(); c.setPosition(starts[3] - 2); ed4.setTextCursor(c)
bar4._find_prev()
out["prev_after_click"] = bar4._current          # the 3rd match

# Typing in the document replaces nothing: the caret is a caret.
c = ed4.textCursor(); c.setPosition(starts[1] + len("Example")); ed4.setTextCursor(c)
ed4.insertPlainText("!")
out["doc_second_line"] = ed4.toPlainText().split("\\n")[1]

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


def test_find_as_you_type_does_not_walk_down_the_file(driven):
    """#417: every keystroke jumped to the next hit and left the cursor at
    the end of it, so the following keystroke searched on from there --
    three letters in, you were three occurrences down the file."""
    assert driven["typing_cursor_positions"] == [0]      # cursor never moved
    assert driven["typing_current"] == [0]               # always the first match


def test_find_next_and_prev_run_from_the_cursor(driven):
    """#417: they stepped from the last match found, so scrolling away and
    clicking somewhere else did not change where an arrow took you."""
    current, caret_after_match, has_selection = driven["next1"]
    assert current == 0
    assert caret_after_match          # caret sits at the end of the match...
    assert not has_selection          # ...as a caret, not the match selected
    assert driven["next2_current"] == 1

    assert driven["next_after_click"] == 3
    assert driven["prev_after_click"] == 2
    assert driven["doc_second_line"] == "Example! two"
