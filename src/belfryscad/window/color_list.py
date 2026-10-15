"""The Color List dock — every name `color()` accepts, and your own palettes.

A dockable pane, like OpenSCAD's Color List (#766). Two sets of names, as
OpenSCAD's parse_color reads them (and openscad_cpp_evaluator >= 1.29.0 with
it):

- the CSS names: Qt's list (`color_names._PICKABLE_NAMES`) plus
  `rebeccapurple`, which Qt never adopted; `transparent` is left out, since
  its swatch would be a lie;
- the 949 xkcd survey names, listed -- and inserted -- as `xkcd:<name>`,
  which is how color() takes them (`xkcd_colors.py`).

**Palette:** shows one set at a time -- Web colors (the default), XKCD colors,
All named colors -- or one of your own palettes (`color_palettes`), and ends
with **Edit palette list…**. In a palette, **+** and **−** add and remove
colours, Name and Hex can be typed over, and Paste adds a line per colour.

Sort by name, hue (30-degree bands, dark to light), saturation, chroma,
lightness, value or luminance. **Insert** emits `insert_requested` with the
selected row's *token* -- its name, or for a palette colour (whose name is
the user's own, not one color() takes) its hex -- and the main window puts it
in the editor in place of the selection (`color_literals.insertion_for`).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from belfryscad.window import color_palettes
from belfryscad.window.color_literals import color_for_name, looks_like_color_vector, name_for
from belfryscad.window.color_names import SORT_KEYS, sorted_colors

#: The chooser's built-in views of the names color() takes, before your own
#: palettes. Web is the CSS names (plus rebeccapurple); XKCD the 949 survey
#: names as `xkcd:<name>`; All both, so one filter searches everything.
WEB, XKCD, ALL = "Web colors", "XKCD colors", "All named colors"
BUILTIN_VIEWS = (WEB, XKCD, ALL)
#: The chooser's last entry: not a view, it opens the palette list editor.
EDIT_PALETTES = "Edit palette list…"

_COLUMNS = ("", "Name", "Hex", "RGB")
_NAME_COL, _HEX_COL = 1, 2


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


def literal_color(literal: str) -> tuple[str | None, QColor | None]:
    """(name to look for, colour) for a colour literal's source text: a
    quoted name is its own name -- name_for() reverses only the CSS names,
    so `"xkcd:dark mint"` would otherwise be lost -- and anything else the
    name its colour has, if any."""
    if literal.startswith('"'):
        content = literal[1:-1]
        c = color_for_name(content)
        if c is not None and not content.startswith("#"):
            return content, c
    else:
        c = looks_like_color_vector(literal.strip("[]").split(","))
    return (name_for(c) if c is not None else None), c


class _SwatchDelegate(QStyledItemDelegate):
    """The swatch cell in its own colour, selected or not. The default
    delegate paints a selected cell in the selection colour, so the one row
    you were looking at showed the wrong colour."""

    def paint(self, painter, option, index):
        painter.fillRect(option.rect, index.data(Qt.ItemDataRole.BackgroundRole))


class ColorListPane(QWidget):
    """Colors (view), Filter, Sort by and the swatch table, with the count
    and + / − under it (enabled in a palette).

    `insert_requested(token)` fires on Insert. `select(token)` and
    `reveal(name, hex)` set the selection from code."""

    insert_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        self._palettes = color_palettes.load_palettes()
        self._view = QComboBox()
        self._view.setToolTip("The named colors color() takes, or one of your own palettes")
        self._view.currentTextChanged.connect(self._on_view_picked)
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("Palette:"))
        view_row.addWidget(self._view, 1)
        layout.addLayout(view_row)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by name or hex…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        self._sort = QComboBox()
        self._sort.addItems(list(SORT_KEYS))
        self._sort.setCurrentText("Hue")
        self._sort.currentTextChanged.connect(self._fill)
        # A macOS combo asks for 32px and draws itself in the middle ~22,
        # which read as a padded gap under the Colors row (#766). Capped at
        # the line edit's height, the rows sit at an even spacing.
        for combo in (self._view, self._sort):
            combo.setMaximumHeight(self._search.sizeHint().height() + 4)
        top = QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(QLabel("Sort by:"))
        top.addWidget(self._sort)
        layout.addLayout(top)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        # In a palette, double-click edits Name and Hex in place (only those
        # cells are editable, and only there -- _fill) and opens the system
        # colour panel on the swatch.
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.itemChanged.connect(self._on_item_edited)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setItemDelegateForColumn(0, _SwatchDelegate(self._table))
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_NAME_COL, QHeaderView.ResizeMode.Stretch)
        self._table.cellDoubleClicked.connect(
            lambda row, col: self._pick_swatch(row) if col == 0 else None)
        paste = QShortcut(QKeySequence.StandardKey.Paste, self._table)
        paste.setContext(Qt.ShortcutContext.WidgetShortcut)
        paste.activated.connect(lambda: self.paste_text(QApplication.clipboard().text()))
        layout.addWidget(self._table, 1)

        self._count = QLabel()
        self._add_btn = QToolButton(text="+", toolTip="Add a color to this palette")
        self._add_btn.clicked.connect(self._add_color)
        self._remove_btn = QToolButton(text="−", toolTip="Remove the selected color from this palette")
        self._remove_btn.clicked.connect(self._remove_color)
        bottom = QHBoxLayout()
        insert = QPushButton("Insert")
        insert.setToolTip("Put the selected color in the editor, replacing the selection")
        insert.clicked.connect(self._insert)
        bottom.addWidget(self._count, 1)
        bottom.addWidget(insert)
        bottom.addWidget(self._add_btn)
        bottom.addWidget(self._remove_btn)
        layout.addLayout(bottom)

        self._palette: str | None = None
        self._view_name = WEB
        self._fill_views(color_palettes.last_view())

    def _token(self, row: tuple) -> str:
        return row[0] if self._palette is None else row[1]

    def current_name(self) -> str | None:
        row = self._table.currentRow()
        return self._token(self._rows[row]) if 0 <= row < len(self._rows) else None

    # -- views -------------------------------------------------------------

    def _fill_views(self, want: str | None):
        """(Re)list the views in the chooser and show `want`, or Web colors
        if there is no such view."""
        self._view.blockSignals(True)
        self._view.clear()
        self._view.addItems(BUILTIN_VIEWS)
        if self._palettes:
            self._view.insertSeparator(self._view.count())
            self._view.addItems(list(self._palettes))
        self._view.insertSeparator(self._view.count())
        self._view.addItem(EDIT_PALETTES)
        self._view.blockSignals(False)
        self._set_view(want if want in BUILTIN_VIEWS or want in self._palettes else WEB)

    def _set_view(self, view: str):
        """Show `view` without remembering it as the user's choice."""
        self._view.blockSignals(True)
        self._view.setCurrentText(view)
        self._view.blockSignals(False)
        self._on_view(view)

    def _on_view_picked(self, text: str):
        if text == EDIT_PALETTES:
            self._set_view(self._view_name)     # an action, not a view
            # After the popup has closed, not from inside its event handling.
            QTimer.singleShot(0, self._edit_palette_list)
            return
        self._on_view(text)
        color_palettes.set_last_view(text)     # remembered only when picked

    def _on_view(self, text: str):
        self._view_name = text
        self._palette = text if text in self._palettes and text not in BUILTIN_VIEWS else None
        self._add_btn.setEnabled(self._palette is not None)
        self._remove_btn.setEnabled(self._palette is not None)
        self._fill(self._sort.currentText())

    def _edit_palette_list(self):
        dlg = PaletteListEditor(self._palettes, self._palette, self)
        dlg.exec()
        self._palettes = dlg.palettes
        color_palettes.save_palettes(self._palettes)
        self._fill_views(dlg.current or self._view_name)

    # -- a palette's colours ----------------------------------------------

    def _save_palette(self, entries: list, select: str | None):
        self._palettes[self._palette] = entries
        color_palettes.save_palettes(self._palettes)
        self._fill(self._sort.currentText())
        if select:
            self.select(select)

    def _add_color(self):
        if self._palette is None:
            return
        start = QColor(self.current_name() or "#808080")
        c = QColorDialog.getColor(start, self, "Add Color")
        if not c.isValid():
            return
        hexa = c.name(QColor.NameFormat.HexRgb)
        name, ok = QInputDialog.getText(self, "Add Color", "Name:", text=hexa)
        if ok:
            self._save_palette(self._palettes[self._palette] + [(name.strip() or hexa, hexa)], hexa)

    def _entry_index(self, row: int) -> int:
        """The palette entry table row `row` shows (the table is sorted)."""
        name, hexa = self._rows[row][:2]
        return next(i for i, (n, h) in enumerate(self._palettes[self._palette])
                    if n == name and QColor(h).name() == hexa)

    def _remove_color(self):
        row = self._table.currentRow()
        if self._palette is None or not 0 <= row < len(self._rows):
            return
        entries = list(self._palettes[self._palette])
        del entries[self._entry_index(row)]
        self._save_palette(entries, None)

    def _insert(self):
        token = self.current_name()
        if token:
            self.insert_requested.emit(token)

    def _pick_swatch(self, row: int):
        """A palette swatch double-clicked: change its colour in the system
        colour panel, through the Hex cell so it is saved like a typed one."""
        if self._palette is None or not 0 <= row < len(self._rows):
            return
        c = QColorDialog.getColor(self._rows[row][3], self, "Choose Color")
        if c.isValid():
            self._table.item(row, _HEX_COL).setText(c.name(QColor.NameFormat.HexRgb))

    def _on_item_edited(self, item):
        """A palette row's Name or Hex typed over. Hex takes `#rrggbb`,
        `#rgb` or 0-255 `r, g, b`; anything else puts the old value back.
        A blank name becomes the hex, as everywhere else."""
        if self._palette is None or item.column() not in (_NAME_COL, _HEX_COL):
            return
        entries = list(self._palettes[self._palette])
        i = self._entry_index(item.row())
        name, hexa = entries[i]
        if item.column() == _NAME_COL:
            name = item.text().strip()
        else:
            hexa = color_palettes.parse_color_value(item.text()) or hexa
        entries[i] = (name or hexa, hexa)
        # Rebuilt after the editor that sent this has finished closing.
        QTimer.singleShot(0, lambda: self._save_palette(entries, QColor(hexa).name()))

    def paste_text(self, text: str):
        """Add a palette colour per line of `text` that names one."""
        if self._palette is None:
            return
        entries, _bad = color_palettes.parse_palette(text)
        if entries:
            self._save_palette(self._palettes[self._palette] + entries, entries[-1][1])

    # -- selection ---------------------------------------------------------

    def reveal(self, name: str | None, hexa: str):
        """Make the colour `name` (or, in a palette, `hexa`) the selected row,
        switching -- without remembering it -- to the first view that lists
        it if this one does not. Leaves the view alone if none lists it."""
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

    def show_literal(self, literal: str):
        """Select the row for a colour literal's source text, if any view
        lists it -- what Choose Color… does."""
        name, c = literal_color(literal)
        if c is not None:
            self.reveal(name, c.name(QColor.NameFormat.HexRgb))

    def select(self, name: str | None):
        """Select `name`'s row (or nothing)."""
        rows = [r for r, row in enumerate(self._rows) if self._token(row) == name]
        if rows:
            self._table.setCurrentCell(rows[0], _NAME_COL)
            self._table.scrollToItem(self._table.item(rows[0], _NAME_COL))
        else:
            self._table.setCurrentCell(-1, -1)
            self._table.clearSelection()

    def _fill(self, sort: str):
        """(Re)build the table in `sort` order, keeping the filter and the
        selected name.

        Auto-sized columns are switched off while the cells go in and sized
        once at the end: ResizeToContents re-measures every row each time a
        cell is set, which made a re-sort of 1,097 rows take 85 s."""
        keep = self.current_name() if hasattr(self, "_rows") else None
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        if self._palette is not None:
            self._rows = color_rows(sort, self._palettes[self._palette])
        else:
            rows = color_rows(sort)
            if self._view_name != ALL:
                xkcd = self._view_name == XKCD
                rows = [r for r in rows if r[0].startswith("xkcd:") == xkcd]
            self._rows = rows
        self._table.blockSignals(True)          # no itemChanged for our own cells
        self._table.setRowCount(len(self._rows))
        fixed = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        for r, (name, hexa, rgb, color) in enumerate(self._rows):
            swatch = QTableWidgetItem("      ")
            swatch.setBackground(color)
            swatch.setFlags(fixed)
            self._table.setItem(r, 0, swatch)
            for c, text in enumerate((name, hexa, rgb), start=1):
                item = QTableWidgetItem(text)
                if self._palette is None or c not in (_NAME_COL, _HEX_COL):
                    item.setFlags(fixed)
                self._table.setItem(r, c, item)
        self._table.blockSignals(False)
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


