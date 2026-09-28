"""Help ▸ Color List — every name `color()` accepts, with its swatch.

A window rather than OpenSCAD's Color List dock. Two sets, as OpenSCAD's
parse_color reads them (and openscad_cpp_evaluator >= 1.29.0 with it):

- the CSS names: Qt's list (the colour picker's, `_PICKABLE_NAMES`) plus
  `rebeccapurple`, which Qt never adopted; `transparent` is left out, since
  its swatch would be a lie;
- the 949 xkcd survey names, listed -- and copied -- as `xkcd:<name>`,
  which is how color() takes them (`xkcd_colors.py`).

Sort by name, hue (the picker's 30-degree bands, dark to light), saturation,
chroma, lightness, value or luminance. Double-click a row (or press Copy Name) to
put `"name"` on the clipboard, ready to paste into `color(...)`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from belfryscad.window.color_names import SORT_KEYS, sorted_colors

_COLUMNS = ("", "Name", "Hex", "RGB")
_NAME_COL = 1


def color_rows(sort: str = "Hue") -> list:
    """(name, hex, rgb-vector text, QColor) per colour, in `sort` order."""
    rows = []
    for name, c in sorted_colors(sort):
        rgb = ", ".join(f"{v:.3g}" for v in (c.redF(), c.greenF(), c.blueF()))
        rows.append((name, c.name(), f"[{rgb}]", c))
    return rows


def matches(row: tuple, needle: str) -> bool:
    """Case-insensitive substring over the name and hex."""
    needle = needle.casefold().strip()
    return not needle or needle in row[0] or needle in row[1]


class ColorTable(QWidget):
    """Filter, Sort by, Hide xkcd colors and the swatch table, with a count
    under it. The Color List window and the colour picker are both built on
    it, so they list the same names in the same orders.

    `current_changed(name)` fires when the user moves the selection;
    `activated(name)` on a double-click. `select(name)` sets the selection
    from code without firing either."""

    current_changed = Signal(str)
    activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by name or hex…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        self._sort = QComboBox()
        self._sort.addItems(list(SORT_KEYS))
        self._sort.setCurrentText("Hue")
        self._sort.currentTextChanged.connect(self._fill)
        top = QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(QLabel("Sort by:"))
        top.addWidget(self._sort)
        layout.addLayout(top)
        self._hide_xkcd = QCheckBox("Hide xkcd colors")
        self._hide_xkcd.toggled.connect(lambda _on: self._apply_filter(self._search.text()))
        layout.addWidget(self._hide_xkcd)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_NAME_COL, QHeaderView.ResizeMode.Stretch)
        self._table.currentCellChanged.connect(self._on_current)
        self._table.itemDoubleClicked.connect(
            lambda item: self.activated.emit(self._rows[item.row()][0]))
        layout.addWidget(self._table)

        self._count = QLabel()
        layout.addWidget(self._count)
        self._quiet = False
        self._fill(self._sort.currentText())

    def current_name(self) -> str | None:
        row = self._table.currentRow()
        return self._rows[row][0] if 0 <= row < len(self._rows) else None

    def select(self, name: str | None):
        """Select `name`'s row (or nothing) without firing current_changed."""
        self._quiet = True
        try:
            rows = [r for r, row in enumerate(self._rows) if row[0] == name]
            if rows:
                self._table.setCurrentCell(rows[0], _NAME_COL)
                self._table.scrollToItem(self._table.item(rows[0], _NAME_COL))
            else:
                self._table.setCurrentCell(-1, -1)
                self._table.clearSelection()
        finally:
            self._quiet = False

    def _on_current(self, row, _col, _prev_row, _prev_col):
        if not self._quiet and 0 <= row < len(self._rows):
            self.current_changed.emit(self._rows[row][0])

    def _fill(self, sort: str):
        """(Re)build the table in `sort` order, keeping the filter and the
        selected name.

        Auto-sized columns are switched off while the cells go in and sized
        once at the end: ResizeToContents re-measures every row each time a
        cell is set, which made a re-sort of 1,097 rows take 85 s."""
        keep = self.current_name() if hasattr(self, "_rows") else None
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._quiet = True
        self._rows = color_rows(sort)
        self._table.setRowCount(len(self._rows))
        for r, (name, hexa, rgb, color) in enumerate(self._rows):
            swatch = QTableWidgetItem("      ")
            swatch.setBackground(color)
            self._table.setItem(r, 0, swatch)
            for c, text in enumerate((name, hexa, rgb), start=1):
                self._table.setItem(r, c, QTableWidgetItem(text))
        self._quiet = False
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_NAME_COL, QHeaderView.ResizeMode.Stretch)
        self._table.scrollToTop()
        self._apply_filter(self._search.text())
        if keep:
            self.select(keep)

    def _apply_filter(self, needle: str):
        # Row r is self._rows[r]: the table is only ever ordered by _fill,
        # never by clicking a header, so the two cannot drift apart.
        shown = 0
        hide_xkcd = self._hide_xkcd.isChecked()
        for r, row in enumerate(self._rows):
            hit = matches(row, needle) and not (hide_xkcd and row[0].startswith("xkcd:"))
            self._table.setRowHidden(r, not hit)
            shown += hit
        total = len(self._rows)
        self._count.setText(f"{total} colors" if shown == total else f"{shown} of {total} colors")


class ColorListDialog(QDialog):
    """Modeless, so a name can be copied while the editor stays reachable."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Color List")
        self.resize(520, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Colors <b>color()</b> accepts by name. Double-click a row to copy it."))
        self.colors = ColorTable()
        self.colors.activated.connect(lambda _name: self._copy_selected())
        layout.addWidget(self.colors)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_btn = buttons.addButton("Copy Name", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(self._copy_selected)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def _copy_selected(self):
        name = self.colors.current_name()
        if name:
            QApplication.clipboard().setText(f'"{name}"')


def show_color_list(parent=None):
    """Open the Color List. Modeless and self-deleting."""
    dlg = ColorListDialog(parent)
    dlg.show()
    return dlg
