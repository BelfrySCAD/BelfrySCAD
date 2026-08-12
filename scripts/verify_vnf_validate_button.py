#!/usr/bin/env python3
"""The VNF viewer's Validate button finds problems and draws them.

tests/test_vnf_validate.py covers the geometry; this covers the button:
that pressing it reports what the validator found, highlights it in the
viewport, and says nothing alarming about a sound mesh.

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

CUBE_PTS = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],
            [0, 0, 10], [10, 0, 10], [10, 10, 10], [0, 10, 10]]
CUBE_TRIS = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
             [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
             [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.data_viewers import VNFViewer

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    def open_viewer(vnf):
        dlg = VNFViewer("mesh", vnf, editable=True)
        dlg.resize(820, 560)
        dlg.show()
        pump(1.3)
        return dlg

    def overlay_segments(vp):
        return 0 if getattr(vp, "_validation_vao", None) is None else 1

    # --- a sound cube -------------------------------------------------
    dlg = open_viewer([[p[:] for p in CUBE_PTS], [t[:] for t in CUBE_TRIS]])
    check("the viewer has a Validate button", hasattr(dlg, "_validate_btn"))
    check("and it is labelled Validate", dlg._validate_btn.text() == "Validate")
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("a sound cube reports no problems", "No problems found" in msg, msg)
    check("and nothing is highlighted", overlay_segments(dlg._vp) == 0,
          "an overlay was drawn for a clean mesh")
    dlg.close()
    pump(0.3)

    # --- one face removed: holes --------------------------------------
    tris = [t[:] for t in CUBE_TRIS]
    del tris[0]
    dlg = open_viewer([[p[:] for p in CUBE_PTS], tris])
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("a missing face is reported as hole edges", "hole edge" in msg, msg)
    check("and the overlay is drawn", overlay_segments(dlg._vp) == 1, msg)
    dlg.close()
    pump(0.3)

    # --- a reversed face: flipped normals -----------------------------
    tris = [t[:] for t in CUBE_TRIS]
    tris[0] = list(reversed(tris[0]))
    dlg = open_viewer([[p[:] for p in CUBE_PTS], tris])
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("a reversed face is reported as flipped normals",
          "flipped-normal edge" in msg, msg)
    check("and the overlay is drawn", overlay_segments(dlg._vp) == 1, msg)
    dlg.close()
    pump(0.3)

    # --- intersecting faces -------------------------------------------
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0],
         [5, -5, -5], [5, 5, -5], [5, 0, 5]]
    dlg = open_viewer([v, [[0, 1, 2], [3, 4, 5]]])
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("crossing faces are reported", "intersecting face pair" in msg, msg)
    check("and the overlay is drawn", overlay_segments(dlg._vp) == 1, msg)
    dlg.close()
    pump(0.3)

    # --- overlapping coplanar faces -----------------------------------
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0],
         [1, 1, 0], [11, 1, 0], [1, 11, 0]]
    dlg = open_viewer([v, [[0, 1, 2], [3, 4, 5]]])
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("coplanar overlap is reported", "overlapping coplanar pair" in msg, msg)
    dlg.close()
    pump(0.3)

    # --- indices as an evaluated VNF carries them ---------------------
    # A VNF that came from a value, not from source text, has float
    # indices -- this is what the button actually failed on in use.
    v = [[0., 0., 0.], [10., 0., 0.], [10., 10., 0.], [0., 10., 0.]]
    dlg = open_viewer([v, [[0., 1., 2.], [0., 2., 3.]]])
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("float face indices validate rather than failing",
          "hole edge" in msg, msg)
    check("and the overlay is drawn", overlay_segments(dlg._vp) == 1, msg)
    dlg.close()
    pump(0.3)

    # --- face outlines survive float indices too ----------------------
    # These go through a second index path (the viewer's own remap
    # lookup), which silently drew nothing rather than raising.
    v = [[0., 0., 0.], [10., 0., 0.], [0., 10., 0.],
         [1., 1., 0.], [11., 1., 0.], [1., 11., 0.]]
    dlg = open_viewer([v, [[0., 1., 2.], [3., 4., 5.]]])
    dlg._validate_btn.click()
    pump(1.2)
    msg = dlg._validation_label.text()
    check("coplanar overlap on float indices is reported",
          "overlapping coplanar pair" in msg, msg)
    # Counting the buffer would not isolate this: two open triangles also
    # produce hole edges, so an overlay exists either way. Look for the
    # overlap colour specifically -- only the outline path emits it.
    want = dlg._vp.VALIDATION_COLORS["overlapping"]
    check("and its face outlines are drawn",
          any(tuple(c) == want for _a, _b, c in dlg._vp._validation_segs),
          f"no {want} segments among {len(dlg._vp._validation_segs)}")
    dlg.close()
    pump(0.3)

    # --- re-validating clears the previous overlay --------------------
    # Otherwise a fixed mesh keeps its old highlights and reads as broken.
    tris = [t[:] for t in CUBE_TRIS]
    del tris[0]
    dlg = open_viewer([[p[:] for p in CUBE_PTS], tris])
    dlg._validate_btn.click()
    pump(1.0)
    had = overlay_segments(dlg._vp)
    dlg._vnf[1].append(CUBE_TRIS[0][:])          # put the face back
    dlg._validate_btn.click()
    pump(1.2)
    check("validating a repaired mesh clears the old highlights",
          had == 1 and overlay_segments(dlg._vp) == 0,
          f"before={had} after={overlay_segments(dlg._vp)}")
    check("and it now reports clean",
          "No problems found" in dlg._validation_label.text(),
          dlg._validation_label.text())
    dlg.close()
    pump(0.3)

    # --- editing the VNF drops the result -----------------------------
    # A report describes one exact mesh. Left up after an edit, its marks
    # sit on vertices that have moved and claim defects that may be gone.
    edits = [
        ("a table edit", lambda d: d._vert_table.item(0, 0).setText("3")),
        ("a vertex drag", lambda d: d._on_viewport_vertex_moved(0, 1.0, 2.0, 3.0)),
        ("deleting a face", lambda d: d._delete_face(1)),
        ("reversing a face", lambda d: d._reverse_face(1)),
        ("undo", lambda d: (d._delete_face(1), d._undo_stack.undo())),
    ]
    for label, do_edit in edits:
        tris = [t[:] for t in CUBE_TRIS]
        del tris[0]
        dlg = open_viewer([[p[:] for p in CUBE_PTS], tris])
        dlg._validate_btn.click()
        pump(1.0)
        before = overlay_segments(dlg._vp)
        had_text = bool(dlg._validation_label.text())
        do_edit(dlg)
        pump(0.8)
        check(f"{label} clears the marks",
              before == 1 and overlay_segments(dlg._vp) == 0,
              f"before={before} after={overlay_segments(dlg._vp)}")
        check(f"{label} clears the message",
              had_text and dlg._validation_label.text() == "",
              f"had={had_text!r} now={dlg._validation_label.text()!r}")
        dlg.close()
        pump(0.3)

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
