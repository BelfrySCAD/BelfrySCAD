"""ObjectViewer: the entries of an OscObject, or of an all-literal object() call."""
from __future__ import annotations

import re

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QMenu,
                               QPushButton)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from belfryscad.window.data_viewer_common import (_UndoableViewerMixin, _fmt_short,
                                                  _is_oscobject,
                                                  _scad_literal_to_python,
                                                  _style_table_headers)


# ---------------------------------------------------------------------------
# Matrix Viewer
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Object Viewer / Editor
# ---------------------------------------------------------------------------

class ObjectViewer(QDialog, _UndoableViewerMixin):
    """Key/Type/Value table for an OpenSCAD `object()`.

    Read-only by default, which is the only possibility for a value reached
    from the debugger -- a runtime value has no source span to write back to.
    Pass `editable=True` (from "Edit as Object...", which only offers itself
    for an all-literal `object(...)` call) for Save/Cancel, editable keys and
    values, and add/delete rows. `committed` fires once, with the whole
    re-rendered call text, when Save is clicked.

    Values are shown and typed as OpenSCAD source (`true`, `undef`, `[1,2]`),
    not Python, since that is what the user is editing.

    Right-click a row to open its value in whichever other viewer fits --
    the same menu the editor's own right-click builds, so a nested mesh
    reaches the VNF viewer.
    """

    committed = Signal(str)

    def __init__(self, title: str, value, parent=None, editable: bool = False,
                 entries: list | None = None):
        super().__init__(parent)
        self._title = title
        self._editable = editable
        label = "Object Editor" if editable else "Object Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(560, 420)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # `entries` is the parsed literal form (editing); `value` is a live
        # OscObject (viewing). Exactly one drives the table.
        if entries is None:
            entries = [(str(k), v) for k, v in value.items()] if _is_oscobject(value) else []
        self._entries: list[list] = [[k, v] for k, v in entries]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Key", "Type", "Value"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        _style_table_headers(self._table)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        if editable:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                         | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        self._refresh()
        if editable:
            self._table.itemChanged.connect(self._on_item_changed)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        if editable:
            self._setup_undo(self._entries)
            add = QPushButton("Add Key")
            add.clicked.connect(self._on_add)
            btn_row.addWidget(add)
            self._del_btn = QPushButton("Delete Key")
            self._del_btn.clicked.connect(self._on_delete)
            btn_row.addWidget(self._del_btn)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.setDefault(True)
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

    # -- _UndoableViewerMixin contract ------------------------------------

    def _get_value(self):
        return [list(e) for e in self._entries]

    def _apply_value(self, value):
        self._entries = [list(e) for e in value]
        self._refresh()

    # -- display ----------------------------------------------------------

    def _refresh(self):
        blocked = self._table.blockSignals(True)
        self._table.setRowCount(len(self._entries))
        for row, (key, val) in enumerate(self._entries):
            key_item = QTableWidgetItem(str(key))
            self._table.setItem(row, 0, key_item)

            type_item = QTableWidgetItem(_scad_type_name(val))
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, type_item)

            # OpenSCAD source form when it round-trips, else the short
            # display form -- a live OscObject or a huge mesh has no useful
            # editable text, and showing one would invite a bad edit.
            shown = _python_value_to_scad(val) if _is_scad_literal_value(val) else _fmt_short(val)
            val_item = QTableWidgetItem(shown)
            if not self._editable or not _is_scad_literal_value(val):
                val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 2, val_item)
            if not self._editable:
                key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.blockSignals(blocked)

    # -- editing ----------------------------------------------------------

    def _on_item_changed(self, item):
        # Every mutation builds the NEW list and hands it to _commit_value,
        # which reads the "before" itself and pushes one undo step. Rejected
        # edits just refresh, putting the cell back -- the same silent
        # reject the other editors use for unparseable input.
        row, col = item.row(), item.column()
        if row < 0 or row >= len(self._entries):
            return
        new_entries = self._get_value()
        if col == 0:
            new_key = item.text().strip()
            if not re.match(r"^\$?[A-Za-z_][A-Za-z0-9_]*$", new_key):
                self._refresh()
                return
            if any(k == new_key for i, (k, _v) in enumerate(self._entries) if i != row):
                self._refresh()   # an object cannot hold the same key twice
                return
            new_entries[row][0] = new_key
        elif col == 2:
            ok, parsed = _scad_literal_to_python(item.text().strip())
            if not ok:
                self._refresh()
                return
            new_entries[row][1] = parsed
        else:
            return
        self._commit_value(new_entries, "Edit Key" if col == 0 else "Edit Value")

    def _on_add(self):
        new_entries = self._get_value()
        base, n = "key", 1
        existing = {k for k, _ in self._entries}
        while f"{base}{n}" in existing:
            n += 1
        new_entries.append([f"{base}{n}", 0])
        self._commit_value(new_entries, "Add Key")
        self._table.selectRow(len(self._entries) - 1)

    def _on_delete(self):
        rows = {i.row() for i in self._table.selectedIndexes()}
        if not rows:
            return
        new_entries = [e for i, e in enumerate(self._get_value()) if i not in rows]
        self._commit_value(new_entries, "Delete Key")

    def _on_save(self):
        self.committed.emit(_render_object_call([(k, v) for k, v in self._entries]))
        self.accept()

    # -- drill-down -------------------------------------------------------

    def _context_menu(self, pos):
        # Local: data_viewers imports this module to open it.
        from belfryscad.window.data_viewers import build_viewer_menu
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._entries):
            return
        key, val = self._entries[row]
        menu = QMenu(self)
        build_viewer_menu(menu, f"{self._title}.{key}" if self._title else str(key), val, self)
        if menu.isEmpty():
            return
        menu.exec(self._table.viewport().mapToGlobal(pos))


def _scad_type_name(v) -> str:
    """OpenSCAD's own name for a value's type, matching what `object()`'s
    diagnostics and the evaluator's oscTypeName() print."""
    if v is None:
        return "undef"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if _is_oscobject(v):
        return "object"
    if isinstance(v, (list, tuple)):
        return "vector"
    return "unknown"


def _is_scad_literal_value(v) -> bool:
    """True when `v` round-trips through OpenSCAD source text, i.e. is safe
    to show in an editable cell. A live OscObject does not."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, (list, tuple)):
        return all(_is_scad_literal_value(x) for x in v)
    return False


def _python_value_to_scad(v) -> str:
    """Render a Python value back as OpenSCAD source. Inverse of
    `_scad_literal_to_python` for the value shapes an object can hold."""
    if v is None:
        return "undef"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, float):
        # Trim the ".0" a whole number would otherwise carry: OpenSCAD has
        # one number type, so `42` and `42.0` are the same value and the
        # shorter form is what a human would have written.
        return str(int(v)) if v == int(v) else repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_python_value_to_scad(x) for x in v) + "]"
    return str(v)


def _render_object_call(entries) -> str:
    inner = ", ".join(f"{k}={_python_value_to_scad(v)}" for k, v in entries)
    return f"object({inner})"
