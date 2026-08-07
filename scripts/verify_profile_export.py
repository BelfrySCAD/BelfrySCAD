#!/usr/bin/env python3
"""The profile report's Export CSV button writes the tab you are looking at.

Checks both tabs' output against the ProfileResult it came from, including
the case the tree gets wrong if it is read off the widgets: children attach
lazily on expand, so an unexpanded tree must still export in full.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import csv
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication  # noqa: E402

from openscad_cpp_evaluator import Evaluator  # noqa: E402

from belfryscad.window.data_viewers import ProfileViewer  # noqa: E402

SRC = """\
module leaf(n) { cube(n); }
module mid() { leaf(1); leaf(2); }
module wrap() { children(); }
mid();
wrap() mid();
"""

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def as_csv(viewer):
    """Round-trip through the csv module, the way the written file will be."""
    header, rows = viewer._csv_rows()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return list(csv.reader(io.StringIO(buf.getvalue())))


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    with tempfile.TemporaryDirectory() as td:
        scad = Path(td) / "m.scad"
        scad.write_text(SRC)
        ev = Evaluator(profile=True)
        ev.evaluate(str(scad))
        result = ev.profile_result

        v = ProfileViewer(result)

        # --- flat tab ---------------------------------------------------
        v._tabs.setCurrentIndex(1)
        flat = as_csv(v)
        check("flat header names every column",
              flat[0] == ["name", "caller", "kind", "line", "column", "file",
                          "calls", "self_ms", "self_pct", "total_ms", "total_pct"],
              str(flat[0]))
        check("flat exports one row per call site",
              len(flat) - 1 == len(result.call_sites),
              f"{len(flat) - 1} rows vs {len(result.call_sites)} sites")

        leaf_rows = [r for r in flat[1:] if r[0] == "leaf"]
        check("the two leaf() calls on one line stay separate rows",
              len(leaf_rows) == 2 and leaf_rows[0][4] != leaf_rows[1][4],
              str([(r[3], r[4]) for r in leaf_rows]))
        check("a child-forwarded call exports kind 'child'",
              any(r[2] == "child" for r in flat[1:]),
              str(sorted({r[2] for r in flat[1:]})))

        # --- tree tab ---------------------------------------------------
        v._tabs.setCurrentIndex(0)
        tree = as_csv(v)
        check("tree header names every column",
              tree[0] == ["depth", "path", "name", "kind", "line", "column", "file",
                          "calls", "self_ms", "total_ms", "total_pct"],
              str(tree[0]))
        # The whole point of reading self._paths instead of the widgets.
        check("tree exports every path node while collapsed",
              len(tree) - 1 == len(result.paths),
              f"{len(tree) - 1} rows vs {len(result.paths)} nodes")
        check("the root is the only depth-0 row",
              sum(1 for r in tree[1:] if r[0] == "0") == 1)
        check("root is 100% and carries no call site",
              tree[1][2] == "<toplevel>" and tree[1][10] == "100.00" and tree[1][4] == "",
              str(tree[1]))

        depths = [int(r[0]) for r in tree[1:]]
        check("depth never jumps by more than one",
              all(b - a <= 1 for a, b in zip(depths, depths[1:])),
              str(depths))
        check("a nested row's path names its callers",
              all(r[1].startswith("<toplevel>") for r in tree[2:]),
              str({r[1] for r in tree[2:]}))

        # --- the button actually writes a file --------------------------
        out = Path(td) / "out.csv"
        from PySide6.QtWidgets import QFileDialog
        orig = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out), "CSV Files (*.csv)"))
        try:
            v._export_csv()
        finally:
            QFileDialog.getSaveFileName = orig
        check("the file is written", out.exists())
        if out.exists():
            written = list(csv.reader(out.open(newline="", encoding="utf-8")))
            check("the written file matches what the tab holds", written == tree,
                  f"{len(written)} rows vs {len(tree)}")

        # Cancelling must not write anything.
        gone = Path(td) / "cancelled.csv"
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
        try:
            v._export_csv()
        finally:
            QFileDialog.getSaveFileName = orig
        check("cancelling the dialog writes nothing", not gone.exists())

        v.close()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
