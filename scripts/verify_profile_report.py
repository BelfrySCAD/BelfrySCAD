"""Drive a real ProfileViewer and check the three requested changes."""
import sys, types
from PySide6.QtGui import QSurfaceFormat
fmt = QSurfaceFormat(); fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)
from PySide6.QtWidgets import QApplication
from belfryscad.window.data_viewers import ProfileViewer

app = QApplication(sys.argv)
LIB = "/Users/gminette/Documents/OpenSCAD/libraries"
TMP = "/Users/gminette/models/tmpab12cd.scad"

def site(name, origin, line):
    s = types.SimpleNamespace()
    s.name, s.caller_name, s.kind = name, "caller", "module"
    s.call_origin, s.call_line, s.call_column, s.call_count = origin, line, 5, 3
    s.self_time, s.cumulative_time = 0.001, 0.002
    return s

result = types.SimpleNamespace(
    total_time=0.01, resolve_time=0.008, generate_time=0.002, unattributed_time=0.001,
    call_sites=[site("cuboid", f"{LIB}/BOSL2/shapes3d.scad", 812),
                site("mypart", TMP, 4),
                site("other", "/Users/gminette/models/saved.scad", 9)])

v = ProfileViewer(result, path_labels={TMP: "untitled-3.scad *"}, trim_prefix=LIB)
hdr = [v._table.horizontalHeaderItem(c).text() for c in range(v._table.columnCount())]
print("columns:", hdr)
ok = True
loc_ok = hdr[8:11] == ["Line", "Col", "Caller File"]
ok &= loc_ok
print(("PASS " if loc_ok else "FAIL ") + "Line, Col then Caller File, at the end")

rows = {}
for r in range(v._table.rowCount()):
    rows[v._table.item(r, 0).text()] = (v._table.item(r, 8).text(), v._table.item(r, 10).text())
for name, (line, path) in sorted(rows.items()):
    print(f"   {name:8} line={line:<5} file={path}")

checks = [
    ("library prefix trimmed", rows["cuboid"][1] == "BOSL2/shapes3d.scad"),
    ("temp file shows tab name", rows["mypart"][1] == "untitled-3.scad *"),
    ("ordinary path untouched", rows["other"][1] == "/Users/gminette/models/saved.scad"),
    ("line numbers in the Line column", rows["cuboid"][0] == "812"),
]
for label, cond in checks:
    print(("PASS " if cond else "FAIL ") + label); ok &= cond

# navigation must still use the RAW path, not the shortened label
site_obj = v._site_at_row(0)
ok &= site_obj.call_origin.startswith("/")
print(("PASS " if site_obj.call_origin.startswith("/") else "FAIL ") + "navigation keeps the raw path")
print()
print("ALL PASS" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
