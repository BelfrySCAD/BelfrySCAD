"""The Welcome window -- OpenSCAD's LaunchingScreen, laid out like it.

Shown at startup when no file was named on the command line: the logo,
New / Open / Help, the recent files and the bundled examples, each opened
by a double-click or by selecting it and pressing Open Recent / Open
Example. "Don't show again" clears preference `app/showWelcome`, which is
also on the Editor tab of Preferences; Help > Welcome Screen... opens the
window anyway.

The background (a sweep of the window colour across the base colour) and
the lists are drawn from the palette, so dark mode follows along. Only the
buttons carry fixed colours: the logo's green, as OpenSCAD's carry its own.

Not modal: a modal dialog stops the menu bar's shortcuts reaching the main
window, and nothing here needs the user's answer before carrying on.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from belfryscad.settings import app_settings
from belfryscad.window.about import DOCS_URL, about_info

SHOW_KEY = "app/showWelcome"
_PATH = Qt.ItemDataRole.UserRole
_LOGO = Path(__file__).parent.parent / "resources" / "logo.png"
# The olive of the logo's cube, for "Belfry" as OpenSCAD colours its "Open".
_OLIVE = "#8a8412"

_STYLE = """
QPushButton { color: white; border: 1px solid #2c6e33; border-radius: 3px; padding: 4px 14px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #5fbf6a, stop:1 #2f8f3d); }
QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #6fcf7a, stop:1 #38a047); }
QPushButton:pressed { background: #2a7a36; }
QPushButton:disabled { color: #f4f4f4; border-color: #a8a8a8;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #cfcfcf, stop:1 #b4b4b4); }
QListWidget, QTreeWidget { background: transparent; border: none; }
"""


class WelcomeDialog(QDialog):
    """`window` supplies the actions: _new_document, _open_file,
    open_file_by_path (recent files) and _open_example (examples, which
    open as an editable copy)."""

    def __init__(self, window, recents: list[str], examples: list[tuple[str, list[Path]]]):
        super().__init__(window)
        self._window = window
        self.setWindowTitle("Welcome to BelfrySCAD")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(_STYLE)
        self.resize(620, 560)
        info = about_info()

        # -- header: logo, name, tagline
        logo = QLabel()
        pix = QPixmap(str(_LOGO))
        if not pix.isNull():
            ratio = self.devicePixelRatioF()
            pix = pix.scaled(int(96 * ratio), int(96 * ratio), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            pix.setDevicePixelRatio(ratio)
            logo.setPixmap(pix)
        title = QLabel(f"<span style='font-size:22pt; font-weight:bold'>"
                       f"<span style='color:{_OLIVE}'>Belfry</span>SCAD</span>")
        tagline = QLabel(info["description"])
        titles = QVBoxLayout()
        titles.addStretch()
        titles.addWidget(title)
        titles.addWidget(tagline)
        titles.addStretch()
        header = QHBoxLayout()
        header.addWidget(logo)
        header.addSpacing(24)
        header.addLayout(titles)
        header.addStretch()

        # -- left column: New / Open / Help, then the recent files
        new_btn = QPushButton("New")
        new_btn.clicked.connect(lambda: self._then(window._new_document))
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(lambda: self._then(window._open_file))
        help_btn = QPushButton("Help")
        help_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DOCS_URL)))
        top_buttons = QHBoxLayout()
        for b in (new_btn, open_btn, help_btn):
            top_buttons.addWidget(b)

        self.recent_list = QListWidget()
        self.recent_list.setFrameShape(QFrame.Shape.NoFrame)
        for path in recents:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(_PATH, path)
            self.recent_list.addItem(item)
        if not recents:
            empty = QListWidgetItem("(none yet)")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recent_list.addItem(empty)
        self.recent_list.itemActivated.connect(self._open_recent)
        self.open_recent_btn = QPushButton("Open Recent")
        self.open_recent_btn.setEnabled(False)
        self.open_recent_btn.clicked.connect(lambda: self._open_recent(self.recent_list.currentItem()))
        self.recent_list.currentItemChanged.connect(
            lambda item, _prev: self.open_recent_btn.setEnabled(bool(item and item.data(_PATH))))

        # -- right column: the examples, by category
        self.example_tree = QTreeWidget()
        self.example_tree.setHeaderHidden(True)
        self.example_tree.setFrameShape(QFrame.Shape.NoFrame)
        for category, paths in examples:
            top = QTreeWidgetItem([category])
            for p in paths:
                leaf = QTreeWidgetItem([p.stem.replace("_", " ")])
                leaf.setData(0, _PATH, str(p))
                leaf.setToolTip(0, p.name)
                top.addChild(leaf)
            self.example_tree.addTopLevelItem(top)
        self.example_tree.itemActivated.connect(self._open_example)
        self.open_example_btn = QPushButton("Open Example")
        self.open_example_btn.setEnabled(False)
        self.open_example_btn.clicked.connect(lambda: self._open_example(self.example_tree.currentItem()))
        self.example_tree.currentItemChanged.connect(
            lambda item, _prev: self.open_example_btn.setEnabled(bool(item and item.data(0, _PATH))))

        # Two columns on one grid, so the Recents and Examples labels, the
        # lists and the two Open buttons each share a row across both.
        grid = QGridLayout()
        grid.setHorizontalSpacing(32)
        grid.addLayout(top_buttons, 0, 0)
        grid.addWidget(QLabel("Examples"), 0, 1, Qt.AlignmentFlag.AlignBottom)
        grid.addWidget(QLabel("Recents"), 1, 0)
        grid.addWidget(self.recent_list, 2, 0)
        grid.addWidget(self.example_tree, 1, 1, 2, 1)
        grid.addWidget(self.open_recent_btn, 3, 0)
        grid.addWidget(self.open_example_btn, 3, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(2, 1)

        # -- footer
        self.dont_show = QCheckBox("Don't show again")
        self.dont_show.setChecked(not app_settings().value(SHOW_KEY, True, type=bool))
        self.dont_show.toggled.connect(lambda v: app_settings().setValue(SHOW_KEY, not v))
        footer = QHBoxLayout()
        footer.addWidget(self.dont_show)
        footer.addStretch()
        footer.addWidget(QLabel(f"BelfrySCAD {info['version']}"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 18)
        layout.addLayout(header)
        layout.addSpacing(28)
        layout.addLayout(grid, 1)
        layout.addSpacing(20)
        layout.addLayout(footer)

    def paintEvent(self, event):
        """The base colour, with the window colour swept up across the lower
        part in one curve -- LaunchingScreen's backdrop, in palette colours."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), self.palette().base())
        sweep = QPainterPath(QPointF(0, h))
        sweep.lineTo(0, h * 0.72)
        sweep.cubicTo(w * 0.35, h * 0.45, w * 0.65, h * 0.36, w, h * 0.34)
        sweep.lineTo(w, h)
        sweep.closeSubpath()
        p.fillPath(sweep, self.palette().window())

    def _open_recent(self, item):
        path = item.data(_PATH) if item else None
        if path:
            self._then(lambda: self._window.open_file_by_path(path))

    def _open_example(self, item):
        path = item.data(0, _PATH) if item else None
        if path:   # a category row just expands/collapses
            self._then(lambda: self._window._open_example(path))

    def _then(self, action):
        # Close first: Open raises its own file dialog, which should not
        # appear behind this one.
        self.close()
        action()
