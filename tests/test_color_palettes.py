"""#766: your own colour palettes in the colour picker and the Color List.

The text format is plain functions and tests directly; the widgets run in a
subprocess with scratch settings.
"""
import json
import subprocess
import sys

from belfryscad.window.color_palettes import format_palette, parse_palette


def test_a_line_is_a_name_then_hex_or_rgb():
    entries, bad = parse_palette(
        "Sunlu Cyan      #0074c3\n"
        "Sunlu Magenta   196, 65, 136\n"
        "Yellow: [206, 189, 42]\n"
        "White\t215 216 227\n"
        "short #FfF\n"
        "#123456\n")
    assert entries == [("Sunlu Cyan", "#0074c3"), ("Sunlu Magenta", "#c44188"),
                       ("Yellow", "#cebd2a"), ("White", "#d7d8e3"), ("short", "#ffffff"),
                       ("#123456", "#123456")]
    assert bad == []


def test_blank_lines_and_comments_are_skipped_and_bad_lines_reported():
    entries, bad = parse_palette("// my shelf\n\nPLA 3  10, 20, 30  // the last three numbers\n"
                                 "no colour here\nToo bright 300, 0, 0\n")
    assert entries == [("PLA 3", "#0a141e")]
    assert bad == [4, 5]


def test_format_round_trips():
    entries = [("Sunlu Cyan", "#0074c3"), ("Red", "#ff0000")]
    assert parse_palette(format_palette(entries)) == (entries, [])


DRIVER = r'''
import json, tempfile
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window import color_palettes
from belfryscad.window.color_list import WEB, ColorTable
from belfryscad.window.color_picker import ColorPickerDialog
color_palettes.save_palettes({"Shelf": [("Sunlu Cyan", "#0074c3"), ("Sunlu Magenta", "#c44188")]})
out = {}
t = ColorTable()
out["views"] = [t._view.itemText(i) for i in range(t._view.count())]
# A literal not in the view last chosen opens on a view that lists it --
# without making that view the remembered one.
x = ColorPickerDialog('"xkcd:dark mint"', QColor("#48c072"))
out["xkcd_literal"] = [x._colors._view.currentText(), x._colors.current_name(), color_palettes.last_view()]
out["opens_on_web"] = t._view.currentText() == WEB
out["remembered_on_open"] = color_palettes.last_view()
t._view.setCurrentText("Shelf")
out["rows"] = [r[0] for r in t._rows]
out["remembered"] = color_palettes.last_view()
t._table.setCurrentCell(0, 1)
out["token"] = t.current_name()
# The picker on a string literal: a palette colour is written as its hex,
# and an existing literal of that colour selects its row.
d = ColorPickerDialog('"#0074c3"', QColor("#0074c3"))
out["picker_view"] = d._colors._view.currentText()
out["picker_selected"] = d._colors.current_name()
d._on_name("#c44188")
out["literal"] = d.chosen_literal()
v = ColorPickerDialog("[1, 0, 0]", QColor("red"))
v._on_name("#0074c3")
out["vector"] = v.chosen_literal()
# The editor: a new palette, rows added, typed and pasted, a rename, all
# saved together.
from belfryscad.window.color_list import PaletteEditor
e = PaletteEditor(color_palettes.load_palettes(), "Shelf")
out["columns"] = [e._table.horizontalHeaderItem(i).text() for i in range(3)]
out["shelf_rows"] = e._rows()
e._ask_name = lambda *a: "Silk"
e._new()
e._add()
e._table.item(0, e.NAME).setText("Gold")
e._table.item(0, e.HEX).setText("212, 175, 55")       # rgb, rewritten as hex
out["normalised"] = e._table.item(0, e.HEX).text()
out["swatch"] = e._table.item(0, e.SWATCH).background().color().name()
e._add()
e._table.item(1, e.NAME).setText("Broken")
e._table.item(1, e.HEX).setText("#12")
e.paste_text("Silver  #c0c0c0\nnothing here\n")
out["paste_status"] = e._status.text()
e._check()
out["status"] = e._status.text()
e._ask_name = lambda *a: "Silk PLA"
e._rename()
e._combo.setCurrentText("Shelf")
out["shelf_kept"] = e._rows()[0]
e._combo.setCurrentText("Silk PLA")
out["silk_kept"] = [r[0] for r in e._rows()]
e._save()
out["saved"] = {k: v for k, v in e.palettes.items()}
print(json.dumps(out))
'''


def test_a_palette_is_listed_picked_as_hex_and_remembered(tmp_path):
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["views"] == ["Web colors", "XKCD colors", "All named colors", "", "Shelf"]
    assert out["opens_on_web"] and out["remembered_on_open"] is None
    assert out["xkcd_literal"] == ["XKCD colors", "xkcd:dark mint", None]
    assert sorted(out["rows"]) == ["Sunlu Cyan", "Sunlu Magenta"]
    assert out["remembered"] == "Shelf"
    assert out["token"] in ("#0074c3", "#c44188")
    assert out["picker_view"] == "Shelf"
    assert out["picker_selected"] == "#0074c3"
    assert out["literal"] == '"#c44188"'
    assert out["vector"] == "[0, 0.455, 0.765]"
    assert out["columns"] == ["Name", "Hex", ""]
    assert out["shelf_rows"] == [["Sunlu Cyan", "#0074c3"], ["Sunlu Magenta", "#c44188"]]
    assert out["normalised"] == "#d4af37" and out["swatch"] == "#d4af37"
    assert out["paste_status"] == "Pasted 1; no color on line 2 of the clipboard."
    assert out["status"] == "2 colors; row 2 has no valid color and will be left out"
    assert out["shelf_kept"] == ["Sunlu Cyan", "#0074c3"]
    assert out["silk_kept"] == ["Gold", "Broken", "Silver"]
    assert out["saved"] == {"Shelf": [["Sunlu Cyan", "#0074c3"], ["Sunlu Magenta", "#c44188"]],
                            "Silk PLA": [["Gold", "#d4af37"], ["Silver", "#c0c0c0"]]}
