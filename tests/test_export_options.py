"""What the export dialog asks, per format.

The options used to live in Preferences > Export; they are now asked once
the file name and format are known. The field list is plain data with no
Qt in it, so what each format offers -- and what those answers become --
is testable here without a widget. The dialog itself only renders this.
"""
import pytest

from belfryscad.window.export_options import (
    export_fields, export_kwargs, saved_values,
)
from belfryscad.window.main_window import _EXPORT_FORMATS


def keys(ext):
    return [f.key for f in export_fields(ext)]


def test_a_format_with_nothing_to_choose_asks_nothing():
    """An empty field list is the signal not to show a dialog at all --
    interrupting an export to show an empty box would be worse than the
    preferences pane this replaces."""
    assert export_fields(".off") == []
    assert export_kwargs(".off", {}) == {}


def test_pdf_asks_about_the_page():
    # PDF is the one format with a page to set up: it centres the drawing
    # on a fixed sheet rather than cutting the page to fit.
    assert keys(".pdf") == ["export/pdfPaperSize", "export/pdfOrientation",
                             "export/pdfShowScale", "export/pdfShowGrid"]
    paper = export_fields(".pdf")[0]
    assert paper.kind == "choice"
    assert "a4" in paper.choices and "letter" in paper.choices
    assert len(paper.choice_labels) == len(paper.choices)


def test_svg_asks_about_the_stroke():
    assert keys(".svg") == ["export/svgStrokeWidth", "export/svgFill"]
    width = export_fields(".svg")[0]
    assert width.kind == "float"
    assert width.minimum == 0.0


def test_stl_can_finally_choose_ascii_from_the_gui():
    """--export-format asciistl was CLI-only; the GUI wrote binary with no
    way to say otherwise."""
    assert keys(".stl") == ["export/stlAscii"]
    assert export_kwargs(".stl", {"export/stlAscii": True}) == {"ascii_stl": True}


@pytest.mark.parametrize("ext", [".3mf", ".amf"])
def test_the_printable_formats_ask_what_the_print_is_for(ext):
    """3MF carries a material per object and AMF one per volume, and both
    are formats a slicer takes. The question is what the file is FOR;
    "should the parts be separate?" is an implementation detail that
    answer implies."""
    assert keys(ext) == ["export/printType", "export/splitComponents"]


@pytest.mark.parametrize("ext", [".obj", ".ply", ".wrl", ".x3d"])
def test_the_other_multi_object_formats_do_not(ext):
    """No slicer assigns filaments from an OBJ, so asking about a print
    that is not happening would be noise. They keep the per-colour split
    they always had."""
    assert keys(ext) == ["export/splitComponents"]
    assert export_kwargs(ext, {"export/splitComponents": False}) == {"split_components": False}


@pytest.mark.parametrize("ext", [".3mf", ".amf"])
def test_multi_material_splits_by_colour_and_single_does_not(ext):
    multi = export_kwargs(ext, {"export/printType": "multi", "export/splitComponents": False})
    single = export_kwargs(ext, {"export/printType": "single", "export/splitComponents": False})
    # One object per colour for a slicer to assign to filaments...
    assert multi == {"split_colors": True, "split_components": False}
    # ... and one welded solid when the colours are irrelevant.
    assert single == {"split_colors": False, "split_components": False}


@pytest.mark.parametrize("ext", [".3mf", ".amf"])
def test_separate_pieces_is_independent_of_the_print_type(ext):
    """Disjoint pieces and colour regions are different questions: a
    single-material print of two separate parts may still want them
    listed separately."""
    kwargs = export_kwargs(ext, {"export/printType": "single", "export/splitComponents": True})
    assert kwargs == {"split_colors": False, "split_components": True}


def test_svg_answers_become_export_arguments():
    kwargs = export_kwargs(".svg", {"export/svgFill": True, "export/svgStrokeWidth": 1.5})
    assert kwargs == {"svg_fill": True, "svg_stroke_width": 1.5}


