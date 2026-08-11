#!/usr/bin/env python3
"""Every 3D data viewer can hide the markers on unselected points.

The markers sit on top of the shape they describe, so a dense path or
grid is mostly indicators. Each viewer gets the "Show Vertices" checkbox
_VNFViewport already had; unchecking it clears the unselected markers
while a selected point keeps its own blinking marker, so you never lose
what you picked.

Checked two ways: the renderer's point buffers actually empty, and the
rendered pixels actually change. Qt widgets crash pytest in this
project, so this runs standalone.
"""
import math
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
    from belfryscad.window.data_viewers import (AffineMatrixViewer, GridViewer,
                                                PathViewer, RegionViewer,
                                                VNFViewer)

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    star = [[round(math.cos(a * math.pi / 8) * (30 if a % 2 else 55), 4),
             round(math.sin(a * math.pi / 8) * (30 if a % 2 else 55), 4)]
            for a in range(16)]
    grid = [[[x * 12.0, y * 12.0, round(math.sin(x / 1.5) * 9, 3)]
             for x in range(7)] for y in range(6)]
    square = [[[-40, -40], [40, -40], [40, 40], [-40, 40]]]
    ident = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    cases = [
        ("PathViewer", lambda: PathViewer("outline", star, editable=True)),
        ("GridViewer", lambda: GridViewer("net", grid, editable=True)),
        ("RegionViewer", lambda: RegionViewer("region", square, editable=True)),
        ("AffineMatrixViewer", lambda: AffineMatrixViewer("xf", ident, editable=True)),
        ("VNFViewer", lambda: VNFViewer("vnf", [[[0, 0, 0], [20, 0, 0], [0, 20, 0],
                                                 [0, 0, 20]],
                                                [[0, 1, 2], [0, 1, 3], [1, 2, 3],
                                                 [0, 2, 3]]])),
    ]

    for name, make in cases:
        dlg = make()
        dlg.resize(700, 520)
        dlg.show()
        pump(1.4)

        vp = dlg._vp
        check(f"{name}: has the toggle", hasattr(vp, "set_show_unselected"))
        cbs = [c for c in dlg.findChildren(type(dlg._show_verts_cb))
               if c.text() == "Show Vertices"] if hasattr(dlg, "_show_verts_cb") else []
        if name != "VNFViewer":
            check(f"{name}: shows a \"Show Vertices\" checkbox", bool(cbs))
            check(f"{name}: checked by default, preserving old behaviour",
                  bool(cbs) and cbs[0].isChecked())

        def markers():
            return len(vp._renderer._point_buffers)

        def shot():
            return vp.grab().toImage()

        def differing(a, b):
            """Pixels that actually changed. Counting non-black pixels does
            not work -- the background is light grey, so nearly every pixel
            qualifies and the total never moves however many markers are
            drawn. An earlier version of this check passed on that."""
            n = 0
            for y in range(0, min(a.height(), b.height()), 2):
                for x in range(0, min(a.width(), b.width()), 2):
                    if a.pixelColor(x, y) != b.pixelColor(x, y):
                        n += 1
            return n

        # RegionViewer selects every point of the region by default, and
        # selected points are drawn by the *selection* markers -- with
        # nothing left unselected there is correctly nothing to hide.
        if hasattr(vp, "set_selected"):
            vp.set_selected([])
            pump(0.5)

        if name == "VNFViewer":       # opt-in: turn it on first
            vp.set_show_unselected(True)
            pump(0.6)

        on_markers, on_img = markers(), shot()
        check(f"{name}: markers present while shown", on_markers > 0, f"{on_markers} buffers")

        vp.set_show_unselected(False)
        pump(0.6)
        off_markers, off_img = markers(), shot()
        check(f"{name}: unchecking clears the markers", off_markers == 0,
              f"{off_markers} buffers still uploaded")
        changed = differing(on_img, off_img)
        check(f"{name}: and the view actually changes", changed > 20,
              f"only {changed} pixels differ")

        vp.set_show_unselected(True)
        pump(0.6)
        check(f"{name}: re-checking brings them back", markers() == on_markers,
              f"{markers()} vs {on_markers}")

        # A selected point must survive the toggle -- that is the whole
        # point of hiding only the *unselected* ones.
        if hasattr(vp, "set_selected"):
            vp.set_selected([1])
            pump(0.6)
            vp.set_show_unselected(False)
            pump(0.6)
            sel_alive = any(getattr(vp, a, None) is not None
                            for a in ("_sel_vao_r", "_sel_vao_w",
                                      "_vert_marker_vao_r", "_vert_marker_vao_w"))
            check(f"{name}: the selected point keeps its marker when hidden",
                  sel_alive, "selection markers were released too")

        dlg.close()
        pump(0.3)

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
