"""MatrixViewer: a table for square 2x2-5x5 lists of lists of numbers."""
from __future__ import annotations

import copy

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QAbstractItemView, QPushButton)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from belfryscad.window.data_viewer_common import (_UndoableViewerMixin, _fmt_short,
                                                  _format_value, _identity_matrix,
                                                  _is_list, _parse_number,
                                                  _style_table_headers)


def _is_matrix(v) -> bool:
    """A matrix is a square list of lists of numbers, 2x2 through 5x5 —
    e.g. a transform matrix. No overlap with `_is_grid`: a grid row is a
    list of *points* (2/3-number lists), one nesting level deeper than a
    matrix row, which is a list of plain numbers."""
    if not (_is_list(v) and 2 <= len(v) <= 5):
        return False
    n = len(v)
    return all(_is_list(row) and len(row) == n
               and all(isinstance(x, (int, float)) for x in row) for row in v)


class MatrixViewer(QDialog, _UndoableViewerMixin):
    """Displays a square 2x2-5x5 list of lists of numbers as a grid of
    cells, row/column headers 0-indexed to match OpenSCAD list indexing.
    Read-only by default. Pass `editable=True` for a Save/Cancel editing
    mode: cell edits only update this dialog's own table (no writeback),
    and `committed` fires once, with the whole re-serialized value, when
    Save is clicked."""

    committed = Signal(str)

    def __init__(self, title: str, value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        self._title = title
        self._editable = editable
        label = "Matrix Editor" if editable else "Matrix Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._value = value

        n = len(value)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setRowCount(n)
        self._table.setColumnCount(n)
        self._table.setHorizontalHeaderLabels([str(c) for c in range(n)])
        self._table.setVerticalHeaderLabels([str(r) for r in range(n)])
        _style_table_headers(self._table)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        if editable:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                         | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, row in enumerate(value):
            for c, val in enumerate(row):
                item = QTableWidgetItem(_fmt_short(val))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        layout.addWidget(self._table)
        if editable:
            self._table.itemChanged.connect(self._on_item_changed)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        if editable:
            self._setup_undo(value)
            reset_id = QPushButton("Reset to Identity")
            reset_id.clicked.connect(self._on_reset_identity)
            btn_row.addWidget(reset_id)
            btn_row.addLayout(self._make_reset_button_row())
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        col_w = sum(self._table.columnWidth(c) for c in range(n))
        row_h = sum(self._table.rowHeight(r) for r in range(n))
        width = col_w + self._table.verticalHeader().width() + 40
        height = row_h + self._table.horizontalHeader().height() + 80
        self.resize(max(220, width), max(180, height))

    def _get_value(self):
        return self._value

    def _apply_value(self, value):
        self._value = value
        n = len(value)
        self._table.blockSignals(True)
        for r in range(n):
            for c in range(n):
                self._table.item(r, c).setText(_fmt_short(value[r][c]))
        self._table.blockSignals(False)

    def _on_reset_identity(self):
        self._commit_value(_identity_matrix(len(self._value)), "Reset to Identity")

    def _on_item_changed(self, item: QTableWidgetItem):
        parsed = _parse_number(item.text())
        if parsed is None:
            self._table.blockSignals(True)
            item.setText(_fmt_short(self._value[item.row()][item.column()]))
            self._table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._value)
        new_value[item.row()][item.column()] = parsed
        self._commit_value(new_value, "Edit Cell")

    def _on_save(self):
        self.committed.emit(_format_value(self._value))
        self.accept()
