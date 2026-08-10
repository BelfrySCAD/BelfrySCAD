#!/usr/bin/env python3
"""Point-to-point and three-point measurement, end to end.

Drives a real render and real screen coordinates: the snap math has its own
checks in verify_measure_snap.py, and what this adds is whether clicking a
place on screen reaches the right world point at all.

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

import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow
    from belfryscad.window.viewport import Measurement

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if until():
                return True
        return False

    # --- the value type, independent of any window --------------------
    d = Measurement("distance", [np.array([0., 0, 0]), np.array([3., 4, 0])],
                    ["vertex", "vertex"])
    check("a distance is the euclidean length", abs(d.value() - 5.0) < 1e-9, d.value())
    check("and its label carries the component deltas",
          "dx 3.000" in d.label() and "dy 4.000" in d.label(), d.label())

    a = Measurement("angle", [np.array([1., 0, 0]), np.array([0., 0, 0]),
                              np.array([0., 1, 0])], ["vertex"] * 3)
    check("a right angle measures 90", abs(a.value() - 90.0) < 1e-9, a.value())
    check("the middle point is the vertex",
          abs(Measurement("angle", [np.array([1., 0, 0]), np.array([0., 0, 0]),
                                    np.array([-1., 0, 0])], ["vertex"] * 3).value()
              - 180.0) < 1e-9)
    check("and its label names both leg lengths",
          "legs 1.000, 1.000" in a.label(), a.label())

    z = Measurement("angle", [np.array([0., 0, 0]), np.array([0., 0, 0]),
                              np.array([0., 1, 0])], ["vertex"] * 3)
    check("an angle with a zero-length leg is undefined, not a crash",
          z.value() != z.value() and "undefined" in z.label(), z.label())

    # --- against a real render ----------------------------------------
    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()
    pump(0.8)
    w._new_document()
    w._current_tab().editor.setPlainText("cube([20, 20, 20]);")
    w._render_threadsafe()
    pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    check("the test cube rendered", "volume 8000.000" in w._geometry_summary(),
          w._geometry_summary()[:120])

    vp = w._viewport
    vp.resize(500, 400)
    pump(0.4)

    def screen_of(world):
        """Where a world point lands on screen, for aiming a click."""
        ww, hh = vp.width(), vp.height()
        mvp = (vp._renderer.camera.projection_matrix(ww / hh)
               @ vp._renderer.camera.view_matrix())
        clip = mvp.astype(np.float64) @ np.array([*world, 1.0])
        ndc = clip[:2] / clip[3]
        return QPoint(int((ndc[0] * .5 + .5) * ww), int((1 - (ndc[1] * .5 + .5)) * hh))

    def click(pt):
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, pt.toPointF(),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        vp.mousePressEvent(ev)
        pump(0.2)

    # Two opposite top corners of the cube: a known 20*sqrt(2) diagonal.
    c1 = np.array([0., 0., 20.])
    c2 = np.array([20., 20., 20.])

    w._set_measure_mode("distance")
    check("entering the mode arms the viewport",
          vp.measure_mode() == "distance", str(vp.measure_mode()))
    check("and prompts for the first point",
          "first point" in w.statusBar().currentMessage(),
          w.statusBar().currentMessage())

    click(screen_of(c1))
    check("one click does not finish a distance", w._measurements == [])
    click(screen_of(c2))
    check("two clicks make a measurement", len(w._measurements) == 1,
          str(len(w._measurements)))

    if w._measurements:
        m = w._measurements[0]
        want = float(np.linalg.norm(c2 - c1))
        check("the distance is the face diagonal", abs(m.value() - want) < 0.5,
              f"{m.value():.3f} vs {want:.3f}")
        check("both points snapped to vertices, not the surface",
              m.snaps == ["vertex", "vertex"], str(m.snaps))
        check("and they are the corners themselves",
              np.allclose(sorted([tuple(np.round(p, 3)) for p in m.points]),
                          sorted([tuple(c1), tuple(c2)])), str(m.points))

    # Angle: two edges meeting at a corner is exactly 90 degrees.
    w._clear_measurements()
    w._act_measure_distance.setChecked(False)
    w._set_measure_mode("angle")
    click(screen_of(np.array([20., 0., 20.])))
    click(screen_of(np.array([0., 0., 20.])))      # the vertex
    check("two clicks do not finish an angle", w._measurements == [])
    click(screen_of(np.array([0., 20., 20.])))
    check("three clicks make an angle", len(w._measurements) == 1)
    if w._measurements:
        check("and a cube corner measures 90 degrees",
              abs(w._measurements[0].value() - 90.0) < 1.0,
              str(w._measurements[0].value()))

    # --- the toolbar toggles ---------------------------------------------
    # One QAction each, shown in both the toolbar and the menu, so the two
    # cannot drift apart.
    tb_actions = w._toolbar.actions()
    check("both toggles are on the toolbar",
          w._act_measure_distance in tb_actions
          and w._act_measure_angle in tb_actions, str(len(tb_actions)))
    check("and they carry icons", not w._act_measure_distance.icon().isNull()
          and not w._act_measure_angle.icon().isNull())
    check("they are checkable", w._act_measure_distance.isCheckable()
          and w._act_measure_angle.isCheckable())

    w._set_measure_mode(None)
    w._act_measure_distance.setChecked(True)
    pump(0.1)
    check("checking distance arms that mode", vp.measure_mode() == "distance",
          str(vp.measure_mode()))

    # Only one at a time: turning on angle must turn distance off, and the
    # live mode must end up as angle however Qt orders the two signals.
    w._act_measure_angle.setChecked(True)
    pump(0.1)
    check("turning on angle turns distance off",
          not w._act_measure_distance.isChecked())
    check("and the mode really is angle, not left mid-switch",
          vp.measure_mode() == "angle", str(vp.measure_mode()))

    # Clicking the active one again puts the tool down -- a plain exclusive
    # group would refuse to uncheck and leave no way out.
    #
    # trigger(), not setChecked(False): a programmatic setChecked bypasses
    # the group's exclusion policy entirely, so it passes whichever policy
    # is set and proves nothing about clicking.
    w._act_measure_angle.trigger()
    pump(0.1)
    check("unchecking the active toggle leaves the mode",
          vp.measure_mode() is None, str(vp.measure_mode()))
    check("and neither toggle is left lit",
          not w._act_measure_distance.isChecked()
          and not w._act_measure_angle.isChecked())

    # Escape leaving the mode must clear the toolbar toggle too, or the
    # button would stay lit with nothing behind it. Nothing measured and
    # nothing pending first, or Escape would peel those layers instead --
    # which is what it is supposed to do.
    w._clear_measurements()
    vp.cancel_measurement()
    w._act_measure_distance.setChecked(True)
    pump(0.1)
    w.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                              Qt.KeyboardModifier.NoModifier))
    pump(0.1)
    check("escaping out of the tool unlights the toolbar toggle",
          not w._act_measure_distance.isChecked() and vp.measure_mode() is None,
          f"checked={w._act_measure_distance.isChecked()} mode={vp.measure_mode()}")

    # --- dismissing -----------------------------------------------------
    # Two measurements, so dismissing one has to remove the right one.
    w._clear_measurements()
    w._set_measure_mode("distance")
    click(screen_of(np.array([0., 0., 20.])))
    click(screen_of(np.array([20., 0., 20.])))          # 20 along x
    click(screen_of(np.array([0., 0., 20.])))
    click(screen_of(np.array([0., 20., 20.])))          # 20 along y
    check("two measurements were taken", len(w._measurements) == 2,
          str(len(w._measurements)))

    labels = [lab for lab in vp._measure_labels if lab.isVisible()]
    check("each has a visible label", len(labels) == 2, str(len(labels)))
    check("and the label says what it measures",
          "20.000" in labels[0].text(), labels[0].text())

    if len(w._measurements) == 2 and len(labels) == 2:
        second = w._measurements[1]
        # Click the FIRST label: the remaining measurement must be the
        # second one, not merely "one fewer".
        labels[0].clicked.emit(labels[0])
        pump(0.3)
        check("clicking a label dismisses that measurement",
              len(w._measurements) == 1, str(len(w._measurements)))
        check("and it is the other one that survives",
              w._measurements and w._measurements[0] is second,
              str(w._measurements))
        check("the console records the dismissal",
              "Dismissed measurement" in w._console_tail(),
              w._console_tail()[-120:])

    # A stale index must not throw -- labels outlive the measurements they
    # showed, so a click can arrive after the list has shrunk.
    before = len(w._measurements)
    w._on_measurement_dismissed(99)
    check("an out-of-range dismissal is ignored", len(w._measurements) == before)

    # Escape with finished measurements clears them.
    check("there is something to clear", len(w._measurements) > 0)
    esc = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                    Qt.KeyboardModifier.NoModifier)
    w.keyPressEvent(esc)
    pump(0.2)
    check("escape clears the finished measurements", w._measurements == [],
          str(len(w._measurements)))
    check("and the tool is still armed", vp.measure_mode() == "distance",
          str(vp.measure_mode()))
    w.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                              Qt.KeyboardModifier.NoModifier))
    pump(0.2)
    check("a further escape leaves the tool", vp.measure_mode() is None,
          str(vp.measure_mode()))

    w._set_measure_mode("angle")

    # Escape: first drops the half-taken measurement, then leaves the mode.
    click(screen_of(c1))
    check("a point is pending", vp._measure_pending != [])
    check("escape drops it", vp.cancel_measurement() and vp._measure_pending == [])
    check("but stays in the mode", vp.measure_mode() == "angle")

    # A render clears them: they point at geometry that no longer exists.
    # Take a fresh one first -- the escape checks above emptied the list.
    w._act_measure_angle.setChecked(False)
    w._set_measure_mode("distance")
    click(screen_of(np.array([0., 0., 20.])))
    click(screen_of(np.array([20., 0., 20.])))
    before = len(w._measurements)
    check("there is a measurement to lose", before > 0)
    w._current_tab().editor.setPlainText("cube([30, 30, 30]);")
    w._render_threadsafe()
    pump(30, lambda: not w._measurements and not w._render_busy())
    check("a render clears the measurements", w._measurements == [],
          str(len(w._measurements)))
    check("and says so", "cleared by the render" in w._console_tail(),
          w._console_tail()[-160:])

    # A click that misses the model must not invent a point.
    w._set_measure_mode("distance")
    click(QPoint(3, 3))
    check("a click on empty space adds no point", vp._measure_pending == [])
    check("and says the click missed", "missed" in w.statusBar().currentMessage(),
          w.statusBar().currentMessage())

    # Leaving the mode tidies up.
    w._act_measure_distance.setChecked(False)
    w._set_measure_mode(None)
    check("leaving the mode disarms the viewport", vp.measure_mode() is None)

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
