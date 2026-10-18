"""TurtleViewer: a BOSL2 turtle() command list, as a table of commands beside
the path it draws.

The path is worked out by `belfryscad.turtle`, a port of BOSL2's own
turtle(), so the preview needs no library and no render. The viewport is the
2D path editor's, read-only: the path is derived from the commands, so it is
the commands that are edited, and selecting a row highlights the points that
command drew. The background image to trace over comes with it.
"""
from __future__ import annotations

import ast
import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QSplitter, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from belfryscad import turtle
from belfryscad.window.background_image import restore_session_background
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin,
                                                  _style_table_headers,
                                                  _sync_viewport_to_main_window)
from belfryscad.window.data_viewer_path import _PathViewport


def format_turtle(value) -> str:
    """OpenSCAD source for a turtle list: `["move", 10, "arcleft", 5, 90]`."""
    if isinstance(value, list):
        return "[" + ", ".join(format_turtle(x) for x in value) + "]"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.10g}" if isinstance(value, float) else str(value)


def _parse_arg(text: str):
    """A cell's argument: None when blank, else a number, vector or list."""
    text = text.strip()
    if not text:
        return None
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = None
    if not isinstance(value, (int, float, list)) or isinstance(value, bool):
        raise turtle.TurtleError(f"An argument is a number or a list, not {text}")
    return value


