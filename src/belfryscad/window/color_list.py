"""Help ▸ Color List — every name `color()` accepts, with its swatch.

A window rather than OpenSCAD's Color List dock. Two sets, as OpenSCAD's
parse_color reads them (and openscad_cpp_evaluator >= 1.29.0 with it):

- the CSS names: Qt's list (the colour picker's, `_PICKABLE_NAMES`) plus
  `rebeccapurple`, which Qt never adopted; `transparent` is left out, since
  its swatch would be a lie;
- the 949 xkcd survey names, listed -- and copied -- as `xkcd:<name>`,
  which is how color() takes them (`xkcd_colors.py`).

**Colors:** shows one set at a time -- Web colors (the default), XKCD colors,
All named colors -- or one of your own palettes (`color_palettes`).

Sort by name, hue (the picker's 30-degree bands, dark to light), saturation,
chroma, lightness, value or luminance. Double-click a row (or press Copy Name) to
put `"name"` on the clipboard, ready to paste into `color(...)`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QStyledItemDelegate, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from belfryscad.window import color_palettes
from belfryscad.window.color_names import SORT_KEYS, sorted_colors

#: The chooser's built-in views of the names color() takes, before your own
#: palettes. Web is the CSS names (plus rebeccapurple); XKCD the 949 survey
#: names as `xkcd:<name>`; All both, so one filter searches everything.
WEB, XKCD, ALL = "Web colors", "XKCD colors", "All named colors"
BUILTIN_VIEWS = (WEB, XKCD, ALL)

_COLUMNS = ("", "Name", "Hex", "RGB")
_NAME_COL = 1


def color_rows(sort: str = "Hue", palette: list | None = None) -> list:
    """(name, hex, rgb-vector text, QColor) per colour, in `sort` order: the
    named colours, or a user palette's (name, "#rrggbb") entries."""
    if palette is None:
        pairs = sorted_colors(sort)
    else:
        pairs = sorted(((n, QColor(h)) for n, h in palette), key=lambda kv: SORT_KEYS[sort](*kv))
    rows = []
    for name, c in pairs:
        rgb = ", ".join(f"{v:.3g}" for v in (c.redF(), c.greenF(), c.blueF()))
        rows.append((name, c.name(), f"[{rgb}]", c))
    return rows


def matches(row: tuple, needle: str) -> bool:
    """Case-insensitive substring over the name and hex."""
    needle = needle.casefold().strip()
    return not needle or needle in row[0].casefold() or needle in row[1]


class _SwatchDelegate(QStyledItemDelegate):
    """The swatch cell in its own colour, selected or not. The default
    delegate paints a selected cell in the selection colour, so the one row
    you were looking at showed the wrong colour."""

    def paint(self, painter, option, index):
        painter.fillRect(option.rect, index.data(Qt.ItemDataRole.BackgroundRole))


