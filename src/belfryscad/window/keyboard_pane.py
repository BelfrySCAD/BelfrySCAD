"""Preferences > Keyboard: every command and its shortcut, changeable (#821).

The commands are a main window's `_shortcut_bindings` (see `shortcuts`).
A shortcut is changed where it is shown, as in other Mac apps: double-click
its row and press the new key.
A change is saved and applied to every open window at once, like the rest
of Preferences. A key already in use is offered back from the command that
has it; a standard shortcut (Cut, Save, ...) cannot be changed or taken.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView,
                               QKeySequenceEdit, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

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


_PLAIN = Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.KeypadModifier


class _KeyEdit(QKeySequenceEdit):
    """The Shortcut cell's editor. Takes Tab and Shift+Tab as keys (#830):
    Qt's lets Tab move the focus instead, so Indent could not be given Tab
    back, and records Shift+Tab as "Shift+Backtab", which is not the
    "Shift+Tab" every binding is stored as. Delete or Backspace on its own
    removes the shortcut, as in other Mac apps; nothing binds those bare."""

    def event(self, e):
        if e.type() == QEvent.Type.KeyPress and not e.modifiers() & ~_PLAIN:
            if e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                shift = e.key() == Qt.Key.Key_Backtab or e.modifiers() & Qt.KeyboardModifier.ShiftModifier
                self.setKeySequence(QKeySequence("Shift+Tab" if shift else "Tab"))
                self.editingFinished.emit()
                return True
            if e.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not e.modifiers():
                self.setKeySequence(QKeySequence())
                self.editingFinished.emit()
                return True
        return super().event(e)


class _KeyDelegate(QStyledItemDelegate):
    """Edits the Shortcut column in place: the first key pressed is the new
    shortcut and ends the edit; Escape or a click elsewhere leaves it alone."""

    def __init__(self, pane):
        super().__init__(pane)
        self._pane = pane

    def createEditor(self, parent, option, index):
        ed = _KeyEdit(parent)
        ed.setMaximumSequenceLength(1)
        ed._binding = self._pane._bindings[index.row()]
        ed.editingFinished.connect(lambda: self._finish(ed))
        return ed

    def setEditorData(self, editor, index):
        editor.setKeySequence(QKeySequence())   # empty: "press the new key"

    def _finish(self, ed):
        if getattr(ed, "_done", False):
            return
        ed._done = True
        b, seq = ed._binding, ed.keySequence()
        self.closeEditor.emit(ed)
        # After the editor is gone: the change refills the table, and may ask.
        QTimer.singleShot(0, lambda: self._pane._set_key(b, seq))

    def setModelData(self, editor, model, index):
        pass    # a click away without a key is not a change; _finish does the rest

    def eventFilter(self, obj, e):
        # The item view would take Tab, Shift+Tab and Return to move or commit.
        if e.type() == QEvent.Type.KeyPress and e.key() != Qt.Key.Key_Escape:
            return False
        return super().eventFilter(obj, e)


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
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.setItemDelegateForColumn(1, _KeyDelegate(self))
        # A double-click anywhere on the row edits its shortcut.
        self._table.doubleClicked.connect(
            lambda ix: ix.column() == 0 and self._table.edit(ix.siblingAtColumn(1)))
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self._table, 1)

        bottom = QHBoxLayout()
        self._note = QLabel()
        self._note.setStyleSheet("QLabel { color: gray; }")
        bottom.addWidget(self._note, 1)
        self._default_btn = QPushButton("Restore Default")
        self._default_btn.clicked.connect(self._restore_one)
        bottom.addWidget(self._default_btn)
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
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if b.locked:
                keys.setFlags(keys.flags() & ~Qt.ItemFlag.ItemIsEditable)
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
        self._default_btn.setEnabled(b is not None and not b.locked and b.id in self._overrides)
        self._note.setText("A standard shortcut: it can't be changed." if b is not None and b.locked
                           else "Double-click a shortcut, then press the new key. "
                                "Delete removes it, Escape keeps it.")

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

    def _set_key(self, b, seq: QKeySequence):
        """Give `b` the key `seq` (empty for none), asking first for a key
        another command has."""
        if b is None or b.locked:
            return
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
