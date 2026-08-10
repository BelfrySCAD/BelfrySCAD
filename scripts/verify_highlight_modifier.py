#!/usr/bin/env python3
"""`#` draws its object see-through, in red, the way the reference does.

The bug this covers: a `#` body was drawn solid in the opaque pass and
then given a 0.35-alpha wash, which lands within a shade of the untouched
surface colour -- so `difference() { cylinder(); #cube(); }` showed a
plain solid cube and no sign that anything was highlighted.

Reads pixels back out of the real viewport, since "is it see-through" is
not answerable from the geometry.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def reddish(rgb):
    r, g, b = rgb
    return r > g + 40 and r > 90


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.resize(700, 520)
    w.show()

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if until():
                return True
        return False

    pump(1.0)

    def render(src):
        w._current_tab().editor.setPlainText(src)
        w._render_threadsafe()
        pump(40, lambda: bool(w._geometry_summary()) and not w._render_busy())
        pump(0.6)
        img = w._viewport.grabFramebuffer()
        counts = collections.Counter()
        for y in range(0, img.height(), 2):
            for x in range(0, img.width(), 2):
                c = img.pixelColor(x, y)
                counts[(c.red(), c.green(), c.blue())] += 1
        return counts

    HL = "difference() {\n    cylinder(h=10,d=10);\n    #cube(10);\n}\n"
    PLAIN = "difference() {\n    cylinder(h=10,d=10);\n    cube(10);\n}\n"

    # --- the roles reaching the renderer ---------------------------------
    counts = render(HL)
    roles = [b.role for b in w._bodies]
    check("the evaluator returns the highlighted body alongside the result",
          sorted(roles) == ["highlight", "normal"], str(roles))
    buf_roles = [b.role for b in w._viewport._renderer._buffers]
    check("and both reach the renderer", sorted(buf_roles) == ["highlight", "normal"],
          str(buf_roles))

    # --- what it looks like ----------------------------------------------
    red = sum(n for rgb, n in counts.items() if reddish(rgb))
    check("a highlighted body is drawn in red", red > 200, f"{red} reddish pixels")

    # See-through: the highlighted cube covers the cylinder, and what is
    # behind it has to change the colour, or it is painted solid.
    red_shades = {rgb for rgb in counts if reddish(rgb) and counts[rgb] > 20}
    check("and is see-through, so what is behind it shows through",
          len(red_shades) >= 2, f"only {len(red_shades)} shade(s): {sorted(red_shades)}")

    # Red, not pink: the reference's highlight has green and blue equal,
    # and this drifted well off that while nobody could see it anyway.
    dominant = max((rgb for rgb in counts if reddish(rgb)),
                   key=lambda rgb: counts[rgb])
    check("the highlight is a true red, as the reference's is",
          abs(dominant[1] - dominant[2]) <= 8, f"{dominant} green vs blue")

    # --- and only when asked ---------------------------------------------
    plain = render(PLAIN)
    plain_red = sum(n for rgb, n in plain.items() if reddish(rgb))
    check("the same model without # is not red", plain_red < 200,
          f"{plain_red} reddish pixels")

    # A `#` on its own, not inside a boolean, is drawn the same way.
    alone = render("#cube(10);\n")
    alone_red = sum(n for rgb, n in alone.items() if reddish(rgb))
    check("a highlighted body outside any boolean is red too", alone_red > 200,
          f"{alone_red} reddish pixels")

    # --- the other modifiers still behave --------------------------------
    bg = render("difference() {\n    cylinder(h=10,d=10);\n    %cube(10);\n}\n")
    bg_red = sum(n for rgb, n in bg.items() if reddish(rgb))
    check("% is not drawn in the highlight colour", bg_red < 200,
          f"{bg_red} reddish pixels")

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
