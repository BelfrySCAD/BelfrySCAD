"""Checks the profile report's Call Tree against REAL profile data.

Standalone rather than a pytest test: constructing Qt widgets under
pytest crashes the runner (same as scripts/verify_gestures.py).

Run it against a deep BOSL2 model to exercise nesting, and against a
recursive one to exercise the cycle guard -- a name already on the path
from the root is shown with a marker and NOT expanded, or the graph
would unroll forever.
"""
import sys
from PySide6.QtGui import QSurfaceFormat
fmt = QSurfaceFormat(); fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from openscad_cpp_evaluator import Evaluator
from belfryscad.window.data_viewers import ProfileViewer

app = QApplication(sys.argv)
ev = Evaluator(profile=True)
ev.evaluate(sys.argv[1] if len(sys.argv) > 1 else "proftest.scad")
result = ev.profile_result
print(f"{len(result.call_sites)} call sites")

LIB = "/Users/gminette/Documents/OpenSCAD/libraries"
v = ProfileViewer(result, trim_prefix=LIB)

ok = True
def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok &= cond

check("Call Tree is the first tab", v._tabs.tabText(0) == "Call Tree")
check("Call Tree shows by default", v._tabs.currentIndex() == 0)
check("flat table is still there", v._tabs.tabText(1) == "All Call Sites")

tree = v._tree
root = tree.topLevelItem(0)
check("root is <toplevel>", root.text(0) == "<toplevel>", root.text(0))
check("root has children", root.childCount() > 0, str(root.childCount()))

print("\n  <toplevel>")
for i in range(root.childCount()):
    c = root.child(i)
    print(f"    {c.text(0):22} {c.text(1):9} calls={c.text(2):>4} total={c.text(3):>8}ms {c.text(4):>5}%  {c.text(7)}")

# children sorted most-expensive first
totals = [float(root.child(i).text(3)) for i in range(root.childCount())]
check("children sorted by total desc", totals == sorted(totals, reverse=True), str(totals))

# expand the heaviest branch a few levels and make sure it populates
node = root.child(0)
depth = 0
while depth < 6:
    if node.childCount() == 0:
        break
    tree.expandItem(node)          # triggers the lazy fill
    app.processEvents()
    if node.childCount() == 0:
        break
    kids = [node.child(i).text(0) for i in range(node.childCount())]
    print(f"    depth {depth+1}: {node.text(0).strip()} -> {kids[:4]}{' …' if len(kids)>4 else ''}")
    check(f"depth {depth+1} populated (no placeholder left)", "…" not in kids, str(kids))
    node = node.child(0)
    depth += 1
# Depth depends on the model: a recursive one legitimately stops at the
# first repeat. What matters is that expansion terminates and every level
# it did open got real children (asserted in the loop above).
check("expanded at least one level", depth >= 1, f"depth={depth}")

# recursion guard: no row marked recursive should have been expanded
def scan(item, seen):
    for i in range(item.childCount()):
        c = item.child(i)
        if "↻" in c.text(0):
            seen.append((c.text(0), c.childCount()))
        scan(c, seen)
    return seen
recursive_rows = scan(root, [])
bad = [r for r in recursive_rows if r[1] != 0]
check("recursive rows are not expanded", not bad, str(bad[:3]))
print(f"  ({len(recursive_rows)} recursive rows marked)")

# a real site is attached for navigation
site = root.child(0).data(0, Qt.ItemDataRole.UserRole)
check("rows carry their site for navigation", site is not None and bool(site.call_origin))

# -- cycle guard: recursion vs. child nesting ----------------------------
#
# These are different things and the guard must tell them apart:
#   module foo(x) { ... foo(x+1); }   revisits the SAME call site forever
#   foo() foo() foo();                is three DIFFERENT foo->foo sites
# A name-keyed guard cuts the second one off at the first repeat, hiding
# the rest of the chain. Keying on the call site fixes that.
import tempfile, os

def _tree_for_source(src):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "case.scad")
    with open(path, "w") as fh:
        fh.write(src)
    e = Evaluator(profile=True)
    e.evaluate(path)
    return ProfileViewer(e.profile_result)

def _walk(v, item, depth=0, out=None, budget=None):
    out = [] if out is None else out
    budget = [400] if budget is None else budget
    for i in range(item.childCount()):
        c = item.child(i)
        budget[0] -= 1
        if budget[0] < 0:
            out.append("RUNAWAY")
            return out
        out.append(f"{'  ' * depth}{c.text(0)} (line {c.text(6)})")
        v._tree.expandItem(c)
        app.processEvents()
        _walk(v, c, depth + 1, out, budget)
    return out

print()
rec = _walk(_tree_for_source(
    "module foo(x) { cube(1); if (x < 5) translate([0,0,2]) foo(x + 1); }\nfoo(0);\n"),
    None) if False else None
vr = _tree_for_source(
    "module foo(x) { cube(1); if (x < 5) translate([0,0,2]) foo(x + 1); }\nfoo(0);\n")
rec = _walk(vr, vr._tree.topLevelItem(0))
check("true recursion is marked and stopped",
      any("\u21bb" in l for l in rec) and not any("RUNAWAY" in l for l in rec), str(rec))

vc = _tree_for_source(
    "module foo() { cube(1); translate([0,0,2]) children(); }\nfoo()\n    foo()\n        foo();\n")
nest = _walk(vc, vc._tree.topLevelItem(0))
check("child nesting terminates", not any("RUNAWAY" in l for l in nest), str(nest))
check("child nesting still reaches its distinct later call site",
      any("line 4" in l for l in nest), str(nest))

print()
print("ALL PASS" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
