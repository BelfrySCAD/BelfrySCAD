"""Rewrap a comment block, repeating its own `//` prefix (#467).

Editing BOSL2 documentation means rewrapping by hand otherwise. This is
vim's `gq` with the prefix detection that makes it usable on doc comments.

The lines to rewrap are always SELECTED ones -- see
CodeEditor._reflow_target. Working out the block from the caret's prefix
alone could not tell a docsgen `//   .` spacer or a markdown table from
prose, and rewrapped both into the paragraph.
"""
import json
import os
import subprocess
import sys

import pytest

from belfryscad.window.scad_format import comment_prefix, reflow_comment


# -- What counts as a comment line -----------------------------------------

@pytest.mark.parametrize("line, want", [
    ("// hi", "// "),
    ("//   Makes a widget.", "//   "),
    ("//no space", "//"),
    ("    //   indented body", "    //   "),
    ("/// three slashes", "/// "),
    ("//", "//"),
])
def test_the_prefix_is_captured_verbatim(line, want):
    assert comment_prefix(line) == want


@pytest.mark.parametrize("line", [
    "cube(1); // why",          # a trailing comment: the code is not prose
    "module f() {",
    "",
    "   ",
    "/* block */",
])
def test_a_line_that_is_not_a_whole_line_comment_has_no_prefix(line):
    assert comment_prefix(line) is None


# -- Which lines the block covers ------------------------------------------

DOC = [
    "// Description:",
    "//   Makes a widget of the given size,",
    "//   with an optional hole.",
    "// Arguments:",
    "//   size = The width.",
    "module widget() {}",
]


# -- The rewrap itself -----------------------------------------------------

def test_the_prefix_is_repeated_on_every_wrapped_line():
    lines = ["//   " + "word " * 30]
    out = reflow_comment(lines, 40)
    assert len(out) > 1
    assert all(ln.startswith("//   ") for ln in out), out


def test_every_line_fits_the_width():
    out = reflow_comment(["//   " + "word " * 30], 40)
    assert max(len(ln) for ln in out) <= 40


def test_a_line_uses_the_whole_width_not_width_minus_the_prefix():
    """#507, reported verbatim. textwrap's `width` already covers
    initial_indent, so subtracting the prefix first counted it twice and
    wrapped a `//   ` comment at 95 columns when 100 was asked for."""
    line = ("//   This table shows the results from different combinations of "
            "font size, finite `max_width`, and finite `max_height`:")
    out = reflow_comment([line], 100)
    assert out[0] == ("//   This table shows the results from different combinations of "
                      "font size, finite `max_width`, and")
    assert len(out[0]) == 99


@pytest.mark.parametrize("width", [40, 60, 72, 100])
@pytest.mark.parametrize("prefix", ["//", "//   ", "    //   ", "//!  "])
def test_no_wrapped_line_could_have_taken_the_next_word(width, prefix):
    """The general form of #507: `<= width` is not enough, since a wrap that
    stops early satisfies it too. Every break must be one the next word
    genuinely did not fit through -- which is what the old tests, all of
    them upper bounds, could not see."""
    out = reflow_comment([prefix + " " + "alpha beta gamma delta epsilon " * 6], width)
    assert max(len(ln) for ln in out) <= max(width, len(prefix.rstrip()))
    for here, nxt in zip(out, out[1:]):
        body = nxt[len(comment_prefix(nxt) or ""):].strip()
        if not body:
            continue
        assert len(here) + 1 + len(body.split()[0]) > width, (here, body.split()[0])


def test_short_lines_are_joined_not_left_ragged():
    out = reflow_comment(["//   one", "//   two", "//   three"], 60)
    assert out == ["//   one two three"]


def test_an_indented_comment_keeps_its_indentation():
    out = reflow_comment(["    //   " + "word " * 20], 40)
    assert all(ln.startswith("    //   ") for ln in out), out
    assert max(len(ln) for ln in out) <= 40


def test_a_blank_comment_line_separates_paragraphs():
    """A selection can span one, and the structure has to survive."""
    out = reflow_comment(["//   first para", "//", "//   second para"], 60)
    assert out == ["//   first para", "//", "//   second para"]


def test_a_long_word_is_never_broken():
    """A URL or a some_function() split across lines stops being either."""
    url = "https://github.com/BelfrySCAD/BOSL2/wiki/shapes3d.scad#module-cuboid"
    out = reflow_comment([f"//   see {url} for more"], 40)
    assert any(url in ln for ln in out), out


def test_a_prefix_wider_than_the_target_still_produces_output():
    """Rather than raising out of textwrap on a negative width."""
    out = reflow_comment(["//   " + "word " * 5], 3)
    assert out and all(ln.strip() for ln in out)


