"""Edit > Manage Templates…: add, edit and delete Insert Template's snippets.

Edits only ever write the user folder (`scad_templates.user_dir()`, which
OpenSCAD reads too). A built-in can be edited like any other; saving it
writes a user copy under the same name, which replaces it, and Revert to
Built-in deletes that copy again.

Not modal, like the other tool windows: a modal dialog stops the menu
bar's shortcuts reaching the main window.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from belfryscad import scad_templates
from belfryscad.scad_templates import MARKER, Template

_KEY = Qt.ItemDataRole.UserRole


class TemplateManager(QDialog):
    def __init__(self, parent=None, builtin_dir: Path | None = None, user_dir: Path | None = None):
        super().__init__(parent)
        self._builtin_dir = builtin_dir or scad_templates.BUILTIN_DIR
        self._user_dir = user_dir or scad_templates.user_dir()
        self.setWindowTitle("Manage Templates")
        self.resize(720, 440)

        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_current_changed)
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self._delete)
        folder_btn = QPushButton("Open Folder")
        folder_btn.setToolTip(str(self._user_dir))
        folder_btn.clicked.connect(self._open_folder)

        self.name = QLineEdit()
        self.content = QPlainTextEdit()
        self.content.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.content.setTabStopDistance(4 * self.content.fontMetrics().horizontalAdvance(" "))
        self.content.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.origin = QLabel()
        self.origin.setWordWrap(True)
        hint = QLabel(f"{MARKER} marks where the cursor is left (the end, without one). "
                      "Tab is one indent level, inserted as the editor's own indent.")
        hint.setWordWrap(True)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save)
        self.name.textEdited.connect(self._mark_dirty)
        self.content.textChanged.connect(self._mark_dirty)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)

        left = QVBoxLayout()
        left.addWidget(self.list)
        row = QHBoxLayout()
        for b in (new_btn, self.delete_btn, folder_btn):
            row.addWidget(b)
        left.addLayout(row)
        form = QFormLayout()
        form.addRow("Name:", self.name)
        form.addRow("Content:", self.content)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(self.origin)
        right.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.save_btn)
        buttons.addWidget(close_btn)
        right.addLayout(buttons)
        columns = QHBoxLayout(self)
        columns.addLayout(left, 1)
        columns.addLayout(right, 2)

        self._current: Template | None = None
        self._dirty = False
        self._reload()

    # -- model ---------------------------------------------------------------

    def _reload(self, select: str | None = None):
        self._templates, self._builtins, errors = scad_templates.load(self._builtin_dir, self._user_dir)
        self.list.blockSignals(True)
        self.list.clear()
        for key in sorted(self._templates, key=str.casefold):
            item = QListWidgetItem(key + ("  (built-in)" if self._templates[key].builtin else ""))
            item.setData(_KEY, key)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if errors:
            QMessageBox.warning(self, "Manage Templates", "\n".join(errors))
        self._select(select if select in self._templates else None)

    def _select(self, key: str | None):
        for i in range(self.list.count()):
            if key is None or self.list.item(i).data(_KEY) == key:
                self.list.setCurrentRow(i)
                return
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._show(None)

    def _show(self, t: Template | None):
        self._current = t
        self.name.blockSignals(True)
        self.content.blockSignals(True)
        self.name.setText(t.key if t else "")
        self.content.setPlainText(t.content if t else "")
        self.name.blockSignals(False)
        self.content.blockSignals(False)
        for w in (self.name, self.content):
            w.setEnabled(t is not None)
        self._dirty = t is not None and t.path is None      # a New not yet saved
        self.save_btn.setEnabled(self._dirty)
        if t is None:
            self.origin.setText("")
            self.delete_btn.setEnabled(False)
            return
        overrides = not t.builtin and t.key in self._builtins
        self.delete_btn.setText("Revert to Built-in" if overrides else "Delete")
        self.delete_btn.setEnabled(not t.builtin)
        if t.builtin:
            self.origin.setText("Built-in. Saving a change makes your own copy, which replaces it.")
        elif t.path is None:
            self.origin.setText("New; not saved yet.")
        else:
            self.origin.setText(f"Yours{', replacing the built-in' if overrides else ''}: {t.path}")

    def _mark_dirty(self, *_):
        if self._current is not None:
            self._dirty = True
            self.save_btn.setEnabled(True)

    # -- actions -------------------------------------------------------------

    def save(self) -> bool:
        t = self._current
        if t is None:
            return True
        key = self.name.text().strip()
        if not key:
            QMessageBox.warning(self, "Manage Templates", "A template needs a name.")
            return False
        other = self._templates.get(key)
        if other is not None and other is not t and not other.builtin:
            QMessageBox.warning(self, "Manage Templates", f"You already have a template named \"{key}\".")
            return False
        if self.content.toPlainText().count(MARKER) > 1:
            QMessageBox.warning(self, "Manage Templates", f"Use {MARKER} at most once: the cursor goes to one place.")
            return False
        t.key, t.content = key, self.content.toPlainText()
        try:
            scad_templates.save(t, self._user_dir)
        except OSError as e:
            QMessageBox.critical(self, "Manage Templates", f"Could not save the template: {e}")
            return False
        self._dirty = False
        self._reload(select=key)
        return True

    def _new(self):
        if not self._settle():
            return
        n, key = 1, "new template"
        while key in self._templates:
            n += 1
            key = f"new template {n}"
        t = Template(key, "", None, False)
        self._templates[key] = t
        item = QListWidgetItem(key)
        item.setData(_KEY, key)
        self.list.blockSignals(True)
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self._show(t)
        self.name.setFocus()
        self.name.selectAll()

    def _delete(self):
        t = self._current
        if t is None or t.builtin:
            return
        if t.path is not None:
            reverting = t.key in self._builtins
            question = (f"Delete your copy of \"{t.key}\" and go back to the built-in?" if reverting
                        else f"Delete the template \"{t.key}\"?\n\n{t.path}")
            if QMessageBox.question(self, "Manage Templates", question) != QMessageBox.StandardButton.Yes:
                return
            try:
                t.path.unlink()
            except OSError as e:
                QMessageBox.critical(self, "Manage Templates", f"Could not delete {t.path}: {e}")
                return
        self._dirty = False
        self._reload(select=t.key)   # a reverted one selects its built-in

    def _open_folder(self):
        self._user_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._user_dir)))

    # -- unsaved edits -------------------------------------------------------

    def _settle(self) -> bool:
        """Before leaving an edited template: Save, Discard, or stay."""
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self, "Manage Templates", f"Save the changes to \"{self.name.text().strip() or 'this template'}\"?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        if answer == QMessageBox.StandardButton.Discard:
            self._dirty = False
            if self._current is not None and self._current.path is None:
                self._reload()        # drop the unsaved New from the list
            return True
        return False

    def _on_current_changed(self, item, previous):
        if self._dirty and previous is not None:
            wanted = item.data(_KEY) if item else None
            if not self._settle():
                self.list.blockSignals(True)
                self.list.setCurrentItem(previous)
                self.list.blockSignals(False)
                return
            # Saving or discarding may have rebuilt the list, and `item` with it.
            if self.list.currentItem() is None or self.list.currentItem().data(_KEY) != wanted:
                self._select(wanted)
                return
        self._show(self._templates.get(item.data(_KEY)) if item else None)

    def closeEvent(self, event):
        if self._settle():
            event.accept()
        else:
            event.ignore()
