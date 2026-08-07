#!/usr/bin/env python3
"""Find-bar toggles: labels must fit, and Match Whole Word must work.

The width check compares each button's width against the rendered width of
its own label, so it fails for the actual reason the buttons looked wrong
(a flat 22px that fits one glyph but not two) rather than asserting a
magic number.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication  # noqa: E402

from belfryscad.window.editor import CodeEditor  # noqa: E402

TEXT = "foo foobar barfoo foo_bar FOO\nfoo\n"

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    ed = CodeEditor()
    ed.setPlainText(TEXT)
    ed.show_find()
    bar = ed._find_bar
    app.processEvents()

    # --- widths -------------------------------------------------------
    fm = bar.fontMetrics()
    for name in ("_btn_prev", "_btn_next", "_btn_case", "_btn_word", "_btn_regex", "_btn_close"):
        btn = getattr(bar, name, None)
        if btn is None:
            check(f"{name} exists", False)
            continue
        need = fm.horizontalAdvance(btn.text())
        check(f"{name} ({btn.text()!r}) is wide enough for its label",
              btn.width() >= need + 4, f"width {btn.width()} vs label {need}")

    # --- disclosure triangle ------------------------------------------
    check("the whole-word label is 'ab'", bar._btn_word.text() == "ab", repr(bar._btn_word.text()))
    check("the whole-word label is underlined", bar._btn_word.font().underline())
    check("neighbouring buttons are NOT underlined",
          not bar._btn_case.font().underline() and not bar._btn_regex.font().underline())

    check("opened with show_find(): collapsed", not bar._btn_disclose.isChecked())
    check("collapsed hides the replace row", bar._replace_widget.isHidden())
    check("collapsed arrow points right", bar._btn_disclose.text() == "\u25b6", repr(bar._btn_disclose.text()))

    bar._btn_disclose.setChecked(True)
    app.processEvents()
    check("expanding shows the replace row", not bar._replace_widget.isHidden())
    check("expanded arrow points down", bar._btn_disclose.text() == "\u25bc", repr(bar._btn_disclose.text()))

    bar._btn_disclose.setChecked(False)
    app.processEvents()
    check("collapsing hides the replace row again", bar._replace_widget.isHidden())

    # Opening straight into replace must leave the arrow agreeing with it.
    ed.show_find(replace=True)
    app.processEvents()
    check("show_find(replace=True) expands the triangle", bar._btn_disclose.isChecked())
    check("show_find(replace=True) shows the replace row", not bar._replace_widget.isHidden())
    ed.show_find()
    app.processEvents()
    check("reopening as find-only collapses it again",
          not bar._btn_disclose.isChecked() and bar._replace_widget.isHidden())

    # --- Match Whole Word --------------------------------------------
    def count(term, word=False, case=False, regex=False):
        bar._find_input.setText(term)
        bar._btn_word.setChecked(word)
        bar._btn_case.setChecked(case)
        bar._btn_regex.setChecked(regex)
        bar._on_search_changed()
        app.processEvents()
        return len(bar._matches)

    plain = count("foo")
    whole = count("foo", word=True)
    check("without whole-word, substrings count", plain == 6, f"got {plain} (want 6)")
    # 'foo' and 'foo' on line 2 only; foobar/barfoo/foo_bar are not whole
    # words, and FOO is excluded once case matters -- but case is off here,
    # so FOO counts too.
    check("whole-word excludes substring hits", whole == 3, f"got {whole} (want 3: foo, FOO, foo)")

    check("whole-word + case-sensitive drops FOO",
          count("foo", word=True, case=True) == 2, f"got {count('foo', word=True, case=True)}")

    # The alternation case the non-capturing group exists for.
    both = count("foo|bar", word=True, regex=True)
    check("whole-word anchors the whole regex, not just its first branch",
          both == 3, f"got {both} (want 3 -- foo, FOO, foo; no bar is a whole word)")

    # Toggling back off must restore the unrestricted count.
    check("toggling whole-word off restores substring matching",
          count("foo") == 6, "count did not return to 6")

    # An underscore is a word character, so foo_bar must not match.
    check("underscore counts as a word character",
          count("foo_bar", word=True) == 1)

    ed.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
