#!/usr/bin/env python3
"""The proposal diff is legible, in either theme.

Measures WCAG contrast between each line's text and the colour it is
actually drawn on -- read back from the rendered document, not from the
HTML source, because Qt's rich-text engine drops the alpha in
`rgba(...)`. A "20% tint" rendered as the accent at full strength, which
is how dark green text ended up on saturated green at about 2:1.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication, QTextBrowser  # noqa: E402

AA = 4.5           # WCAG AA for body text
failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def _lum(c):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c.red()) + 0.7152 * f(c.green()) + 0.0722 * f(c.blue())


def contrast(fg: QColor, bg: QColor) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


DIFF = ("--- a/part.scad\n"
        "+++ b/part.scad\n"
        "@@ -1,3 +1,4 @@\n"
        " include <BOSL2/std.scad>\n"
        "+include <BOSL2/nurbs.scad>\n"
        "-old_line();\n"
        " cube(10);\n")


def measure(base_hex):
    """Every (character, its colour, the colour behind it) in the render.

    Read back from the laid-out document rather than the HTML, since the
    point is what Qt actually paints. Where a run sets no colour of its
    own, the widget's own text/background show through -- which in a real
    dark theme are light-on-dark together, so the harness pairs them the
    same way.
    """
    from belfryscad.window.ai_chat import diff_to_html
    base = QColor(base_hex)
    default_fg = QColor("#FFFFFF") if base.lightness() < 128 else QColor("#000000")
    view = QTextBrowser()
    view.setHtml(diff_to_html(DIFF, base))
    doc = view.document()
    seen = []
    cur = QTextCursor(doc)
    for i in range(1, doc.characterCount()):
        cur.setPosition(i)
        ch = doc.characterAt(i - 1)
        if not ch.strip():
            continue
        f = cur.charFormat()
        fg = (f.foreground().color()
              if f.foreground().style() != Qt.BrushStyle.NoBrush else default_fg)
        bg = (f.background().color()
              if f.background().style() != Qt.BrushStyle.NoBrush else base)
        seen.append((ch, fg, bg))
    return seen


def main():
    QApplication.instance() or QApplication(sys.argv)

    for label, bases in (("light", ["#FFFFFF", "#F6F8FA", "#ECECEC"]),
                         ("dark", ["#1E1E1E", "#2B2B2B", "#000000", "#323232"])):
        for base in bases:
            rows = measure(base)
            check(f"{label} {base}: the diff rendered at all", len(rows) > 20,
                  f"{len(rows)} characters")
            worst = min(((contrast(fg, bg), fg.name(), bg.name())
                         for _, fg, bg in rows), default=(0, "", ""))
            check(f"{label} {base}: every character clears {AA}:1",
                  worst[0] >= AA,
                  f"worst {worst[0]:.2f}:1 -- {worst[1]} on {worst[2]}")

    # The specific regression: the tint must not be painted at full strength.
    rows = measure("#FFFFFF")
    tints = {bg.name().lower() for _, _, bg in rows}
    for accent in ("#2ea043", "#cf222e", "#54aeff"):
        check(f"the {accent} accent is a tint, not a solid band",
              accent not in tints, str(sorted(tints)))

    # Added and removed must stay distinguishable from each other, or the
    # colour is carrying no information.
    for base in ("#FFFFFF", "#1E1E1E"):
        rows = measure(base)
        check(f"{base}: added and removed lines differ in colour",
              len({bg.name() for _, _, bg in rows}) >= 3,
              str(sorted({bg.name() for _, _, bg in rows})))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
