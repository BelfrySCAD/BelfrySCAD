"""Help ▸ Color List — every name `color()` accepts, with its swatch.

A window rather than OpenSCAD's Color List dock. The names are the colour
picker's own list (`color_picker._PICKABLE_NAMES`): Qt's, which is the
table the evaluator's `css_colors.cpp` was generated from, so a name shown
here is a name `color()` resolves. Hue order, as in the picker, so similar
colours sit together rather than `aliceblue` beside `antiquewhite`.

Double-click a row (or press Copy) to put `"name"` on the clipboard, quoted
and ready to paste into `color(...)`.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from belfryscad.window.color_picker import _PICKABLE_NAMES

_COLUMNS = ("", "Name", "Hex", "RGB")
_NAME_COL = 1


def color_rows() -> list:
    """(name, hex, rgb-vector text) per colour, in hue order."""
    rows = []
    for name in _PICKABLE_NAMES:
        c = QColor(name)
        rgb = ", ".join(f"{v:.3g}" for v in (c.redF(), c.greenF(), c.blueF()))
        rows.append((name, c.name(), f"[{rgb}]"))
    return rows


def matches(row: tuple, needle: str) -> bool:
    """Case-insensitive substring over the name and hex."""
    needle = needle.casefold().strip()
    return not needle or needle in row[0] or needle in row[1]


class ColorListDialog(QDialog):
    """Modeless, so a name can be copied while the editor stays reachable."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Color List")
        self.resize(460, 600)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._rows = color_rows()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Colors <b>color()</b> accepts by name. Double-click a row to copy it."))

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by name or hex…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._table = QTableWidget(len(self._rows), len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_NAME_COL, QHeaderView.ResizeMode.Stretch)
        for r, (name, hexa, rgb) in enumerate(self._rows):
            swatch = QTableWidgetItem("      ")
            swatch.setBackground(QColor(name))
            self._table.setItem(r, 0, swatch)
            for c, text in enumerate((name, hexa, rgb), start=1):
                self._table.setItem(r, c, QTableWidgetItem(text))
        self._table.itemDoubleClicked.connect(lambda _item: self._copy_selected())
        layout.addWidget(self._table)

        self._count = QLabel()
        row = QHBoxLayout()
        row.addWidget(self._count)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_btn = buttons.addButton("Copy Name", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(self._copy_selected)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._apply_filter("")

    def _apply_filter(self, needle: str):
        # ponytail: rows are never sorted, so row r is always self._rows[r].
        shown = 0
        for r, row in enumerate(self._rows):
            hit = matches(row, needle)
            self._table.setRowHidden(r, not hit)
            shown += hit
        total = len(self._rows)
        self._count.setText(f"{total} colors" if shown == total else f"{shown} of {total} colors")

    def _copy_selected(self):
        row = self._table.currentRow()
        if row >= 0:
            QApplication.clipboard().setText(f'"{self._rows[row][0]}"')


def show_color_list(parent=None):
    """Open the Color List. Modeless and self-deleting."""
    dlg = ColorListDialog(parent)
    dlg.show()
    return dlg