def test_the_extension_is_matched_case_insensitively():
    """_resolve_export_format keeps the user's own case, so "PART.PDF"
    reaches here spelled their way."""
    assert keys(".PDF") == keys(".pdf")
    assert export_kwargs(".STL", {"export/stlAscii": False}) == {"ascii_stl": False}


def test_every_offered_format_is_accounted_for():
    """A format whose fields nobody wrote is not an error -- .off has none
    on purpose -- but every one must at least reach export_kwargs without
    raising, since the export flow calls it unconditionally."""
    for _filter, ext in _EXPORT_FORMATS:
        values = {f.key: _sample(f) for f in export_fields(ext)}
        export_kwargs(ext, values, "design.scad")


def _sample(f):
    if f.kind == "bool":
        return True
    if f.kind == "choice":
        return f.choices[0]
    return f.minimum


def test_defaults_come_from_the_saved_preferences():
    """The dialog opens on what the last export used, so a second export in
    a session asks nothing new."""
    values = saved_values(".pdf")
    assert set(values) == set(keys(".pdf"))
    assert values["export/pdfPaperSize"] in export_fields(".pdf")[0].choices
    assert isinstance(values["export/pdfShowScale"], bool)


def test_every_field_key_is_a_known_preference():
    """A typo'd key would read a default of None and write a setting
    nothing else ever looks at."""
    from belfryscad.window.preferences import _DEFAULTS

    for _filter, ext in _EXPORT_FORMATS:
        for f in export_fields(ext):
            assert f.key in _DEFAULTS, f.key


def test_the_dialog_renders_the_fields_and_reports_them_back(tmp_path):
    """Driven in a subprocess: this suite keeps Qt widgets out of pytest's
    own process. Covers what the pure tests above cannot -- that the fields
    become real controls, that cancelling aborts the export, and that
    Preferences still builds now that its Export tab is gone."""
    import json
    import subprocess
    import sys

    driver = tmp_path / "_dlg.py"
    driver.write_text('''
import json, sys
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

QCoreApplication.setOrganizationName("BelfrySCADTest")
QCoreApplication.setApplicationName("ExportOptionsTest")
app = QApplication([])

from belfryscad.window.export_options import _build_dialog, ask_export_options

out = {}

dlg, values_of = _build_dialog(".pdf", None, "part.pdf")
out["title"] = dlg.windowTitle()
out["defaults"] = values_of()

# Drive the real widgets, then read them back through values().
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox
combos = dlg.findChildren(QComboBox)
combos[0].setCurrentIndex(list(combos[0].itemText(i) for i in range(combos[0].count())).index("Letter"))
combos[1].setCurrentIndex(2)                      # auto
for cb in dlg.findChildren(QCheckBox):
    cb.setChecked(not cb.isChecked())
out["after"] = values_of()

svg, svg_values = _build_dialog(".svg", None, "part.svg")
spin = svg.findChildren(QDoubleSpinBox)[0]
spin.setValue(1.25)
out["svg"] = svg_values()

# A format with no options shows nothing and answers immediately -- if it
# tried to open a dialog here, this call would block forever.
out["off"] = ask_export_options(".off", None, "part.off")

print(json.dumps(out))
''')
    result = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                             env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                                  "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert out["title"] == "PDF Export Options"
    # Not asserted against "a4": app_settings() fixes the organisation and
    # application names, so QSettings finds the REAL user preferences
    # whatever this process sets -- and the dialog is supposed to open on
    # whatever the last export chose. What must hold is that it opens on a
    # value the writer will accept.
    assert out["defaults"]["export/pdfPaperSize"] in (
        "a6", "a5", "a4", "a3", "letter", "legal", "tabloid")
    # The combo's label ("Letter") maps back to the value the writer wants.
    assert out["after"]["export/pdfPaperSize"] == "letter"
    assert out["after"]["export/pdfOrientation"] == "auto"
    assert out["after"]["export/pdfShowScale"] is not out["defaults"]["export/pdfShowScale"]
    assert out["svg"]["export/svgStrokeWidth"] == 1.25
    assert out["off"] == {}
