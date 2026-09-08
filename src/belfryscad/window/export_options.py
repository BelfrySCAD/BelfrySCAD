"""Per-export options, asked once the file name and format are known.

These used to live in Preferences ▸ Export. They went there because a
native macOS save dialog cannot host extra controls, but it put PDF's
paper size three menus away from the Export command that uses it, and left
no hint that the setting existed at all. They are now asked where they
apply: after the format is chosen, and only for a format that has any.

The saved preferences did not go away -- they are the DEFAULTS the dialog
opens on, and each export writes its choices back, so a second export in
the same session opens on what the first one used.

The field list is a plain data structure with no Qt in it, so what each
format offers is testable without a widget; the dialog only renders it.
"""
from dataclasses import dataclass

# Formats that can hold more than one object in a file.
_MULTI_OBJECT = (".3mf", ".amf", ".obj", ".ply", ".wrl", ".x3d")

PDF_PAPER_SIZES = ("a6", "a5", "a4", "a3", "letter", "legal", "tabloid")
PDF_ORIENTATIONS = ("portrait", "landscape", "auto")


@dataclass(frozen=True)
class Field:
    """One control: which preference it reads and writes, and how to draw it."""
    key: str
    kind: str               # "bool" | "choice" | "float"
    label: str
    caption: str = ""       # checkbox text; the label sits in the form's left column
    choices: tuple = ()
    choice_labels: tuple = ()
    tip: str = ""
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 0.05
    decimals: int = 2


def export_fields(ext: str) -> list:
    """The options that apply to `ext`, in the order to show them.

    Empty for a format with nothing to choose (.off, and .stl's siblings
    that carry no switches), which is the signal not to ask at all.
    """
    ext = ext.lower()
    if ext == ".pdf":
        return [
            Field("export/pdfPaperSize", "choice", "Paper size:",
                  choices=PDF_PAPER_SIZES,
                  choice_labels=tuple(n.upper() if n.startswith("a") else n.capitalize()
                                      for n in PDF_PAPER_SIZES)),
            Field("export/pdfOrientation", "choice", "Orientation:",
                  choices=PDF_ORIENTATIONS,
                  choice_labels=tuple(n.capitalize() for n in PDF_ORIENTATIONS),
                  tip="Auto turns the page sideways when the model is wider than it is tall."),
            Field("export/pdfShowScale", "bool", "Scale:", caption="Ruler and calibration caption",
                  tip="Draws a millimetre ruler along two edges, labelled in MODEL\n"
                      "coordinates, with a caption explaining what to measure. That is\n"
                      "what lets a printed page show how far off a printer's scaling is."),
            Field("export/pdfShowGrid", "bool", "Grid:", caption="Grid over the page"),
        ]
    if ext == ".svg":
        return [
            Field("export/svgStrokeWidth", "float", "Stroke width:",
                  minimum=0.0, maximum=10.0, step=0.05, decimals=2,
                  tip="Millimetres. Also pads the page, so a wider stroke makes a\n"
                      "larger document. 0 with stroke off leaves an unstroked outline."),
            Field("export/svgFill", "bool", "Fill:", caption="Fill the shape",
                  tip="Off (the default) writes outlines only, which is what a cutter\n"
                      "or plotter wants."),
        ]
    if ext == ".stl":
        return [
            Field("export/stlAscii", "bool", "Encoding:", caption="ASCII instead of binary",
                  tip="Binary is smaller and reads faster everywhere. ASCII is human\n"
                      "readable, and what some older toolchains expect."),
        ]
    if ext in _MULTI_OBJECT:
        return [
            Field("export/splitComponents", "bool", "Multi-object:",
                  caption="Separate object per disconnected piece",
                  tip="Off (the default, and what OpenSCAD writes) keeps a model in one\n"
                      "object however many disjoint pieces it is in. On gives every piece\n"
                      "its own, which a slicer then lists separately."),
        ]
    return []


