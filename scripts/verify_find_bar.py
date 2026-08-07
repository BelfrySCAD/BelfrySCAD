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

from PySide6.QtCore import Qt  # noqa: E402
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
    # Inspect the rendered pixels, not the property. A QFont with
    # setUnderline(True) stores fine and draws nothing on macOS's native
    # button style, so font().underline() would pass on an icon that is
    # visibly wrong -- which is exactly what shipped first.
    icon = bar._btn_word.icon()
    check("whole-word button carries an icon", not icon.isNull())
    if not icon.isNull():
        img = icon.pixmap(bar._btn_word.iconSize()).toImage()
        w, h = img.width(), img.height()
        rows = []
        for y in range(h):
            n = sum(1 for x in range(w) if img.pixelColor(x, y).alpha() > 40)
            rows.append(n)
        ink = [y for y, n in enumerate(rows) if n > 0]
        check("the icon has visible ink", bool(ink), f"row counts {rows}")
        if ink:
            # Split the ink into bands separated by blank rows. A correct
            # icon has exactly two: the glyphs, then the rule under them.
            # Comparing single rows does not work -- the pen is 1.2px and
            # covers two rows, so half the rule reads as a "glyph row".
            bands = [[ink[0]]]
            for y in ink[1:]:
                if y == bands[-1][-1] + 1:
                    bands[-1].append(y)
                else:
                    bands.append([y])
            check("glyphs and rule are separate bands", len(bands) == 2, f"bands {bands}")
            if len(bands) == 2:
                glyphs, rule = bands
                check("the rule sits below the glyphs", rule[0] > glyphs[-1], f"{bands}")
                check("the rule is wider than any glyph row",
                      max(rows[y] for y in rule) > max(rows[y] for y in glyphs),
                      f"rule {[rows[y] for y in rule]} vs glyphs {[rows[y] for y in glyphs]}")
    check("the whole-word button has no stray text label",
          bar._btn_word.text() == "", repr(bar._btn_word.text()))

    # No check here for the disclosure button's checked-state background.
    # macOS paints that through the native Aqua style, which an offscreen
    # QWidget.render() does not reproduce -- a pixel check written for it
    # passed just as happily with the suppressing stylesheet removed, so it
    # would have asserted nothing. Verified on a real screen instead.

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

    # --- replace buttons ----------------------------------------------
    ed.show_find(replace=True)
    app.processEvents()
    for name in ("_btn_replace", "_btn_replace_all"):
        btn = getattr(bar, name)
        check(f"{name} has an icon and no text label",
              not btn.icon().isNull() and btn.text() == "", repr(btn.text()))
        check(f"{name} has a tooltip naming what it does", bool(btn.toolTip()))
        img = btn.icon().pixmap(btn.iconSize()).toImage()
        ink = sum(1 for y in range(img.height()) for x in range(img.width())
                  if img.pixelColor(x, y).alpha() > 60)
        check(f"{name} icon actually draws something", ink > 20, f"{ink} pixels")
        # Nothing may touch the edge, or it reads as clipped.
        edge = any(img.pixelColor(x, y).alpha() > 60
                   for x in range(img.width()) for y in (0, img.height() - 1))
        check(f"{name} icon is not clipped at the top or bottom edge", not edge)

    # The reported bug: at a narrow width the labels were cut mid-word
    # ("eplace Al"). _reposition_find_bar clamps the bar's geometry to the
    # editor's width, and the bar's layout then squeezes its children --
    # text buttons went 85->79 and 116->78. It has to go through show() and
    # that method; resizing the editor alone does not re-lay-out the bar,
    # and a check written that way passes even on the broken version.
    ed.resize(900, 400)
    ed.show()
    ed._reposition_find_bar()
    app.processEvents()
    wide = [getattr(bar, n).width() for n in ("_btn_replace", "_btn_replace_all")]
    ed.resize(300, 400)
    ed._reposition_find_bar()
    app.processEvents()
    narrow = [getattr(bar, n).width() for n in ("_btn_replace", "_btn_replace_all")]
    check("replace buttons keep their width in a narrow editor",
          narrow == wide, f"{wide} -> {narrow}")

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
