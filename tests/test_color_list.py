"""Help ▸ Color List: every name color() accepts, with its swatch.

The data and filter are plain functions and test directly; the dialog is a
widget and is driven in a subprocess.
"""
import json
import subprocess
import sys

from belfryscad.window.color_list import color_rows, matches


def test_rows_are_qts_names_minus_transparent():
    rows = color_rows()
    assert len(rows) == 147
    names = {r[0] for r in rows}
    assert {"red", "cornflowerblue", "grey", "gray"} <= names
    assert "transparent" not in names
    assert dict((r[0], r[1]) for r in rows)["red"] == "#ff0000"
    assert dict((r[0], r[2]) for r in rows)["red"] == "[1, 0, 0]"


def test_filter_is_name_or_hex_case_insensitive():
    row = ("cornflowerblue", "#6495ed", "[0.392, 0.584, 0.929]")
    assert matches(row, "")
    assert matches(row, "Flower")
    assert matches(row, "#6495")
    assert not matches(row, "red")


def test_dialog_filters_and_copies_quoted_name(tmp_path):
    driver = tmp_path / "_cl.py"
    driver.write_text('''
import json
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.window.color_list import ColorListDialog
dlg = ColorListDialog(None)
out = {"title": dlg.windowTitle(), "count": dlg._count.text()}
dlg._search.setText("tomato")
visible = [r for r in range(dlg._table.rowCount()) if not dlg._table.isRowHidden(r)]
out["visible"] = [dlg._table.item(r, 1).text() for r in visible]
out["count_filtered"] = dlg._count.text()
dlg._table.setCurrentCell(visible[0], 1)
dlg._copy_selected()
out["clipboard"] = QApplication.clipboard().text()
print(json.dumps(out))
''')
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                         env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                              "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["title"] == "Color List"
    assert out["count"] == "147 colors"
    assert out["visible"] == ["tomato"]
    assert out["count_filtered"] == "1 of 147 colors"
    assert out["clipboard"] == '"tomato"'
