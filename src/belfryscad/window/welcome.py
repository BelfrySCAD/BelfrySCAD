"""The Welcome window -- OpenSCAD's LaunchingScreen.

Shown at startup when no file was named on the command line: New, Open,
the recent files and the bundled examples, one double-click from any of
them. "Show this window at startup" is preference `app/showWelcome`, also
on the Editor tab of Preferences; Help > Welcome Screen... opens it anyway.

Not modal: a modal dialog stops the menu bar's shortcuts reaching the main
window, and nothing here needs the user's answer before carrying on.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from belfryscad.settings import app_settings

SHOW_KEY = "app/showWelcome"
_PATH = Qt.ItemDataRole.UserRole


class WelcomeDialog(QDialog):
    """`window` supplies the actions: _new_document, _open_file,
    open_file_by_path (recent files) and _open_example (examples, which
    open as an editable copy)."""

    def __init__(self, window, recents: list[str], examples: list[tuple[str, list[Path]]]):
        super().__init__(window)
        self._window = window
        self.setWindowTitle("Welcome to BelfrySCAD")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(640, 420)

        new_btn = QPushButton("New")
        new_btn.clicked.connect(lambda: self._then(window._new_document))
        open_btn = QPushButton("Open…")
        open_btn.clicked.connect(lambda: self._then(window._open_file))

        self.recent_list = QListWidget()
        for path in recents:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(_PATH, path)
            self.recent_list.addItem(item)
        self.recent_list.itemActivated.connect(
            lambda item: self._then(lambda: window.open_file_by_path(item.data(_PATH))))

        self.example_tree = QTreeWidget()
        self.example_tree.setHeaderHidden(True)
        for category, paths in examples:
            top = QTreeWidgetItem([category])
            for p in paths:
                leaf = QTreeWidgetItem([p.stem.replace("_", " ")])
                leaf.setData(0, _PATH, str(p))
                top.addChild(leaf)
            self.example_tree.addTopLevelItem(top)
        self.example_tree.expandAll()
        self.example_tree.itemActivated.connect(self._example_activated)

        left = QVBoxLayout()
        buttons = QHBoxLayout()
        buttons.addWidget(new_btn)
        buttons.addWidget(open_btn)
        left.addLayout(buttons)
        left.addWidget(QLabel("Recent files:" if recents else "Recent files: (none yet)"))
        left.addWidget(self.recent_list)
        right = QVBoxLayout()
        right.addWidget(QLabel("Examples:"))
        right.addWidget(self.example_tree)
        columns = QHBoxLayout()
        columns.addLayout(left)
        columns.addLayout(right)

        self.show_at_startup = QCheckBox("Show this window at startup")
        self.show_at_startup.setChecked(app_settings().value(SHOW_KEY, True, type=bool))
        self.show_at_startup.toggled.connect(lambda v: app_settings().setValue(SHOW_KEY, bool(v)))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(self.show_at_startup)
        bottom.addStretch()
        bottom.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(columns)
        layout.addLayout(bottom)

    def _example_activated(self, item):
        path = item.data(0, _PATH)
        if path:   # a category row just expands/collapses
            self._then(lambda: self._window._open_example(path))

    def _then(self, action):
        # Close first: Open... raises its own file dialog, which should not
        # appear behind this one.
        self.close()
        action()
