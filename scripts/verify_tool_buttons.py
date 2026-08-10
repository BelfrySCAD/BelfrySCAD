#!/usr/bin/env python3
"""The transform tool buttons live in the viewport and follow the selection.

They sit under the perspective toggle, appear only with a shape selected,
and only one runs at a time.

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

from PySide6.QtCore import QEventLoop  # noqa: E402
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
    vp = w._viewport

    check("there are three tool buttons", len(vp._tool_btns) == 3,
          str(len(vp._tool_btns)))
    check("they are checkable", all(b.isCheckable() for b in vp._tool_btns.values()))
    check("and they carry icons",
          all(not b.icon().isNull() for b in vp._tool_btns.values()))

    # Down the left edge, under the perspective toggle.
    px, py = vp._persp_btn.x(), vp._persp_btn.y()
    ys = [vp._tool_btns[i].y() for i in (0, 1, 2)]
    check("all three share the perspective button's column",
          all(vp._tool_btns[i].x() == px for i in (0, 1, 2)),
          str([vp._tool_btns[i].x() for i in (0, 1, 2)]))
    check("they sit below it", min(ys) > py, f"{ys} vs {py}")
    check("and are stacked in order, not on top of each other",
          ys == sorted(ys) and len(set(ys)) == 3, str(ys))

    check("nothing is selected yet, so they are hidden",
          not any(b.isVisible() for b in vp._tool_btns.values()))

    # Render something and select it.
    w._current_tab().editor.setPlainText("cube(10);")
    w._render_threadsafe()
    pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    check("a cube rendered", bool(w._geometry_summary()))
    check("a render alone does not show them -- nothing is selected",
          not any(b.isVisible() for b in vp._tool_btns.values()))

    vp.set_selection(1)
    pump(0.2)
    check("selecting a shape shows all three",
          all(b.isVisible() for b in vp._tool_btns.values()))
    check("none is active until one is clicked",
          not any(b.isChecked() for b in vp._tool_btns.values()))
    check("and no gizmo is drawn yet", not vp._renderer.show_gizmo)

    # Clicking one arms that tool.
    vp._tool_btns[0].click()
    pump(0.2)
    check("clicking Translate arms it", vp._active_tool == 0, str(vp._active_tool))
    check("its button lights up", vp._tool_btns[0].isChecked())
    check("and the gizmo appears", vp._renderer.show_gizmo)
    check("of the right kind", vp._renderer.gizmo_type == 0,
          str(vp._renderer.gizmo_type))

    # Only one at a time.
    vp._tool_btns[1].click()
    pump(0.2)
    check("clicking Rotate switches to it", vp._active_tool == 1, str(vp._active_tool))
    check("and unlights Translate", not vp._tool_btns[0].isChecked())
    check("with only Rotate lit",
          [b.isChecked() for b in (vp._tool_btns[0], vp._tool_btns[1],
                                    vp._tool_btns[2])] == [False, True, False])

    # Clicking the running one puts it away.
    vp._tool_btns[1].click()
    pump(0.2)
    check("clicking the running tool turns it off", vp._active_tool == -1,
          str(vp._active_tool))
    check("no button is left lit",
          not any(b.isChecked() for b in vp._tool_btns.values()))
    check("and the gizmo goes with it", not vp._renderer.show_gizmo)

    # Deselecting hides them, and must not leave a tool running.
    vp._tool_btns[2].click()
    pump(0.2)
    check("Scale is armed", vp._active_tool == 2 and vp._renderer.show_gizmo)
    vp.set_selection(None)
    pump(0.2)
    check("deselecting hides the buttons",
          not any(b.isVisible() for b in vp._tool_btns.values()))
    check("and puts the running tool away", vp._active_tool == -1,
          str(vp._active_tool))
    check("so no gizmo is left over an empty selection",
          not vp._renderer.show_gizmo)

    # Clearing the viewport clears the selection, so they hide with it.
    vp.set_selection(1)
    vp._tool_btns[0].click()
    pump(0.2)
    check("armed again with a selection",
          vp._active_tool == 0 and vp._tool_btns[0].isVisible())
    w._new_document()
    pump(0.3)
    check("File > New hides them too",
          not any(b.isVisible() for b in vp._tool_btns.values()))
    check("and disarms the tool", vp._active_tool == -1, str(vp._active_tool))

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