class PaletteListEditor(QDialog):
    """New, rename and delete palettes -- the list, not their colours, which
    the Color List's + and − edit. Works on a copy: `palettes` is the result,
    and `current` the palette last made or renamed (else None)."""

    def __init__(self, palettes: dict, current: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Palettes")
        self.palettes = {k: list(v) for k, v in palettes.items()}
        self.current = None
        layout = QVBoxLayout(self)
        self._list = QListWidget()
        layout.addWidget(self._list, 1)
        row = QHBoxLayout()
        for label, slot in (("New…", self._new), ("Rename…", self._rename), ("Delete", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
        self._relist(current)

    def _relist(self, select: str | None):
        self._list.clear()
        self._list.addItems(list(self.palettes))
        hits = self._list.findItems(select or "", Qt.MatchFlag.MatchExactly)
        self._list.setCurrentItem(hits[0] if hits else self._list.item(0))

    def _selected(self) -> str | None:
        item = self._list.currentItem()
        return item.text() if item else None

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Palette name:", text=initial)
        name = name.strip()
        if not ok or not name:
            return None
        if name in BUILTIN_VIEWS or name == EDIT_PALETTES or (name in self.palettes and name != initial):
            QMessageBox.warning(self, title, f"There is already a palette called “{name}”.")
            return None
        return name

    def _new(self):
        name = self._ask_name("New Palette")
        if name:
            self.palettes[name] = []
            self.current = name
            self._relist(name)

    def _rename(self):
        old = self._selected()
        name = old and self._ask_name("Rename Palette", old)
        if name and name != old:
            self.palettes = {(name if k == old else k): v for k, v in self.palettes.items()}
            self.current = name
            self._relist(name)

    def _delete(self):
        name = self._selected()
        if name and QMessageBox.question(self, "Delete Palette", f"Delete the palette “{name}”?") \
                == QMessageBox.StandardButton.Yes:
            self.palettes.pop(name)
            if self.current == name:
                self.current = None
            self._relist(None)
