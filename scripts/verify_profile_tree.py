"""Checks the profile report's Call Tree against REAL profile data.

Standalone rather than a pytest test: constructing Qt widgets under pytest
crashes the runner (same as scripts/verify_gestures.py).

The tree is built from ProfileResult.paths -- the evaluator's
calling-context tree -- so a row's times are that path's own rather than a
total across every caller of the name. Recursion is folded into single
nodes by the evaluator, so the view needs no cycle guard of its own; the
fixtures below check that a recursive model still terminates and that a
module passed as its own child (a different thing entirely) stays fully
explorable.

Usage:  verify_profile_tree.py [model.scad]
"""
import os
import sys
import tempfile

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import Qt                      # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402
from openscad_cpp_evaluator import Evaluator       # noqa: E402
from belfryscad.window.data_viewers import ProfileViewer  # noqa: E402

app = QApplication(sys.argv)
ok = True


def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok &= bool(cond)


def viewer_for_file(path, trim=""):
    ev = Evaluator(profile=True)
    ev.evaluate(path)
    return ProfileViewer(ev.profile_result, trim_prefix=trim), ev.profile_result


def viewer_for_source(src):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "case.scad")
    with open(path, "w") as fh:
        fh.write(src)
    return viewer_for_file(path)


def walk(v, item, depth=0, out=None, budget=None):
    """Expand everything (bounded) and return one line per row."""
    out = [] if out is None else out
    budget = [500] if budget is None else budget
    for i in range(item.childCount()):
        c = item.child(i)
        budget[0] -= 1
        if budget[0] < 0:
            out.append("RUNAWAY")
            return out
        out.append(f"{'  ' * depth}{c.text(0)} (line {c.text(6)}, n={c.text(4)})")
        v._tree.expandItem(c)
        app.processEvents()
        walk(v, c, depth + 1, out, budget)
    return out


# -- a real model --------------------------------------------------------
model = sys.argv[1] if len(sys.argv) > 1 else None
if model:
    LIB = os.path.expanduser("~/Documents/OpenSCAD/libraries")
    v, result = viewer_for_file(model, LIB)
    print(f"{len(result.call_sites)} call sites, {len(result.paths)} path nodes")

    check("Call Tree is the first tab", v._tabs.tabText(0) == "Call Tree")
    check("Call Tree shows by default", v._tabs.currentIndex() == 0)
    check("flat table is still there", v._tabs.tabText(1) == "All Call Sites")

    root = v._tree.topLevelItem(0)
    check("root is <toplevel>", root.text(0) == "<toplevel>", root.text(0))
    check("root has children", root.childCount() > 0)

    totals = [float(root.child(i).text(2)) for i in range(root.childCount())]  # Total (ms)
    check("children sorted by total desc", totals == sorted(totals, reverse=True), str(totals))

    # Rows index the evaluator's own tree; times must contain the subtree.
    def containment(idx, paths):
        n = paths[idx]
        kids = sum(paths[c]["cumulative_time"] for c in n["children"])
        bad = [] if kids <= n["cumulative_time"] + 1e-9 else [n["name"]]
        for c in n["children"]:
            bad += containment(c, paths)
        return bad

    sys.setrecursionlimit(100000)
    violations = containment(0, result.paths)
    check("every node's time contains its subtree", not violations, str(violations[:3]))

    # Lazy expansion actually fills in.
    node, depth = root.child(0), 0
    while depth < 6 and node.childCount():
        v._tree.expandItem(node)
        app.processEvents()
        kids = [node.child(i).text(0) for i in range(node.childCount())]
        check(f"depth {depth + 1} populated", "…" not in kids, str(kids))
        node, depth = node.child(0), depth + 1
    check("expanded several levels", depth >= 3, f"depth={depth}")

    idx = root.child(0).data(0, Qt.ItemDataRole.UserRole)
    check("rows index a real path node",
          isinstance(idx, int) and bool(result.paths[idx]["call_origin"]))

# -- recursion vs. child-passing ----------------------------------------
#
# `module foo(x) { ... foo(x+1); }` recurses: the evaluator folds every
# level onto one node, so the view terminates without needing its own
# guard. `foo() foo() foo();` across lines is NOT recursion -- three
# distinct call sites -- and every level must stay explorable.
vr, rr = viewer_for_source(
    "module foo(x) { cube(1); if (x < 5) translate([0,0,2]) foo(x + 1); }\nfoo(0);\n")
rec = walk(vr, vr._tree.topLevelItem(0))
check("recursion terminates", not any("RUNAWAY" in l for l in rec), str(rec[:4]))
check("recursion folded into few nodes", len(rr.paths) < 20, f"{len(rr.paths)} nodes")
check("a folded row shows its repeat count",
      any(n["call_count"] > 1 for n in rr.paths), str([n["call_count"] for n in rr.paths]))

vc, _ = viewer_for_source(
    "module foo() { cube(1); translate([0,0,2]) children(); }\nfoo()\n    foo()\n        foo();\n")
nest = walk(vc, vc._tree.topLevelItem(0))
check("child nesting terminates", not any("RUNAWAY" in l for l in nest), str(nest))
check("child nesting reaches its later call site",
      any("line 4" in l for l in nest), str(nest))

print()
print("ALL PASS" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
