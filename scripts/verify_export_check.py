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

    # --- the merged mesh is what gets written -----------------------------
    # Checking the parts is not the same as checking the file. A Menger
    # sponge is thousands of individually perfect cubes whose concatenation
    # is riddled with duplicate faces, and the per-part check passed it.
    from types import SimpleNamespace
    import numpy as np

    look_mesh = MainWindow._check_export_mesh

    good = SimpleNamespace(
        vert_properties=np.asarray(TETRA_V, dtype=np.float32).reshape(-1, 3),
        tri_verts=np.asarray(TETRA_T, dtype=np.uint32).reshape(-1, 3))
    check("a sound merged mesh produces no warning", look_mesh(good) == [])

    # Two coincident tetrahedra: each is perfect, the concatenation is not.
    doubled = SimpleNamespace(
        vert_properties=np.asarray(TETRA_V + TETRA_V, dtype=np.float32).reshape(-1, 3),
        tri_verts=np.asarray(TETRA_T + [i + 4 for i in TETRA_T],
                             dtype=np.uint32).reshape(-1, 3))
    per_part = look([_Body(TETRA_V, TETRA_T), _Body(TETRA_V, TETRA_T)])
    check("each part on its own looks fine", per_part == [], str(per_part))
    # The two copies use different indices, so an index-based check sees two
    # sound solids. Welding by position -- what reading the file does -- is
    # what turns them into the doubled faces a slicer chokes on, and the
    # export check has to weld for that reason.
    merged = look_mesh(doubled)
    check("but the merged mesh is caught once welded", len(merged) == 1, str(merged))
    if merged:
        check("and names the defect", "duplicate face" in merged[0]
              or "non-manifold" in merged[0], merged[0])

    check("a merged-mesh check that fails is swallowed too",
          look_mesh(SimpleNamespace(vert_properties=None, tri_verts=None)) == [])

    # --- the real thing: a sponge must export manifold --------------------
    import subprocess, tempfile, textwrap
    from belfryscad import exporters
    from openscad_cpp_evaluator import Evaluator, check_mesh
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "sponge.scad"
        src.write_text(textwrap.dedent("""
            module sponge(n, s) {
                if (n == 0) cube(s, center=true);
                else {
                    t = s/3;
                    for (x=[-1:1], y=[-1:1], z=[-1:1])
                        if (abs(x)+abs(y)+abs(z) > 1)
                            translate([x*t, y*t, z*t]) sponge(n-1, t);
                }
            }
            sponge(2, 27);
        """))
        ev = Evaluator(echo_fn=lambda m: None)
        out = ev.evaluate(str(src))
        bodies = out[0] if isinstance(out[0], list) else out
        check("the sponge really is many separate bodies", len(bodies) > 100, str(len(bodies)))
        mesh = exporters.merge_bodies_to_mesh(bodies)
        v = np.asarray(mesh.vert_properties, dtype=np.float32).ravel().tolist()
        t = np.asarray(mesh.tri_verts, dtype=np.uint32).ravel().tolist()
        d = check_mesh(v, t)
        check("merging abutting bodies gives a manifold mesh", d["ok"], d["summary"])
        check("and drops the shared interior faces rather than doubling them",
              len(t) // 3 < len(bodies) * 12, f"{len(t)//3} tris from {len(bodies)} cubes")
        check("export of the sponge warns about nothing", look_mesh(mesh) == [],
              str(look_mesh(mesh)))

    # --- slivers are reported, but not as "not manifold" -------------------
    # A closed cube whose top face is fanned through a point on its diagonal:
    # manifold, with one zero-area triangle. CSG emits these routinely.
    sliver = SimpleNamespace(
        vert_properties=np.asarray(
            [0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
             0, 0, 1,  1, 0, 1,  1, 1, 1,  0, 1, 1,
             0.5, 0.5, 1], dtype=np.float32).reshape(-1, 3),
        tri_verts=np.asarray(
            [0, 2, 1,  0, 3, 2,
             4, 5, 8,  5, 6, 8,  4, 8, 6,  4, 6, 7,
             0, 1, 5,  0, 5, 4,  1, 2, 6,  1, 6, 5,
             2, 3, 7,  2, 7, 6,  3, 0, 4,  3, 4, 7], dtype=np.uint32).reshape(-1, 3))
    msgs = look_mesh(sliver)
    check("a mesh with a sliver is still reported on", len(msgs) == 1, str(msgs))
    if msgs:
        check("but not called non-manifold, because it is one",
              "not a closed manifold" not in msgs[0], msgs[0])
        check("and the reason given is the zero-area triangle",
              "zero-area triangle" in msgs[0] and " 1 " in msgs[0], msgs[0])

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