def export_kwargs(ext: str, values: dict, design_filename: str = "") -> dict:
    """Turn the dialog's answers into `exporters.export_model` keyword
    arguments.

    `values` is keyed by preference key, as `export_fields` names them.
    Anything the chosen format does not use is simply absent -- passing
    PDF's paper size to an STL export would be meaningless, not an error.
    """
    ext = ext.lower()
    if ext == ".pdf":
        return {"pdf_options": {
            "paper-size": values["export/pdfPaperSize"],
            "orientation": values["export/pdfOrientation"],
            "show-scale": bool(values["export/pdfShowScale"]),
            "show-grid": bool(values["export/pdfShowGrid"]),
            "design-filename": design_filename,
            "show-filename": bool(design_filename),
        }}
    if ext == ".svg":
        return {"svg_fill": bool(values["export/svgFill"]),
                "svg_stroke_width": float(values["export/svgStrokeWidth"])}
    if ext == ".stl":
        return {"ascii_stl": bool(values["export/stlAscii"])}
    if ext in _MULTI_OBJECT:
        return {"split_components": bool(values["export/splitComponents"])}
    return {}


def saved_values(ext: str) -> dict:
    """What `export_fields(ext)` currently defaults to, from preferences."""
    from belfryscad.window.preferences import load_preference

    types = {"bool": bool, "float": float, "choice": str}
    return {f.key: load_preference(f.key, type_=types[f.kind]) for f in export_fields(ext)}


def remember(values: dict) -> None:
    """Make this export's choices the next one's defaults."""
    from belfryscad.window.preferences import app_settings

    s = app_settings()
    for key, value in values.items():
        s.setValue(key, value)


def _build_dialog(ext: str, parent, filename: str):
    """(dialog, values_fn) for `ext`. Qt is imported here so this module
    costs nothing in a headless process."""
    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
        QVBoxLayout,
    )

    fields = export_fields(ext)
    defaults = saved_values(ext)

    dlg = QDialog(parent)
    dlg.setWindowTitle(f"{ext.lstrip('.').upper()} Export Options")
    layout = QVBoxLayout(dlg)
    if filename:
        layout.addWidget(QLabel(f"Exporting {filename}"))
    form = QFormLayout()
    layout.addLayout(form)

    widgets = {}
    for f in fields:
        if f.kind == "bool":
            w = QCheckBox(f.caption)
            w.setChecked(bool(defaults[f.key]))
        elif f.kind == "choice":
            w = QComboBox()
            w.addItems(list(f.choice_labels or f.choices))
            try:
                w.setCurrentIndex(list(f.choices).index(defaults[f.key]))
            except ValueError:
                w.setCurrentIndex(0)
        else:
            w = QDoubleSpinBox()
            w.setRange(f.minimum, f.maximum)
            w.setSingleStep(f.step)
            w.setDecimals(f.decimals)
            w.setValue(float(defaults[f.key]))
        if f.tip:
            w.setToolTip(f.tip)
        form.addRow(f.label, w)
        widgets[f.key] = (f, w)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    def values():
        out = {}
        for key, (f, w) in widgets.items():
            if f.kind == "bool":
                out[key] = w.isChecked()
            elif f.kind == "choice":
                out[key] = f.choices[w.currentIndex()]
            else:
                out[key] = w.value()
        return out

    return dlg, values


def ask_export_options(ext: str, parent=None, filename: str = "") -> dict | None:
    """Ask for `ext`'s options, seeded from the saved preferences.

    Returns the values (empty, and showing nothing, for a format with no
    options), or None if the user cancelled -- which cancels the export
    too: they have already named the file, and writing one with settings
    they backed out of would be worse than not writing it.
    """
    if not export_fields(ext):
        return {}
    dlg, values_of = _build_dialog(ext, parent, filename)
    if not dlg.exec():
        return None
    values = values_of()
    remember(values)
    return values
