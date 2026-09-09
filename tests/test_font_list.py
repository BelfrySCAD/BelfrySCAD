"""Help ▸ Font List (#379).

The point of the feature is that these are the names `text()` accepts,
not the names a system font dialog shows -- "All too often I specify an
interesting font only to get the default Liberation Sans instead."

The filter and the data source are plain functions and test directly; the
table itself is a widget and is driven in a subprocess.
"""
import json
import subprocess
import sys

import pytest

from belfryscad.window.font_list import load_fonts, matches


FAKE = [
    {"family": "Liberation Sans", "style": "Regular", "spec": "Liberation Sans", "path": "<bundled>"},
    {"family": "Liberation Sans", "style": "Bold", "spec": "Liberation Sans:style=Bold", "path": "<bundled>"},
    {"family": "Helvetica Neue", "style": "Italic", "spec": "Helvetica Neue:style=Italic", "path": "/f.ttc"},
]


def test_the_list_comes_from_the_evaluator_not_qt():
    """A Qt font list would name faces the way the system does, which is
    the mismatch this feature exists to cure."""
    fonts = load_fonts()
    assert fonts, "no fonts, not even the bundled ones"
    for f in fonts:
        assert {"family", "style", "spec", "path"} <= set(f)
    # The bundled family is always there, whatever the machine has.
    bundled = {f["style"] for f in fonts if f["family"] == "Liberation Sans"}
    assert {"Regular", "Bold", "Italic", "Bold Italic"} <= bundled


def test_the_spec_column_is_what_you_paste():
    fonts = {(f["family"], f["style"]): f["spec"] for f in load_fonts()}
    # Regular is the bare family -- the longer form would teach a habit
    # nobody needs.
    assert fonts[("Liberation Sans", "Regular")] == "Liberation Sans"
    assert fonts[("Liberation Sans", "Bold")] == "Liberation Sans:style=Bold"


@pytest.mark.parametrize("needle,expected", [
    ("", 3),
    ("liberation", 2),
    ("LIBERATION", 2),      # case-insensitive
    ("bold", 1),            # matches on style, not just family
    ("neue", 1),
    ("nothing here", 0),
])
def test_the_filter_matches_family_and_style(needle, expected):
    assert sum(matches(f, needle) for f in FAKE) == expected


def test_the_filter_does_not_double_count_the_spec():
    """The spec is built from family and style, so matching it too would
    only ever repeat a hit -- and would make ':style=' a query."""
    assert matches(FAKE[1], "style=") is False