class _CommandDelegate(QStyledItemDelegate):
    """The Command column edits with a menu of turtle commands."""

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.addItems(turtle.COMMANDS)
        box.activated.connect(lambda _i, b=box: self.commitData.emit(b))
        return box

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data())
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class TurtleViewer(QDialog, _UndoableViewerMixin):
    """Command table + live path preview. Read-only by default; pass
    `editable=True` for Save/Cancel editing, as the other literal editors."""

    committed = Signal(str)

    def __init__(self, title: str, commands: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "Turtle Editor" if editable else "Turtle Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 520)
        self._editable = editable
        self._commands = commands

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _PathViewport([], True, self, editable=False)
        _sync_viewport_to_main_window(self._vp)
        # Every arc is many points; the markers would bury the path. The
        # selected command's points still show.
        self._vp._show_unselected = False
        splitter.addWidget(self._vp)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self._count_label = QLabel()
        side_layout.addWidget(self._count_label)
        self._table = QTableWidget(0, 3)
        self._table.setFont(QFont("Menlo", 11))
        self._table.setHorizontalHeaderLabels(["Command", "Arg 1", "Arg 2"])
        _style_table_headers(self._table)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        fm = self._table.fontMetrics()
        self._table.setColumnWidth(0, fm.horizontalAdvance("arcrightto") + 40)
        self._table.setColumnWidth(1, fm.horizontalAdvance("-0000.00") + 16)
        self._table.setColumnWidth(2, fm.horizontalAdvance("-0000.00") + 16)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._highlight_selection)
        if editable:
            self._table.setItemDelegateForColumn(0, _CommandDelegate(self._table))
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                        | QAbstractItemView.EditTrigger.EditKeyPressed)
            self._table.itemChanged.connect(self._on_item_changed)
            self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._table.customContextMenuRequested.connect(self._show_table_menu)
        else:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        side_layout.addWidget(self._table, 1)
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("QLabel { color: #d33; }")
        side_layout.addWidget(self._error_label)
        splitter.addWidget(side)
        splitter.setSizes([560, 340])
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        restore_session_background(self.windowTitle(), self._vp)
        if editable:
            self._setup_undo(commands)
            add = QPushButton("Add Command")
            add.clicked.connect(lambda: self._insert(len(self._rows())))
            btn_row.addWidget(add)
            btn_row.addLayout(self._make_reset_button_row())
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            self._save = QPushButton("Save")
            self._save.clicked.connect(self._on_save)
            btn_row.addWidget(self._save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._spans: list[tuple[int, int]] = []
        self._populate_table()
        self._vp.schedule_load(lambda: self._rebuild(reframe=True))

    # -- value ------------------------------------------------------------

    def _rows(self):
        return turtle.split(self._commands)

    def _get_value(self):
        return self._commands

    def _apply_value(self, value):
        self._commands = value
        self._populate_table()
        self._rebuild(reframe=False)

    def _populate_table(self):
        rows = self._rows()
        self._table.blockSignals(True)
        self._table.setRowCount(len(rows))
        self._table.setVerticalHeaderLabels([str(i) for i in range(len(rows))])
        for i, (cmd, args) in enumerate(rows):
            texts = [cmd] + [format_turtle(a) for a in args] + [""] * (2 - len(args))
            for j, text in enumerate(texts[:3]):
                item = QTableWidgetItem(text)
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, j, item)
        self._table.blockSignals(False)
        self._count_label.setText(f"Commands ({len(rows)})")

    def _rebuild(self, reframe: bool = False):
        try:
            path, self._spans = turtle.run(self._commands)
            self._error_label.clear()
            ok = True
        except turtle.TurtleError as e:
            path, self._spans = [[0.0, 0.0]], []
            self._error_label.setText(str(e))
            ok = False
        if self._editable:
            self._save.setEnabled(ok)
        if self._vp._ctx is not None:
            self._vp.load_path(path, False, reframe=reframe)
            self._highlight_selection()

    def _highlight_selection(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._spans):
            self._vp.set_selected([])
            return
        first, last = self._spans[rows[0].row()]
        self._vp.set_selected(list(range(first, last + 1)))

    # -- editing ----------------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem):
        rows = copy.deepcopy(self._rows())
        i, j = item.row(), item.column()
        cmd, args = rows[i]
        try:
            if j == 0:
                cmd = item.text()
            else:
                parsed = [_parse_arg(self._table.item(i, k).text()) for k in (1, 2)]
                # A blank first argument with a second makes no sense: keep
                # the arguments as given, up to the first blank.
                args = parsed[:parsed.index(None)] if None in parsed else parsed
            rows[i] = (cmd, args)
            new = turtle.join(rows)
            # A string typed as an argument would be read as a new command.
            if len(turtle.split(new)) != len(rows):
                raise turtle.TurtleError("A string argument would start a new command")
        except turtle.TurtleError as e:
            self._error_label.setText(str(e))
            self._populate_table()
            return
        self._commit_value(new, "Edit Command")

    def _insert(self, at: int, row=("move", [10])):
        rows = self._rows()
        rows.insert(at, (row[0], list(row[1])))
        self._commit_value(turtle.join(rows), "Add Command")
        self._table.selectRow(at)

    def _delete(self, at: int):
        rows = self._rows()
        del rows[at]
        self._commit_value(turtle.join(rows), "Delete Command")

    def _move(self, at: int, by: int):
        rows = self._rows()
        rows[at], rows[at + by] = rows[at + by], rows[at]
        self._commit_value(turtle.join(rows), "Move Command")
        self._table.selectRow(at + by)

    def _show_table_menu(self, pos):
        at = self._table.rowAt(pos.y())
        n = len(self._rows())
        menu = QMenu(self._table)
        if at < 0:
            menu.addAction("Add Command", lambda: self._insert(n))
        else:
            menu.addAction("Insert Command Above", lambda: self._insert(at))
            menu.addAction("Insert Command Below", lambda: self._insert(at + 1))
            menu.addSeparator()
            up = menu.addAction("Move Up", lambda: self._move(at, -1))
            up.setEnabled(at > 0)
            down = menu.addAction("Move Down", lambda: self._move(at, 1))
            down.setEnabled(at < n - 1)
            menu.addSeparator()
            menu.addAction("Delete Command", lambda: self._delete(at))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        if self._editable and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) \
                and self._table.state() != QAbstractItemView.State.EditingState:
            rows = self._table.selectionModel().selectedRows()
            if rows:
                self._delete(rows[0].row())
                event.accept()
                return
        super().keyPressEvent(event)

    def _on_save(self):
        self.committed.emit(format_turtle(self._commands))
        self.accept()
