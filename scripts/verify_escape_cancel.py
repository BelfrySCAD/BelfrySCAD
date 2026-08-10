#!/usr/bin/env python3
"""Escape only reports a cancelled render when one was actually running.

_render_cancel is not cleared when a render finishes -- it is replaced
when the next one starts -- so it says "a render has run at some point".
Testing it alone made every Escape after the first render claim to
cancel one.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import Qt, QEventLoop  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if until():
                return True
        return False

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()
    pump(0.8)

    logged = []
    real_log = w.log
    w.log = lambda msg, *a, **k: (logged.append(str(msg)), real_log(msg, *a, **k))[1]

    def escape():
        logged.clear()
        QTest.keyClick(w, Qt.Key.Key_Escape)
        pump(0.2)
        return [m for m in logged if "cancel" in m.lower()]

    # --- before anything has ever rendered -------------------------------
    check("no render has run yet", w._render_cancel is None)
    check("Escape says nothing", escape() == [], str(escape()))

    # --- with a render actually in flight --------------------------------
    # Something slow enough to still be running when Escape arrives.
    w._current_tab().editor.setPlainText(
        "for (i=[0:60]) translate([i*3,0,0]) "
        "difference() { sphere(2,$fn=64); cube(2,center=true); }")
    w._render_threadsafe()
    started = pump(10, lambda: w._render_busy())
    check("a render is in flight", started and w._render_busy())
    if started:
        said = escape()
        check("Escape cancels it and says so", said == ["Render cancelled."], str(said))
        pump(5, lambda: not w._render_busy())

    # --- after it has finished -------------------------------------------
    # The regression: _render_cancel is still set here, so testing it alone
    # would report a cancellation with nothing to cancel.
    pump(2, lambda: not w._render_busy())
    check("nothing is running now", not w._render_busy())
    check("but _render_cancel is still set -- which is why this test exists",
          w._render_cancel is not None)
    said = escape()
    check("Escape says nothing", said == [], str(said))

    # --- and after a render that completed normally ----------------------
    w._current_tab().editor.setPlainText("cube(10);")
    w._render_threadsafe()
    pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    check("a quick render finished", bool(w._geometry_summary()))
    said = escape()
    check("Escape after a completed render says nothing", said == [], str(said))

    # Escape must still reach everything else it does.
    w._current_tab().editor.setPlainText("cube(5);")
    w._render_threadsafe()
    pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    w._act_measure_distance.setChecked(True)
    pump(0.3)
    check("the measure tool is armed", w._viewport.measure_mode() is not None)
    QTest.keyClick(w, Qt.Key.Key_Escape)
    pump(0.3)
    check("Escape still disarms the measure tool",
          w._viewport.measure_mode() is None, str(w._viewport.measure_mode()))

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
