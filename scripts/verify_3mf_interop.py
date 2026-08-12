#!/usr/bin/env python3
"""Other people's 3MF readers accept what we write.

tests/test_3mf_export.py asserts the file's own structure; this checks
that real consumers agree, which is the part a self-consistent writer
can still get wrong. Both readers are optional -- each is skipped, loudly,
if it is not on this machine.

  * lib3mf, the reference implementation this writer replaced. Compares
    vertices, triangles and object colours against what it reads back.
  * OpenSCAD, an entirely independent importer. Compares facet count and
    bounding box against the same model exported as STL.

Run after any change to the 3MF writer.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

from belfryscad import exporters  # noqa: E402
from openscad_cpp_evaluator import Evaluator  # noqa: E402

SRC = '''
color("red") cube(10);
color("blue") translate([15, 0, 0]) sphere(r=6, $fn=16);
translate([0, 20, 0]) cylinder(h=8, r=4, $fn=12);
'''

failures = []
skipped = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    tmp = Path(tempfile.mkdtemp())
    scad = tmp / "m.scad"
    scad.write_text(SRC)
    bodies, _ = Evaluator(echo_fn=lambda _m: None).evaluate(str(scad))
    bodies = exporters.exportable(bodies)

    ours = tmp / "ours.3mf"
    exporters.write_3mf(str(ours), bodies)
    check("the writer produced a file", ours.exists() and ours.stat().st_size > 0)

    expected = exporters.bodies_to_3mf_meshes(bodies)
    print(f"  wrote {len(expected)} objects, "
          f"{sum(len(t) for _v, t, _c in expected)} triangles, "
          f"{ours.stat().st_size} bytes")

    # --- lib3mf, the implementation this replaced ----------------------
    try:
        import lib3mf
    except ImportError:
        skipped.append("lib3mf (not installed -- it is no longer a dependency)")
    else:
        w = lib3mf.Wrapper()
        model = w.CreateModel()
        model.QueryReader("3mf").ReadFromFile(str(ours))
        got = []
        it = model.GetMeshObjects()
        while it.MoveNext():
            o = it.GetCurrentMeshObject()
            vs = np.array([[p.Coordinates[0], p.Coordinates[1], p.Coordinates[2]]
                           for p in o.GetVertices()], dtype=np.float64)
            ts = np.array([[t.Indices[0], t.Indices[1], t.Indices[2]]
                           for t in o.GetTriangleIndices()], dtype=np.int64)
            rid, pidx, has = o.GetObjectLevelProperty()
            col = None
            if has:
                c = model.GetColorGroupByID(rid).GetColor(pidx)
                col = (c.Red, c.Green, c.Blue, c.Alpha)
            got.append((vs, ts, col))

        check("lib3mf reads the file at all", bool(got), "no objects came back")
        check("lib3mf sees every object", len(got) == len(expected),
              f"{len(got)} of {len(expected)}")
        for i, ((ev, et, ec), (gv, gt, gc)) in enumerate(zip(expected, got)):
            check(f"object {i}: vertices survive",
                  gv.shape == ev.shape and np.allclose(gv, ev, atol=1e-4),
                  f"{gv.shape} vs {ev.shape}")
            check(f"object {i}: triangles survive",
                  gt.shape == et.shape and np.array_equal(gt, et),
                  f"{gt.shape} vs {et.shape}")
            want = tuple(max(0, min(255, int(round(c * 255)))) for c in ec)
            check(f"object {i}: colour survives", gc == want, f"{gc} vs {want}")

    # --- OpenSCAD, an independent importer -----------------------------
    oscad = next((p for p in (
        os.path.expanduser("~/Desktop/OpenSCAD-dev.app/Contents/MacOS/OpenSCAD"),
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        shutil.which("openscad") or "") if p and Path(p).exists()), None)
    if not oscad:
        skipped.append("OpenSCAD (not found on this machine)")
    else:
        def summary(model_path, wrapper):
            s = tmp / "s.json"
            imp = tmp / "imp.scad"
            imp.write_text(wrapper.format(p=model_path))
            subprocess.run([oscad, "-o", str(tmp / "o.stl"), "--summary", "all",
                            "--summary-file", str(s), "-q", str(imp)],
                           capture_output=True, timeout=600)
            import json
            d = json.loads(s.read_text())
            g = d.get("geometry", {})
            return g.get("facets"), g.get("bounding_box", {}).get("size")

        stl = tmp / "ref.stl"
        exporters.write_stl(str(stl), exporters.merge_bodies_to_mesh(bodies))
        f3, bb3 = summary(ours, 'import("{p}");')
        fst, bbst = summary(stl, 'import("{p}");')
        check("OpenSCAD imports our 3MF", f3 is not None, "no geometry came back")
        check("and gets the same triangle count as from our STL", f3 == fst,
              f"3mf={f3} stl={fst}")
        check("and the same bounding box",
              bb3 and bbst and all(abs(a - b) < 1e-3 for a, b in zip(bb3, bbst)),
              f"3mf={bb3} stl={bbst}")

    for s in skipped:
        print(f"SKIP {s}")
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
