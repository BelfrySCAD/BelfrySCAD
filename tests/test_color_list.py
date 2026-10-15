"""Help ▸ Color List: every name color() accepts, with its swatch.

The data, sort keys and filter are plain functions and test directly; the
dialog is a widget and is driven in a subprocess.
"""
import json
import subprocess
import sys

from belfryscad.window.color_list import SORT_KEYS, color_rows, matches
from belfryscad.window.xkcd_colors import XKCD_COLORS


def test_css_names_plus_rebeccapurple_plus_every_xkcd_name():
    rows = color_rows()
    names = {r[0] for r in rows}
    assert len(XKCD_COLORS) == 949
    assert len(rows) == 148 + 949
    assert {"red", "cornflowerblue", "grey", "gray", "rebeccapurple"} <= names
    assert "transparent" not in names
    # xkcd names are listed exactly as color() takes them.
    assert "xkcd:dark mint" in names and "dark mint" not in names
    by = {r[0]: r for r in rows}
    assert by["red"][1] == "#ff0000" and by["red"][2] == "[1, 0, 0]"
    assert by["rebeccapurple"][1] == "#663399"
    assert by["xkcd:dark mint"][1] == "#48c072"


def test_every_sort_keeps_every_row():
    base = sorted(r[0] for r in color_rows())
    for sort in SORT_KEYS:
        assert sorted(r[0] for r in color_rows(sort)) == base, sort


def test_name_sort_files_xkcd_names_by_their_bare_name():
    names = [r[0] for r in color_rows("Name")]
    i = names.index("red")
    assert names[i + 1] == "xkcd:red"
    assert names.index("xkcd:acid green") < names.index("aliceblue")


def test_saturation_and_luminance_run_low_to_high():
    sat = [r[0] for r in color_rows("Saturation")]
    assert sat.index("gray") < sat.index("lavenderblush") < sat.index("red")
    lum = [r[0] for r in color_rows("Luminance")]
    assert lum[0] in ("black", "xkcd:black")
    assert lum.index("blue") < lum.index("yellow") < lum.index("white")


def test_chroma_lightness_and_value_run_low_to_high():
    chroma = [r[0] for r in color_rows("Chroma")]
    assert chroma.index("white") < chroma.index("lavenderblush") < chroma.index("red")
    light = [r[0] for r in color_rows("Lightness")]
    assert light.index("black") < light.index("red") < light.index("white")
    value = [r[0] for r in color_rows("Value")]
    # Value is max(r, g, b): pure red and pure white are both 1.0, above navy.
    assert value.index("navy") < value.index("red") and value.index("navy") < value.index("white")


def test_filter_is_name_or_hex():
    row = ("xkcd:dark mint", "#48c072", "[0.282, 0.753, 0.447]", None)
    assert matches(row, "") and matches(row, "Mint") and matches(row, "xkcd:") and matches(row, "#48c0")
    assert not matches(row, "red")


def test_pane_views_sorts_filters_and_inserts_a_name(tmp_path):
    driver = tmp_path / "_cl.py"
    driver.write_text("""
import json
from PySide6.QtWidgets import QApplication
app = QApplication([])
import tempfile
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.color_list import ColorListPane
c = ColorListPane(None)
inserted = []
c.insert_requested.connect(inserted.append)
out = {"count": c._count.text(), "sorts": [c._sort.itemText(i) for i in range(c._sort.count())],
       "views": [c._view.itemText(i) for i in range(c._view.count())], "view": c._view.currentText()}
c._view.setCurrentText("All named colors")
out["all"] = c._count.text()
c._search.setText("#48c072")
c._sort.setCurrentText("Luminance")        # a re-sort keeps the filter
visible = [r for r in range(c._table.rowCount()) if not c._table.isRowHidden(r)]
out["visible"] = [c._table.item(r, 1).text() for r in visible]
out["count_filtered"] = c._count.text()
c._table.setCurrentCell(visible[0], 1)
c._insert()
out["inserted"] = inserted
c._search.setText("")
c._view.setCurrentText("XKCD colors")       # a view switch keeps the filter
out["xkcd"] = c._count.text()
out["xkcd_only"] = all(row[0].startswith("xkcd:") for row in c._rows)
print(json.dumps(out))
""")
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                         env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                              "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["views"] == ["Web colors", "XKCD colors", "All named colors", "", "Edit palette list…"]
    assert out["view"] == "Web colors" and out["count"] == "148 colors"
    assert out["all"] == "1097 colors"
    assert out["sorts"] == ["Name", "Hue", "Saturation", "Chroma", "Lightness", "Value", "Luminance"]
    assert out["visible"] == ["xkcd:dark mint"]
    assert out["count_filtered"] == "1 of 1097 colors"
    assert out["inserted"] == ["xkcd:dark mint"]
    assert out["xkcd"] == "949 colors" and out["xkcd_only"]


def test_a_selected_rows_swatch_keeps_its_colour(tmp_path):
    """The selection highlight must not paint over the swatch: it is the
    one cell whose colour means something."""
    driver = tmp_path / "_swatch.py"
    driver.write_text("""
import json
from PySide6.QtWidgets import QApplication
app = QApplication([])
import tempfile
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.color_list import ColorListPane
t = ColorListPane(); t.resize(500, 400); t.show()
t._search.setText("red")
t.select("red")
app.processEvents()
table = t._table
row = table.currentRow()
rect = table.visualItemRect(table.item(row, 0))
img = table.viewport().grab().toImage()
swatch = img.pixelColor(rect.center()).name()
name_cell = img.pixelColor(table.visualItemRect(table.item(row, 1)).center()).name()
print(json.dumps({"swatch": swatch, "expected": t._rows[row][3].name(), "name_cell": name_cell}))
""", encoding="utf-8")
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                         env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                              "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["swatch"] == out["expected"] == "#ff0000"
    # The rest of the row still shows it is selected.
    assert out["name_cell"] != "#ffffff"