class ColorTable(QWidget):
    """Colors (view), Filter, Sort by and the swatch table, with
    a count under it. The Color List window and the colour picker are both
    built on it, so they list the same names in the same orders.

    `current_changed(token)` fires when the user moves the selection;
    `activated(token)` on a double-click. A token is what `color()` takes for
    the row: its name, or for a user palette's colour (a name of the user's
    own) its hex. `select(token)` sets the selection from code without
    firing either."""

    current_changed = Signal(str)
    activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._palettes = color_palettes.load_palettes()
        self._view = QComboBox()
        self._view.setToolTip("The named colors color() takes, or one of your own palettes")
        # Remembered only when the user picks one, never on opening.
        self._view.currentTextChanged.connect(
            lambda text: (self._on_view(text), color_palettes.set_last_view(text)))
        edit = QPushButton("Palettes…")
        edit.setToolTip("Make and edit your own palettes -- your filament colors, say")
        edit.clicked.connect(self._edit_palettes)
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("Colors:"))
        view_row.addWidget(self._view, 1)
        view_row.addWidget(edit)
        layout.addLayout(view_row)

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

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setItemDelegateForColumn(0, _SwatchDelegate(self._table))
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_NAME_COL, QHeaderView.ResizeMode.Stretch)
        self._table.currentCellChanged.connect(self._on_current)
        self._table.itemDoubleClicked.connect(
            lambda item: self.activated.emit(self._token(self._rows[item.row()])))
        layout.addWidget(self._table)

        self._count = QLabel()
        layout.addWidget(self._count)
        self._quiet = False
        self._palette: str | None = None
        self._view_name = WEB
        self._fill_views(color_palettes.last_view())

    def _token(self, row: tuple) -> str:
        return row[0] if self._palette is None else row[1]

    def current_name(self) -> str | None:
        row = self._table.currentRow()
        return self._token(self._rows[row]) if 0 <= row < len(self._rows) else None

    def _fill_views(self, want: str | None):
        """(Re)list the views in the chooser and show `want`, or Web colors
        if there is no such view."""
        self._view.blockSignals(True)
        self._view.clear()
        self._view.addItems(BUILTIN_VIEWS)
        if self._palettes:
            self._view.insertSeparator(len(BUILTIN_VIEWS))
            self._view.addItems(list(self._palettes))
        self._view.blockSignals(False)
        self._set_view(want if want in BUILTIN_VIEWS or want in self._palettes else WEB)

    def _set_view(self, view: str):
        """Show `view` without remembering it as the user's choice."""
        self._view.blockSignals(True)
        self._view.setCurrentText(view)
        self._view.blockSignals(False)
        self._on_view(view)

    def _on_view(self, text: str):
        self._view_name = text
        self._palette = text if text in self._palettes and text not in BUILTIN_VIEWS else None
        self._fill(self._sort.currentText())

    def reveal(self, name: str | None, hexa: str):
        """Make the colour `name` (or, in a palette, `hexa`) the selected row,
        switching -- without remembering it -- to the first view that lists
        it if this one does not: the picker opened on `"xkcd:dark mint"` while
        Web colors was the view. Leaves the view alone if none lists it."""
        start = self._view_name
        for view in dict.fromkeys([start, *BUILTIN_VIEWS, *self._palettes]):
            if view != self._view_name:
                self._set_view(view)
            self.select(name)
            if self.current_name() is None and self._palette is not None:
                self.select(hexa)
            if self.current_name() is not None:
                return
        self._set_view(start)

    def _edit_palettes(self):
        dlg = PaletteEditor(self._palettes, self._palette, self)
        accepted = dlg.exec()
        # On macOS, closing a modal dialog makes the app's main window key,
        # raising it over the modeless Color List this dialog came from.
        owner = self.window()
        owner.raise_()
        owner.activateWindow()
        if accepted:
            self._palettes = dlg.palettes
            color_palettes.save_palettes(self._palettes)
            self._fill_views(dlg.current)

    def select(self, name: str | None):
        """Select `name`'s row (or nothing) without firing current_changed."""
        self._quiet = True
        try:
            rows = [r for r, row in enumerate(self._rows) if self._token(row) == name]
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
            self.current_changed.emit(self._token(self._rows[row]))

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
        if self._palette is not None:
            self._rows = color_rows(sort, self._palettes[self._palette])
        else:
            rows = color_rows(sort)
            if self._view_name != ALL:
                xkcd = self._view_name == XKCD
                rows = [r for r in rows if r[0].startswith("xkcd:") == xkcd]
            self._rows = rows
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
        for r, row in enumerate(self._rows):
            hit = matches(row, needle)
            self._table.setRowHidden(r, not hit)
            shown += hit
        total = len(self._rows)
        self._count.setText(f"{total} colors" if shown == total else f"{shown} of {total} colors")


