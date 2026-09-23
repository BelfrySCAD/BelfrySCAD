"""ListViewer: a scrollable table for lists and OscObject values."""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QMenu,
                               QPushButton)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from belfryscad.window.data_viewer_affine import _is_affine_matrix
from belfryscad.window.data_viewer_common import _fmt_short, _is_list, _is_oscobject
from belfryscad.window.data_viewer_matrix import _is_matrix
from belfryscad.window.data_viewer_path import _is_path
from belfryscad.window.data_viewer_vnf import _is_vnf


# ---------------------------------------------------------------------------
# List / Object Viewer
# ---------------------------------------------------------------------------

class ListViewer(QDialog):
    """Scrollable table displaying a list (with indices) or OscObject (with keys)."""

    def __init__(self, title: str, value, parent=None):
        super().__init__(parent)
        self._title = title
        self.setWindowTitle(f"List Viewer: {title}" if title else "List Viewer")
        self.resize(500, 400)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._value = value

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setColumnCount(2)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._entries: list[tuple[str, object]] = []
        self._populate(value)

    def _populate(self, value):
        if _is_oscobject(value):
            self._table.setHorizontalHeaderLabels(["Key", "Value"])
            for k, v in value.items():
                self._entries.append((str(k), v))
        elif _is_list(value):
            self._table.setHorizontalHeaderLabels(["Index", "Value"])
            for i, v in enumerate(value):
                self._entries.append((str(i), v))
        else:
            return

        self._table.setRowCount(len(self._entries))
        for row, (key, val) in enumerate(self._entries):
            key_item = QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, key_item)
            val_item = QTableWidgetItem(_fmt_short(val))
            val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, val_item)

    def _context_menu(self, pos):
        # Local: data_viewers imports this module to open it.
        from belfryscad.window.data_viewers import (
            _open_affine_matrix_viewer, _open_list_viewer, _open_matrix_viewer,
            _open_path_viewer, _open_vnf_viewer)
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._entries):
            return
        key, val = self._entries[row]

        menu = QMenu(self)
        sub_title = f"{self._title}[{key}]"
        if _is_list(val) or _is_oscobject(val):
            menu.addAction("View as List...", lambda: _open_list_viewer(
                sub_title, val, self))
        if _is_vnf(val):
            menu.addAction("View as VNF...", lambda: _open_vnf_viewer(
                sub_title, val, self))
        if _is_path(val):
            menu.addAction("View as Path...", lambda: _open_path_viewer(
                sub_title, val, self))
        if _is_matrix(val):
            menu.addAction("View as Matrix...", lambda: _open_matrix_viewer(
                sub_title, val, self))
        if _is_affine_matrix(val):
            menu.addAction("View as Affine Transform...", lambda: _open_affine_matrix_viewer(
                sub_title, val, self))
        if menu.isEmpty():
            return
        menu.exec(self._table.viewport().mapToGlobal(pos))
