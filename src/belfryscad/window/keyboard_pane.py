"""Preferences > Keyboard: every command and its shortcut, changeable (#821).

The commands are a main window's `_shortcut_bindings` (see `shortcuts`).
A change is saved and applied to every open window at once, like the rest
of Preferences. A key already in use is offered back from the command that
has it; a standard shortcut (Cut, Save, ...) cannot be changed or taken.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView,
                               QKeySequenceEdit, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from belfryscad.settings import app_settings
from belfryscad.window import shortcuts

_NATIVE = QKeySequence.SequenceFormat.NativeText


def _shown(keys: list[str]) -> str:
    return ", ".join(QKeySequence(k).toString(_NATIVE) for k in keys)


def apply_everywhere(overrides: dict):
    """Save `overrides` and apply them in every open main window."""
    shortcuts.save_overrides(app_settings(), overrides)
    for w in QApplication.topLevelWidgets():
        bindings = getattr(w, "_shortcut_bindings", None)
        if bindings is not None:
            shortcuts.apply(bindings, overrides)


class KeyboardPane(QWidget):
    def __init__(self, bindings: list, parent=None):
        super().__init__(parent)
        self._bindings = bindings
        self._overrides = shortcuts.load_overrides(app_settings())

        layout = QVBoxLayout(self)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Search commands and keys")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(len(bindings), 2)
        self._table.setHorizontalHeaderLabels(["Command", "Shortcut"])
        self._table.verticalHeader().hide()
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self._table, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Shortcut:"))
        self._edit = QKeySequenceEdit()
        self._edit.setMaximumSequenceLength(1)
        self._edit.setClearButtonEnabled(True)
        self._edit.editingFinished.connect(self._on_key_entered)
        # The clear button: no editingFinished for that.
        self._edit.keySequenceChanged.connect(lambda k: k.isEmpty() and self._on_key_entered())
        row.addWidget(self._edit, 1)
        self._default_btn = QPushButton("Restore Default")
        self._default_btn.clicked.connect(self._restore_one)
        row.addWidget(self._default_btn)
        layout.addLayout(row)

        bottom = QHBoxLayout()
        self._note = QLabel()
        self._note.setStyleSheet("QLabel { color: gray; }")
        bottom.addWidget(self._note, 1)
        all_btn = QPushButton("Restore All Defaults")
        all_btn.clicked.connect(self._restore_all)
        bottom.addWidget(all_btn)
        layout.addLayout(bottom)

        self._fill()
        self._on_select()

    # -- table --------------------------------------------------------------

    def _fill(self):
        for i, b in enumerate(self._bindings):
            name = QTableWidgetItem(b.id)
            keys = QTableWidgetItem(_shown(shortcuts.effective(b, self._overrides)))
            keys.setData(Qt.ItemDataRole.UserRole,
                         " ".join(shortcuts.effective(b, self._overrides)))
            for item in (name, keys):
                if b.locked:
                    item.setForeground(Qt.GlobalColor.gray)
                    item.setToolTip("A standard shortcut: it can't be changed.")
                elif b.id in self._overrides:
                    f = QFont(item.font())
                    f.setBold(True)
                    item.setFont(f)
                    item.setToolTip(f"Changed. Default: {_shown(b.default) or 'none'}")
            self._table.setItem(i, 0, name)
            self._table.setItem(i, 1, keys)
        self._apply_filter(self._filter.text())

    def _apply_filter(self, text: str):
        t = text.strip().lower()
        for i, b in enumerate(self._bindings):
            hay = (b.id + " " + self._table.item(i, 1).text() + " "
                   + self._table.item(i, 1).data(Qt.ItemDataRole.UserRole)).lower()
            self._table.setRowHidden(i, bool(t) and t not in hay)

    def _current(self):
        rows = self._table.selectionModel().selectedRows()
        return self._bindings[rows[0].row()] if rows else None

    def _on_select(self):
        b = self._current()
        editable = b is not None and not b.locked
        self._edit.setEnabled(editable)
        self._default_btn.setEnabled(editable and b.id in self._overrides)
        self._edit.blockSignals(True)
        keys = shortcuts.effective(b, self._overrides) if b else []
        self._edit.setKeySequence(QKeySequence(keys[0]) if keys else QKeySequence())
        self._edit.blockSignals(False)
        self._note.setText("A standard shortcut: it can't be changed." if b is not None and b.locked
                           else "Select a command, then press its new shortcut." if b is None else "")

    # -- changing -----------------------------------------------------------

    def _commit(self, overrides: dict):
        self._overrides = overrides
        apply_everywhere(overrides)
        row = self._table.currentRow()
        self._fill()
        self._table.selectRow(row)
        self._on_select()

    def _set(self, b, key: str, overrides: dict):
        """`b`'s override in `overrides`, dropped when it is the default."""
        if [key] == b.default or (not key and not b.default):
            overrides.pop(b.id, None)
        else:
            overrides[b.id] = key

    def _on_key_entered(self):
        b = self._current()
        if b is None or b.locked:
            return
        seq = self._edit.keySequence()
        key = seq.toString(QKeySequence.SequenceFormat.PortableText)
        if key == ",".join(shortcuts.effective(b, self._overrides)[:1]):
            return
        overrides = dict(self._overrides)
        if key:
            owner = shortcuts.owner_of(key, self._bindings, overrides, besides=b.id)
            if owner is not None and owner.locked:
                QMessageBox.warning(self, "Keyboard", f"{seq.toString(_NATIVE)} is the standard "
                                    f"shortcut for {owner.id}, and can't be used for anything else.")
                self._on_select()
                return
            if owner is not None:
                answer = QMessageBox.question(
                    self, "Keyboard",
                    f"{seq.toString(_NATIVE)} is already the shortcut for {owner.id}.\n\n"
                    f"Use it for {b.id} instead? {owner.id} will have no shortcut.")
                if answer != QMessageBox.StandardButton.Yes:
                    self._on_select()
                    return
                self._set(owner, "", overrides)
        self._set(b, key, overrides)
        self._commit(overrides)

    def _restore_one(self):
        b = self._current()
        if b is None:
            return
        overrides = dict(self._overrides)
        overrides.pop(b.id, None)
        for k in b.default:   # its default may have been given away since
            owner = shortcuts.owner_of(k, self._bindings, overrides, besides=b.id)
            if owner is not None and not owner.locked:
                # Back to its own default if that is free, else none.
                overrides.pop(owner.id, None)
                if any(shortcuts.owner_of(d, self._bindings, overrides, besides=owner.id)
                       for d in owner.default):
                    self._set(owner, "", overrides)
        self._commit(overrides)

    def _restore_all(self):
        if self._overrides and QMessageBox.question(
                self, "Keyboard", "Put every shortcut back to its default?"
        ) == QMessageBox.StandardButton.Yes:
            self._commit({})