class PaletteEditor(QDialog):
    """Make, rename, delete and edit palettes. A palette is a table of Name,
    Hex and a swatch: Hex takes `#rrggbb`, `#rgb` or `r, g, b` (rewritten as
    hex), a value it cannot read is marked and left out, and double-clicking
    a swatch opens the system colour panel. Paste (with the table focused)
    adds one colour per line of the clipboard, as color_palettes reads them,
    so a list can come straight from filamentcolors.xyz or a spreadsheet.
    Works on a copy: `palettes` and `current` are the result once accepted."""

    NAME, HEX, SWATCH = range(3)
    _HELP = ("Hex takes <b>#rrggbb</b> or <b>r, g, b</b> (0-255). Double-click a swatch to "
             "pick a color. Paste lines of <i>name  #rrggbb</i> to add several at once.")

    def __init__(self, palettes: dict, current: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Palettes")
        self.resize(480, 520)
        self.palettes = {k: list(v) for k, v in palettes.items()}
        self.current = current if current in self.palettes else next(iter(self.palettes), None)
        # Palette -> the table's rows as typed, [name, hex text], for every
        # palette visited, so switching away keeps unsaved edits.
        self._edits: dict[str, list[list[str]]] = {}

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.currentTextChanged.connect(self._switch)
        row.addWidget(self._combo, 1)
        for label, slot in (("New…", self._new), ("Rename…", self._rename), ("Delete", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        layout.addLayout(row)
        help_ = QLabel(self._HELP)
        help_.setWordWrap(True)
        layout.addWidget(help_)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Name", "Hex", ""])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setItemDelegateForColumn(self.SWATCH, _SwatchDelegate(self._table))
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self.NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.HEX, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.SWATCH, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(self.SWATCH, 56)
        self._table.itemChanged.connect(self._on_item)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        paste = QShortcut(QKeySequence.StandardKey.Paste, self._table)
        paste.setContext(Qt.ShortcutContext.WidgetShortcut)   # a cell being edited pastes into itself
        paste.activated.connect(lambda: self.paste_text(QApplication.clipboard().text()))
        layout.addWidget(self._table, 1)

        edit_row = QHBoxLayout()
        self._add_btn = QPushButton("Add Color")
        self._add_btn.clicked.connect(self._add)
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.clicked.connect(self._remove)
        edit_row.addWidget(self._add_btn)
        edit_row.addWidget(self._remove_btn)
        edit_row.addStretch(1)
        layout.addLayout(edit_row)
        self._status = QLabel()
        layout.addWidget(self._status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._shown: str | None = None
        self._relist(self.current)

    # -- the table ---------------------------------------------------------

    def _append(self, name: str, text: str):
        self._table.blockSignals(True)
        r = self._table.rowCount()
        self._table.insertRow(r)
        self._table.setItem(r, self.NAME, QTableWidgetItem(name))
        self._table.setItem(r, self.HEX, QTableWidgetItem(text))
        swatch = QTableWidgetItem()
        swatch.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        swatch.setToolTip("Double-click to pick a color")
        self._table.setItem(r, self.SWATCH, swatch)
        self._table.blockSignals(False)
        self._refresh_row(r)

    def _refresh_row(self, r: int):
        """Normalise row r's hex and paint its swatch, or mark it unreadable."""
        self._table.blockSignals(True)
        try:
            hex_item = self._table.item(r, self.HEX)
            hexa = color_palettes.parse_color_value(hex_item.text())
            swatch = self._table.item(r, self.SWATCH)
            if hexa:
                hex_item.setText(hexa)
                hex_item.setForeground(self._table.palette().text())
                swatch.setBackground(QColor(hexa))
            else:
                hex_item.setForeground(QColor("#d03030"))
                swatch.setBackground(self._table.palette().base())
        finally:
            self._table.blockSignals(False)
        self._check()

    def _on_item(self, item):
        if item.column() == self.HEX:
            self._refresh_row(item.row())
        else:
            self._check()

    def _on_double_click(self, r: int, col: int):
        if col != self.SWATCH:
            return
        start = QColor(color_palettes.parse_color_value(self._table.item(r, self.HEX).text()) or "#808080")
        c = QColorDialog.getColor(start, self, "Choose Color")
        if c.isValid():
            self._table.item(r, self.HEX).setText(c.name(QColor.NameFormat.HexRgb))

    def _rows(self) -> list[list[str]]:
        return [[self._table.item(r, self.NAME).text(), self._table.item(r, self.HEX).text()]
                for r in range(self._table.rowCount())]

    def _add(self):
        self._append("", "#808080")
        r = self._table.rowCount() - 1
        self._table.setCurrentCell(r, self.NAME)
        self._table.editItem(self._table.item(r, self.NAME))

    def _remove(self):
        for r in sorted({i.row() for i in self._table.selectedItems()}, reverse=True):
            self._table.removeRow(r)
        self._check()

    def paste_text(self, text: str):
        """Add a row per line of `text` that names a colour."""
        if self._shown is None:
            return
        entries, bad = color_palettes.parse_palette(text)
        for name, hexa in entries:
            self._append(name, hexa)
        if bad:
            self._status.setText(f"Pasted {len(entries)}; no color on line "
                                 + ", ".join(map(str, bad)) + " of the clipboard.")

    def _check(self):
        if self._shown is None:
            self._status.setText("Press New… to make a palette.")
            return
        rows = self._rows()
        good = sum(bool(color_palettes.parse_color_value(t)) for _n, t in rows)
        bad = [str(i) for i, (_n, t) in enumerate(rows, 1) if not color_palettes.parse_color_value(t)]
        text = f"{good} color{'' if good == 1 else 's'}"
        if bad:
            text += f"; row {', '.join(bad)} {'has' if len(bad) == 1 else 'have'} no valid color and will be left out"
        self._status.setText(text)

    # -- palettes ----------------------------------------------------------

    def _relist(self, want: str | None):
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(list(self.palettes))
        self._combo.blockSignals(False)
        self._shown = None
        if want:
            self._combo.setCurrentText(want)
        self._switch(self._combo.currentText())

    def _stash(self):
        if self._shown is not None:
            self._edits[self._shown] = self._rows()

    def _switch(self, name: str):
        self._stash()
        self._shown = name or None
        self.current = self._shown
        for w in (self._table, self._add_btn, self._remove_btn):
            w.setEnabled(self._shown is not None)
        self._table.setRowCount(0)
        rows = self._edits.get(name, [list(e) for e in self.palettes.get(name, [])])
        for n, t in rows:
            self._append(n, t)
        self._check()

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Palette name:", text=initial)
        name = name.strip()
        if not ok or not name:
            return None
        if name in BUILTIN_VIEWS or (name in self.palettes and name != initial):
            QMessageBox.warning(self, title, f"There is already a palette called “{name}”.")
            return None
        return name

    def _new(self):
        name = self._ask_name("New Palette")
        if name:
            self._stash()
            self.palettes[name] = []
            self._relist(name)

    def _rename(self):
        old = self._shown
        name = old and self._ask_name("Rename Palette", old)
        if name and name != old:
            self._stash()
            self.palettes = {(name if k == old else k): v for k, v in self.palettes.items()}
            if old in self._edits:
                self._edits[name] = self._edits.pop(old)
            self._shown = None
            self._relist(name)

    def _delete(self):
        name = self._shown
        if name and QMessageBox.question(self, "Delete Palette", f"Delete the palette “{name}”?") \
                == QMessageBox.StandardButton.Yes:
            self.palettes.pop(name)
            self._edits.pop(name, None)
            self._shown = None
            self._relist(next(iter(self.palettes), None))

    def _save(self):
        self._stash()
        for name, rows in self._edits.items():
            if name in self.palettes:
                entries = []
                for n, t in rows:
                    hexa = color_palettes.parse_color_value(t)
                    if hexa:
                        entries.append((n.strip() or hexa, hexa))
                self.palettes[name] = entries
        self.accept()


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