def test_nothing_in_gives_nothing_out():
    assert reflow_comment([], 80) == []


def test_already_wrapped_text_is_returned_unchanged():
    """So the editor can skip the edit, and the undo stack stays clean."""
    lines = ["//   one two three"]
    assert reflow_comment(lines, 60) == lines


# -- The editor side, in a subprocess (widgets abort pytest outright) ------

DRIVER = r'''
import json, os, sys, tempfile
from PySide6.QtWidgets import QApplication, QInputDialog
from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor

out = {}
SRC = ("// Description:\n"
       "//   " + "word " * 30 + "\n"
       "//   tail line.\n"
       "// Arguments:\n"
       "cube(1); // trailing\n")

def editor(cols):
    ed = CodeEditor()
    ed.setPlainText(SRC)
    ed._column_guide.set_columns(cols)
    return ed

def at(ed, line, col=4, sel_to=None):
    c = ed.textCursor()
    c.setPosition(ed.document().findBlockByNumber(line).position() + col)
    if sel_to is not None:
        c.setPosition(ed.document().findBlockByNumber(sel_to).position() + col,
                      QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(c)
    return c

def answer(value):
    def go():
        d = QApplication.activeModalWidget()
        out.setdefault("defaults", []).append(d.intValue())
        if value is None: d.reject()
        else: d.setIntValue(value); d.accept()
    QTimer.singleShot(120, go)

ed = editor([67, 100])
out["default_is_widest_guide"] = ed.guide_width()
out["default_with_no_guides"] = editor([]).guide_width()

# Targets: a comment selection reflows; a caret alone, a code line and a
# selection reaching into code all offer nothing.
out["body_target"] = list(ed._reflow_target(at(ed, 1, sel_to=2)))
out["caret_only"] = ed._reflow_target(at(ed, 1))
out["code_target"] = ed._reflow_target(at(ed, 4, 2))
out["selection_over_code"] = ed._reflow_target(at(ed, 1, sel_to=4))

# Reflow at the answered width, twice: the second must default to the first.
answer(50); ed._reflow_comment(*ed._reflow_target(at(ed, 1, sel_to=2)))
out["after_50"] = ed.toPlainText()
ed2 = editor([67, 100]); ed2._last_reflow_width = ed._last_reflow_width
answer(40); ed2._reflow_comment(*ed2._reflow_target(at(ed2, 1, sel_to=2)))
out["after_40"] = ed2.toPlainText()

# Cancel changes nothing.
ed3 = editor([67, 100]); before = ed3.toPlainText()
answer(None); ed3._reflow_comment(*ed3._reflow_target(at(ed3, 1, sel_to=2)))
out["cancel_unchanged"] = ed3.toPlainText() == before

print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


@pytest.fixture(scope="module")
def driven():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True,
                          text=True, timeout=180,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_offered_width_is_the_widest_guide(driven):
    assert driven["default_is_widest_guide"] == 100
    assert driven["default_with_no_guides"] == 80


def test_the_answer_is_remembered_for_the_next_reflow(driven):
    """So reflowing a run of blocks is one keystroke each after the first."""
    assert driven["defaults"][0] == 100, "first offers the guide"
    assert driven["defaults"][1] == 50, "second offers what was answered"


def test_the_selected_comment_lines_are_what_reflows(driven):
    assert driven["body_target"] == [1, 2]


def test_a_caret_with_no_selection_offers_nothing(driven):
    """#467 follow-up. Reflow used to work out the block from the caret's
    prefix, which cannot tell prose from the rest of a `//   ` body: a
    docsgen `//   .` spacer became a stray full stop mid-sentence and a
    markdown table was folded into the paragraph. The author says which
    lines are prose by selecting them."""
    assert driven["caret_only"] is None


def test_a_code_line_offers_no_reflow(driven):
    assert driven["code_target"] is None
    assert driven["selection_over_code"] is None, (
        "a selection reaching into code must not be reflowed")


def test_the_answered_width_is_what_it_wraps_to(driven):
    for key, width in (("after_50", 50), ("after_40", 40)):
        body = [ln for ln in driven[key].splitlines() if ln.startswith("//   ")]
        assert len(body) > 1, driven[key]
        assert max(len(ln) for ln in body) <= width, driven[key]


def test_the_surrounding_block_structure_survives(driven):
    lines = driven["after_50"].splitlines()
    assert lines[0] == "// Description:"
    assert "// Arguments:" in lines
    assert lines[-1] == "cube(1); // trailing"


def test_cancelling_the_dialog_changes_nothing(driven):
    assert driven["cancel_unchanged"]
