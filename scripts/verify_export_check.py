#!/usr/bin/env python3
"""Export warns about a mesh that is not a closed manifold solid.

The GUI writes files through belfryscad.exporters rather than the C++
writers, so it has to run the evaluator's own check or the two front ends
would disagree about what counts as sound. This drives the real check
function, not a copy of it.

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

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


class _Body:
    """Stands in for a ColoredBody: only .body.to_mesh() is read."""

    class _Mesh:
        def __init__(self, verts, tris):
            import numpy as np
            self.vert_properties = np.asarray(verts, dtype="float32").reshape(-1, 3)
            self.tri_verts = np.asarray(tris, dtype="uint32").reshape(-1, 3)

    class _Inner:
        def __init__(self, verts, tris, empty=False):
            self._m = _Body._Mesh(verts, tris)
            self._empty = empty

        def is_empty(self):
            return self._empty

        def to_mesh(self):
            return self._m

    def __init__(self, verts, tris, empty=False):
        self.body = _Body._Inner(verts, tris, empty)


TETRA_V = [0, 0, 0,  1, 0, 0,  0, 1, 0,  0, 0, 1]
TETRA_T = [0, 2, 1,  0, 1, 3,  1, 2, 3,  0, 3, 2]


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow
    from openscad_cpp_evaluator import check_mesh

    look = MainWindow._check_export_bodies

    # --- the check itself, on known meshes -----------------------------
    d = check_mesh(TETRA_V, TETRA_T)
    check("a closed tetrahedron passes", d["ok"] and d["watertight"], d["summary"])

    holed = check_mesh(TETRA_V, TETRA_T[:9])
    check("a missing face is reported as boundary edges",
          holed["boundary_edges"] == 3 and not holed["ok"], holed["summary"])

    flipped = list(TETRA_T)
    flipped[1], flipped[2] = flipped[2], flipped[1]
    wound = check_mesh(TETRA_V, flipped)
    check("a reversed face is caught as inconsistent winding",
          wound["inconsistent_edges"] == 3 and not wound["orientable"], wound["summary"])
    check("and is not mistaken for a hole", wound["boundary_edges"] == 0, wound["summary"])

    # --- what export would say -----------------------------------------
    check("a sound body produces no warning", look([_Body(TETRA_V, TETRA_T)]) == [])

    msgs = look([_Body(TETRA_V, TETRA_T[:9])])
    check("an unsound body produces one warning", len(msgs) == 1, str(msgs))
    if msgs:
        check("the warning names the part and the defect",
              "part 1" in msgs[0] and "boundary edge" in msgs[0], msgs[0])

    msgs = look([_Body(TETRA_V, TETRA_T), _Body(TETRA_V, TETRA_T[:9])])
    check("only the unsound part is named", len(msgs) == 1 and "part 2" in msgs[0], str(msgs))

    check("an empty body is skipped, not reported",
          look([_Body(TETRA_V, TETRA_T, empty=True)]) == [])

    # A check that raises must never stop a save -- the file matters more
    # than the diagnosis.
    class _Broken:
        class _Inner:
            def is_empty(self): return False
            def to_mesh(self): raise RuntimeError("nope")
        body = _Inner()

    try:
        check("a failing check is swallowed rather than blocking export",
              look([_Broken()]) == [])
    except Exception as e:      # noqa: BLE001
        check("a failing check is swallowed rather than blocking export", False, str(e))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
