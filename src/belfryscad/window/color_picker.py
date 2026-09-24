"""Choose Color… -- edit a colour literal in the editor.

Right-click a literal `color_literals.find_color_literal` recognises and the
context menu offers **Choose Color…**: a named-colour list, a `#rgb` /
`#rrggbb` field, and **Other Colors…** for the platform's native colour
panel. Save writes the literal back through `replace_span`, as
**Choose Font…** does.

The value goes back in the literal's own shape. A string stays a string:
it is written the way the user picked it -- the name chosen from the list,
or the hex exactly as typed (`#fff` stays short) -- and a colour from the
native panel is written as its name if the literal was a name and the new
colour has one, otherwise `#rrggbb`. A vector stays a vector of 0..1
components, keeping its alpha if it had four: `thecolor = [...]` may be fed
to `concat()` or indexed elsewhere, so turning it into a string could break
the script.

`literal_for()` is the whole of that rule and has no widget in it, so
`tests/test_color_picker.py` tests it directly.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QLineEdit, QPushButton, QVBoxLayout,
)

from belfryscad.window.color_literals import _HEX, color_for_name, name_for


def literal_for(original: str, color: QColor, spelling: str | None = None) -> str:
    """The source text to replace `original` (a quoted string or a vector
    literal, as `find_color_literal` spans it) with, for `color`.

    `spelling` is how the user chose it -- a name or the hex they typed --
    or None when it came from the native panel.
    """
    if original.startswith("["):
        comps = [color.redF(), color.greenF(), color.blueF()]
        if original.count(",") >= 3:
            comps.append(color.alphaF())
        return "[" + ", ".join(_component(v) for v in comps) + "]"
    if spelling is None:
        was_name = not original.startswith('"#')
        spelling = (name_for(color) if was_name else None) or color.name(QColor.NameFormat.HexRgb)
    return f'"{spelling}"'


def _component(v: float) -> str:
    """0..1 to three decimals, without trailing zeros: 0.502, 1, 0."""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s or "0"


def open_color_picker(original: str, color: QColor, on_commit, parent=None):
    """Show the picker; call `on_commit(new_literal_text)` on Save."""
    dlg = ColorPickerDialog(original, color, parent)
    if dlg.exec() and dlg.chosen_literal() != original:
        on_commit(dlg.chosen_literal())
    return dlg


class ColorPickerDialog(QDialog):
    def __init__(self, original: str, color: QColor, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Color")
        self._original = original
        self._color = QColor(color)
        self._initial = QColor(color)
        self._spelling: str | None = None
        self._updating = False
        self._has_alpha = original.startswith("[") and original.count(",") >= 3

        layout = QVBoxLayout(self)
        self._swatch = QFrame()
        self._swatch.setMinimumSize(220, 48)
        self._swatch.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self._swatch)

        form = QFormLayout()
        self._names = QComboBox()
        self._names.setEditable(True)            # type to jump through 147 names
        self._names.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._names.addItem("")                  # shown when the colour has no name
        for name in _PICKABLE_NAMES:
            pm = QPixmap(14, 14)
            pm.fill(QColor(name))
            self._names.addItem(QIcon(pm), name)
        self._names.textActivated.connect(self._on_name)
        form.addRow("Name:", self._names)

        self._hex = QLineEdit()
        self._hex.setPlaceholderText("#rgb or #rrggbb")
        self._hex.textEdited.connect(self._on_hex)
        form.addRow("Hex:", self._hex)
        layout.addLayout(form)

        other = QPushButton("Other Colors…")
        other.clicked.connect(self._on_native)
        layout.addWidget(other, alignment=Qt.AlignmentFlag.AlignLeft)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show(self._color, from_hex=False)

    def chosen_literal(self) -> str:
        if self._spelling is None and self._color == self._initial:
            return self._original                # opened and saved unchanged
        return literal_for(self._original, self._color, self._spelling)

    def _keep_alpha(self, c: QColor) -> QColor:
        c = QColor(c)
        c.setAlphaF(self._color.alphaF())
        return c

    def _on_name(self, name: str):
        c = color_for_name(name.strip())
        if c is None:
            return
        self._spelling = name.strip()
        self._show(self._keep_alpha(c), from_hex=False)

    def _on_hex(self, text: str):
        text = text.strip()
        if not _HEX.match(text):
            return
        self._spelling = text
        self._show(self._keep_alpha(QColor.fromString(text)), from_hex=True)

    def _on_native(self):
        opts = QColorDialog.ColorDialogOption(0)
        if self._has_alpha:
            opts |= QColorDialog.ColorDialogOption.ShowAlphaChannel
        c = QColorDialog.getColor(self._color, self, "Choose Color", opts)
        if c.isValid():
            self._spelling = None
            self._show(c if self._has_alpha else self._keep_alpha(c), from_hex=False)

    def _show(self, c: QColor, from_hex: bool):
        self._color = c
        self._swatch.setStyleSheet(f"background-color: {c.name(QColor.NameFormat.HexRgb)};")
        if self._updating:
            return
        self._updating = True
        try:
            name = self._spelling if (self._spelling and not self._spelling.startswith("#")) \
                else name_for(c)
            self._names.setCurrentText(name or "")
            if not from_hex:
                self._hex.setText(c.name(QColor.NameFormat.HexRgb))
        finally:
            self._updating = False



#: Qt's names, minus `transparent`: as an rgb it is black, and offering it
#: would write a name that does not mean what the swatch shows.
_PICKABLE_NAMES = [n for n in QColor.colorNames() if n != "transparent"]


