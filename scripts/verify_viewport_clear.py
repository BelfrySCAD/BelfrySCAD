#!/usr/bin/env python3
"""The viewport empties when its geometry no longer belongs to anything.

Two cases: File > New, and closing the tab whose render is on screen. In
both, leaving the model up would mean measuring, exporting or selecting
against geometry with no source behind it.

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

    def render_a_cube():
        w._current_tab().editor.setPlainText("cube(12);")
        w._render_threadsafe()
        return pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())

    def viewport_is_empty():
        """Nothing to draw, and nothing derived from it left behind."""
        return (not w._bodies
                and w._rendered_tab is None
                and not w._geometry_summary())

    # --- File > New ----------------------------------------------------
    check("a cube rendered", render_a_cube(), w._geometry_summary()[:80])
    check("the viewport has geometry", not viewport_is_empty())
    rendered_from = w._rendered_tab
    check("and it is attributed to a tab", rendered_from is not None)

    w._new_document()
    pump(0.4)
    check("File > New clears the viewport", viewport_is_empty(),
          f"bodies={len(w._bodies or [])} tab={w._rendered_tab}")
    check("and the measure toggles go dead with it",
          not w._act_measure_distance.isEnabled())

    # --- closing the tab the render came from --------------------------
    # Three tabs, deliberately: closing the last one falls through to
    # _new_document, which clears the viewport for its own reasons and
    # would mask whether closing the rendered tab does anything at all.
    w._new_document()
    w._new_document()
    pump(0.3)
    check("there are at least three tabs", w._tabs.count() >= 3,
          str(w._tabs.count()))

    # Render from the first of them.
    w._tabs.setCurrentIndex(0)
    pump(0.2)
    check("a cube rendered from the first tab", render_a_cube())
    rendered_from = w._rendered_tab
    check("the render is attributed to the current tab",
          rendered_from is w._tabs.widget(0))

    # Closing an unrelated tab must leave the model alone.
    unrelated = w._tabs.widget(w._tabs.count() - 1)
    check("the unrelated tab is not the rendered one", unrelated is not rendered_from)
    w._close_tab(w._tabs.indexOf(unrelated))
    pump(0.3)
    check("closing an unrelated tab leaves the viewport alone",
          not viewport_is_empty(), f"bodies={len(w._bodies or [])}")

    # Now the one the render came from, with another still open.
    check("another tab is still open", w._tabs.count() >= 2, str(w._tabs.count()))
    w._close_tab(w._tabs.indexOf(rendered_from))
    pump(0.4)
    check("closing the rendered tab clears the viewport", viewport_is_empty(),
          f"bodies={len(w._bodies or [])} tab={w._rendered_tab}")
    check("and a tab remains, so this was not just the new-document path",
          w._tabs.count() >= 1, str(w._tabs.count()))

    # --- what else the clear has to take with it -----------------------
    check("a cube rendered once more", render_a_cube())
    w._viewport._renderer.selected_id = 42
    w.id_to_node = {1: object()}
    w._new_document()
    pump(0.3)
    check("the selection is dropped too",
          w._viewport._renderer.selected_id is None,
          str(w._viewport._renderer.selected_id))
    check("and the id-to-node map, which pointed into the old render",
          w.id_to_node == {}, str(w.id_to_node))

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
