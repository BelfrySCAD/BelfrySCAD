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
from PySide6.QtWidgets import QApplication, QColorDialog, QInputDialog
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window import color_list, color_palettes
from belfryscad.window.color_list import EDIT_PALETTES, WEB, ColorListPane, PaletteListEditor
color_palettes.save_palettes({"Shelf": [("Sunlu Cyan", "#0074c3"), ("Sunlu Magenta", "#c44188")]})
out = {}
t = ColorListPane()
out["views"] = [t._view.itemText(i) for i in range(t._view.count())]
out["opens_on_web"] = t._view.currentText() == WEB
out["buttons_off"] = not t._add_btn.isEnabled() and not t._remove_btn.isEnabled()
# Choose Color... on a literal the view does not list switches to one that
# does -- without making that view the remembered one.
t.show_literal('"xkcd:dark mint"')
out["xkcd_literal"] = [t._view.currentText(), t.current_name(), color_palettes.last_view()]
t._view.setCurrentText("Shelf")
out["rows"] = [r[0] for r in t._rows]
out["remembered"] = color_palettes.last_view()
out["buttons_on"] = t._add_btn.isEnabled() and t._remove_btn.isEnabled()
# A palette colour's token is its hex; a hex literal finds it, and a vector
# literal of the same colour does too.
t._set_view(WEB)
t.show_literal('"#0074c3"')
out["hex_literal"] = [t._view.currentText(), t.current_name()]
t._set_view(WEB)
t.show_literal("[0, 0.455, 0.765]")
out["vector_literal"] = t.current_name()
# "Edit palette list..." is an action: the view stays where it was.
opened = []
t._edit_palette_list = lambda: opened.append(True)
t._view.setCurrentText(EDIT_PALETTES)
app.processEvents()
out["edit_entry"] = [t._view.currentText(), opened, color_palettes.last_view()]
# + asks for a colour and a name; - removes the selected row; Paste adds lines.
QColorDialog.getColor = staticmethod(lambda *a, **k: QColor("#d4af37"))
QInputDialog.getText = staticmethod(lambda *a, **k: ("Gold", True))
t._add_color()
out["added"] = [t._palettes["Shelf"][-1], t.current_name()]
t.select("#0074c3")
t._remove_color()
t.paste_text("Silver  #c0c0c0\nnothing here\n")
out["saved"] = color_palettes.load_palettes()["Shelf"]
# Name and Hex are typed over in place; a hex it cannot read puts the old
# value back. Only a palette's Name and Hex are editable.
from PySide6.QtCore import Qt
def row_of(hexa):
    return next(r for r, row in enumerate(t._rows) if row[1] == hexa)
t._table.item(row_of("#d4af37"), 1).setText("Old Gold")
app.processEvents()
t._table.item(row_of("#c0c0c0"), 2).setText("1, 2, 3")
app.processEvents()
t._table.item(row_of("#c44188"), 2).setText("not a colour")
app.processEvents()
QColorDialog.getColor = staticmethod(lambda *a, **k: QColor("#00ff00"))
t._table.cellDoubleClicked.emit(row_of("#010203"), 0)      # the swatch: system panel
app.processEvents()
out["edited_selected"] = t.current_name()
t._set_view(WEB)
t._table.cellDoubleClicked.emit(0, 0)                      # not in a named view
t._set_view("Shelf")
out["edited"] = color_palettes.load_palettes()["Shelf"]
editable = lambda r, c: bool(t._table.item(r, c).flags() & Qt.ItemFlag.ItemIsEditable)
out["palette_editable"] = [editable(0, c) for c in range(4)]
t._set_view(WEB)
out["web_editable"] = [editable(0, c) for c in range(4)]
# The palette list editor: new, rename, delete; colours are not its business.
e = PaletteListEditor(color_palettes.load_palettes(), "Shelf")
e._ask_name = lambda *a: "Silk"
e._new()
e._ask_name = lambda *a: "Silk PLA"
e._rename()
out["list"] = [e._list.item(i).text() for i in range(e._list.count())]
out["current"] = e.current
out["kept"] = e.palettes["Shelf"] == color_palettes.load_palettes()["Shelf"]
print(json.dumps(out))
'''


def test_palettes_are_listed_inserted_as_hex_and_edited_in_the_pane(tmp_path):
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["views"] == ["Web colors", "XKCD colors", "All named colors", "", "Shelf", "",
                            "Edit palette list…"]
    assert out["opens_on_web"] and out["buttons_off"]
    assert out["xkcd_literal"] == ["XKCD colors", "xkcd:dark mint", None]
    assert sorted(out["rows"]) == ["Sunlu Cyan", "Sunlu Magenta"]
    assert out["remembered"] == "Shelf" and out["buttons_on"]
    assert out["hex_literal"] == ["Shelf", "#0074c3"]
    assert out["vector_literal"] == "#0074c3"
    assert out["edit_entry"] == ["Shelf", [True], "Shelf"]
    assert out["added"] == [["Gold", "#d4af37"], "#d4af37"]
    assert out["saved"] == [["Sunlu Magenta", "#c44188"], ["Gold", "#d4af37"], ["Silver", "#c0c0c0"]]
    assert out["edited"] == [["Sunlu Magenta", "#c44188"], ["Old Gold", "#d4af37"], ["Silver", "#00ff00"]]
    assert out["edited_selected"] == "#00ff00"
    assert out["palette_editable"] == [False, True, True, False]
    assert out["web_editable"] == [False, False, False, False]
    assert out["list"] == ["Shelf", "Silk PLA"] and out["current"] == "Silk PLA" and out["kept"]