def test_a_too_old_evaluator_costs_the_list_not_the_menu(monkeypatch):
    """An out-of-date wheel should leave Help working."""
    import builtins
    real_import = builtins.__import__

    def fail(name, *args, **kwargs):
        if name == "openscad_cpp_evaluator":
            raise ImportError("no list_fonts here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail)
    assert load_fonts() == []


def test_the_dialog_lists_filters_and_copies(tmp_path):
    """Driven in a subprocess: widgets crash the pytest runner here."""
    driver = tmp_path / "_fl.py"
    driver.write_text('''
import json
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.window.font_list import FontListDialog

FAKE = [
    {"family": "Liberation Sans", "style": "Regular", "spec": "Liberation Sans", "path": "<bundled>"},
    {"family": "Liberation Sans", "style": "Bold", "spec": "Liberation Sans:style=Bold", "path": "<bundled>"},
    {"family": "Helvetica Neue", "style": "Italic", "spec": "Helvetica Neue:style=Italic", "path": "/f.ttc"},
]
dlg = FontListDialog(None, fonts=FAKE)
out = {"title": dlg.windowTitle(), "rows": dlg._table.rowCount(), "count": dlg._count.text()}

dlg._search.setText("neue")
out["hidden_after_filter"] = [dlg._table.isRowHidden(r) for r in range(dlg._table.rowCount())]
out["count_filtered"] = dlg._count.text()

# Filtering must survive a sort: the rows move, the source list does not.
dlg._search.setText("")
dlg._table.sortItems(0)
dlg._search.setText("helvetica")
visible = [dlg._table.item(r, 0).text() for r in range(dlg._table.rowCount())
           if not dlg._table.isRowHidden(r)]
out["visible_after_sort"] = visible

dlg._search.setText("")
dlg._table.setCurrentCell(0, 0)
dlg._copy_selected()
out["clipboard"] = QApplication.clipboard().text()
out["spec_col_of_row0"] = dlg._table.item(0, 2).text()
print(json.dumps(out))
''')
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                          env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                               "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["title"] == "Font List"
    assert out["rows"] == 3
    assert out["count"] == "3 fonts"
    assert out["hidden_after_filter"].count(False) == 1
    assert out["count_filtered"] == "1 of 3 fonts"
    # The row that survives a filter applied AFTER sorting is the right one.
    assert out["visible_after_sort"] == ["Helvetica Neue"]
    assert out["clipboard"] == out["spec_col_of_row0"]


def test_the_sample_sentence_is_a_pangram_with_digits_and_stable():
    from belfryscad.window.font_list import PANGRAMS, sample_text_for
    s = sample_text_for("Liberation Sans")
    assert s == sample_text_for("Liberation Sans")          # same face, same sentence
    assert s.endswith(" 0123456789")
    assert s[:-len(" 0123456789")] in PANGRAMS
    letters = {c for c in s.casefold() if c.isalpha()}
    assert letters >= set("abcdefghijklmnopqrstuvwxyz")      # it really is a pangram


def test_the_sample_column_paints_each_row_in_its_own_face(tmp_path):
    """Driven in a subprocess: widgets crash the pytest runner here. The
    grab() is what runs the delegate's paint() over the visible rows."""
    driver = tmp_path / "_fs.py"
    driver.write_text('''
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.window.font_list import FontListDialog, _SAMPLE_COL, sample_text_for
FAKE = [
    {"family": "Liberation Sans", "style": "Regular", "spec": "Liberation Sans", "path": "<bundled>"},
    {"family": "Liberation Sans", "style": "Bold", "spec": "Liberation Sans:style=Bold", "path": "<bundled>"},
]
dlg = FontListDialog(None, fonts=FAKE)
dlg.show(); app.processEvents()
pix = dlg.grab()
out = {"columns": dlg._table.columnCount(),
       "sample_item_text": dlg._table.item(0, _SAMPLE_COL).text(),
       "sample_item_font": dlg._table.item(0, _SAMPLE_COL).data(Qt.ItemDataRole.UserRole)["style"],
       "painted": not pix.isNull() and pix.width() > 0,
       "row_height": dlg._table.rowHeight(0)}
print(json.dumps(out))
''')
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                          env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                               "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["columns"] == 4
    assert out["sample_item_text"] == ""            # the delegate paints it; nothing to sort or filter on
    assert out["sample_item_font"] == "Regular"
    assert out["painted"]
    assert out["row_height"] >= 32                  # room for a 16pt sample


def test_private_dot_families_are_hidden_from_the_gui(monkeypatch):
    """macOS's `.SF NS`-style faces are not offered (issue #402), and the
    filter sits in load_fonts(), which both the list and the picker use."""
    import openscad_cpp_evaluator
    from belfryscad.window.font_list import is_private_family
    fake = [
        {"family": ".Al Bayan PUA", "style": "Regular", "spec": ".Al Bayan PUA", "path": "/p1"},
        {"family": "Al Bayan", "style": "Regular", "spec": "Al Bayan", "path": "/p2"},
        {"family": ".SF NS", "style": "Bold", "spec": ".SF NS:style=Bold", "path": "/p3"},
    ]
    monkeypatch.setattr(openscad_cpp_evaluator, "list_fonts", lambda: fake)
    assert [f["family"] for f in load_fonts()] == ["Al Bayan"]
    assert is_private_family(".SF NS") and not is_private_family("SF Pro")
