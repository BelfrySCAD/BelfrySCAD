import json

import numpy as np

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QVBoxLayout, QWidget, QTabWidget,
    QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QDialogButtonBox, QLabel, QSlider,
    QPushButton, QLineEdit, QListWidget, QListWidgetItem, QColorDialog, QMessageBox,
    QFileDialog, QSplitter,
)
from PySide6.QtGui import QFont, QFontDatabase, QColor
from PySide6.QtCore import QSettings, Qt, Signal

from belfryscad.window.color_themes import (
    COLOR_THEMES, DEFAULT_COLOR_THEME, all_schemes, is_builtin,
    load_custom_schemes, save_custom_schemes, unique_scheme_name, SCHEME_COLOR_KEYS,
)
from belfryscad.window.viewport import Viewport
from belfryscad.window.data_viewers import (
    _diamond_faces, _dodecahedron_faces, _lit_marker_triangles,
)

_DEFAULTS = {
    "editor/fontFamily": "Menlo",
    "editor/fontSize": 13,
    "editor/indentSize": 4,
    "editor/showColumnGuide": True,
    "editor/columnGuide": 80,
    "viewport/viewerIPD": 65.0,         # mm — interpupillary distance
    "viewport/viewerScreenDist": 600.0, # mm — eye-to-screen distance
    "viewport/stereoDepthScale": 0.75,  # comfort trim multiplier
    "viewport/colorTheme": DEFAULT_COLOR_THEME,
    "colorSchemes/custom": "{}",  # JSON-encoded {name: {background, object, axes, unselected_vertex}}
}


def load_preference(key, type_=None):
    s = QSettings("BelfrySCAD", "BelfrySCAD")
    default = _DEFAULTS[key]
    if type_ is not None:
        return s.value(key, default, type=type_)
    return s.value(key, default)


def save_preferences(values: dict):
    s = QSettings("BelfrySCAD", "BelfrySCAD")
    for k, v in values.items():
        s.setValue(k, v)


class PreferencesDialog(QDialog):
    """Every control saves and applies its value immediately on change —
    there's no OK/Cancel, just a Close button."""

    def __init__(self, parent=None, on_change=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(360)
        self._on_change = on_change

        s = QSettings("BelfrySCAD", "BelfrySCAD")

        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(20, 20, 20, 20)

        tabs = QTabWidget()
        outer.addWidget(tabs)

        # --- Editor tab ---
        editor_tab = QWidget()
        form = QFormLayout(editor_tab)
        form.setSpacing(8)

        # Font family
        self._font_family = QComboBox()
        mono_fonts = [
            "Menlo", "Monaco", "Courier New", "Consolas",
            "Source Code Pro", "JetBrains Mono", "Fira Code", "SF Mono",
        ]
        available = set(QFontDatabase.families())
        filtered = [f for f in mono_fonts if f in available] or ["Courier New"]
        # Add current value even if not in the preset list
        current_family = s.value("editor/fontFamily", _DEFAULTS["editor/fontFamily"])
        if current_family not in filtered:
            filtered.insert(0, current_family)
        self._font_family.addItems(filtered)
        idx = self._font_family.findText(current_family)
        if idx >= 0:
            self._font_family.setCurrentIndex(idx)
        self._font_family.currentTextChanged.connect(
            lambda v: self._emit("editor/fontFamily", v)
        )
        form.addRow("Font:", self._font_family)

        # Font size
        self._font_size = QSpinBox()
        self._font_size.setRange(8, 30)
        self._font_size.setSuffix(" pt")
        self._font_size.setValue(s.value("editor/fontSize", _DEFAULTS["editor/fontSize"], type=int))
        self._font_size.valueChanged.connect(lambda v: self._emit("editor/fontSize", v))
        form.addRow("Font size:", self._font_size)

        # Indent size
        self._indent_size = QSpinBox()
        self._indent_size.setRange(1, 8)
        self._indent_size.setSuffix(" spaces")
        self._indent_size.setValue(s.value("editor/indentSize", _DEFAULTS["editor/indentSize"], type=int))
        self._indent_size.valueChanged.connect(lambda v: self._emit("editor/indentSize", v))
        form.addRow("Indent size:", self._indent_size)

        # Column guide
        guide_row = QHBoxLayout()
        guide_row.setSpacing(6)
        self._show_guide = QCheckBox("Show at column")
        self._show_guide.setChecked(s.value("editor/showColumnGuide", _DEFAULTS["editor/showColumnGuide"], type=bool))
        self._guide_column = QSpinBox()
        self._guide_column.setRange(1, 300)
        self._guide_column.setValue(s.value("editor/columnGuide", _DEFAULTS["editor/columnGuide"], type=int))
        self._guide_column.setEnabled(self._show_guide.isChecked())
        self._show_guide.toggled.connect(self._guide_column.setEnabled)
        self._show_guide.toggled.connect(lambda v: self._emit("editor/showColumnGuide", v))
        self._guide_column.valueChanged.connect(lambda v: self._emit("editor/columnGuide", v))
        guide_row.addWidget(self._show_guide)
        guide_row.addWidget(self._guide_column)
        guide_row.addStretch()
        form.addRow("Column guide:", guide_row)

        tabs.addTab(editor_tab, "Editor")

        # --- Viewport tab ---
        viewport_tab = QWidget()
        vp_form = QFormLayout(viewport_tab)
        vp_form.setSpacing(8)

        current_ipd = s.value("viewport/viewerIPD", _DEFAULTS["viewport/viewerIPD"], type=float)
        self._viewer_ipd = QDoubleSpinBox()
        self._viewer_ipd.setRange(40.0, 100.0)
        self._viewer_ipd.setSuffix(" mm")
        self._viewer_ipd.setDecimals(1)
        self._viewer_ipd.setValue(current_ipd)
        self._viewer_ipd.setToolTip("Distance between the centres of your pupils.")
        self._viewer_ipd.valueChanged.connect(lambda v: self._emit("viewport/viewerIPD", v))
        vp_form.addRow("Eye separation (IPD):", self._viewer_ipd)

        current_sdist = s.value("viewport/viewerScreenDist", _DEFAULTS["viewport/viewerScreenDist"], type=float)
        self._viewer_screen_dist = QDoubleSpinBox()
        self._viewer_screen_dist.setRange(300.0, 1500.0)
        self._viewer_screen_dist.setSuffix(" mm")
        self._viewer_screen_dist.setDecimals(0)
        self._viewer_screen_dist.setValue(current_sdist)
        self._viewer_screen_dist.setToolTip("Distance from your eyes to the screen.")
        self._viewer_screen_dist.valueChanged.connect(lambda v: self._emit("viewport/viewerScreenDist", v))
        vp_form.addRow("Screen distance:", self._viewer_screen_dist)

        scale_row = QHBoxLayout()
        scale_row.setSpacing(8)
        current_scale = s.value("viewport/stereoDepthScale", _DEFAULTS["viewport/stereoDepthScale"], type=float)
        self._stereo_scale = QSlider(Qt.Orientation.Horizontal)
        self._stereo_scale.setRange(25, 150)
        self._stereo_scale.setTickInterval(25)
        self._stereo_scale.setValue(int(round(current_scale * 100)))
        self._stereo_scale_label = QLabel(f"{int(round(current_scale * 100))}%")
        self._stereo_scale_label.setMinimumWidth(40)
        self._stereo_scale.valueChanged.connect(
            lambda v: self._stereo_scale_label.setText(f"{v}%")
        )
        self._stereo_scale.valueChanged.connect(
            lambda v: self._emit("viewport/stereoDepthScale", v / 100.0)
        )
        scale_row.addWidget(self._stereo_scale)
        scale_row.addWidget(self._stereo_scale_label)
        vp_form.addRow("Stereo depth scale:", scale_row)

        current_theme = s.value("viewport/colorTheme", _DEFAULTS["viewport/colorTheme"])
        theme_row = QHBoxLayout()
        theme_row.setSpacing(8)
        self._color_theme = QComboBox()
        self._reload_theme_items(current_theme)
        self._color_theme.currentTextChanged.connect(
            lambda v: self._emit("viewport/colorTheme", v)
        )
        theme_row.addWidget(self._color_theme, 1)
        manage_btn = QPushButton("Manage...")
        manage_btn.clicked.connect(self._open_scheme_manager)
        theme_row.addWidget(manage_btn)
        vp_form.addRow("Color theme:", theme_row)

        tabs.addTab(viewport_tab, "Viewport")
        tabs.tabBar().moveTab(1, 0)  # Viewport first
        tabs.setCurrentIndex(0)     # ...and selected by default

        # --- Close button ---
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        outer.addWidget(buttons)

    def _emit(self, key, value):
        save_preferences({key: value})
        if self._on_change:
            self._on_change()

    def _reload_theme_items(self, select: str = None):
        """(Re)populate the color-theme combo from `all_schemes()` --
        called at init and again after the Manager dialog closes, in case
        a custom scheme was added/renamed/deleted while it was open."""
        select = select or self._color_theme.currentText() or load_preference("viewport/colorTheme")
        self._color_theme.blockSignals(True)
        self._color_theme.clear()
        self._color_theme.addItems(sorted(all_schemes()))
        idx = self._color_theme.findText(select)
        self._color_theme.setCurrentIndex(idx if idx >= 0 else 0)
        self._color_theme.blockSignals(False)

    def _open_scheme_manager(self):
        dialog = ColorSchemeManagerDialog(parent=self)
        dialog.exec()
        # The active scheme may have been edited, renamed, or deleted
        # while the manager was open -- refresh the combo and re-apply
        # preferences unconditionally rather than trying to track exactly
        # what changed.
        self._reload_theme_items()
        self._emit("viewport/colorTheme", self._color_theme.currentText())


class _ColorSwatchButton(QWidget):
    """A clickable color swatch (a thin-black-outlined rectangle) followed
    by its "#RRGGBB" hex text -- clicking either opens `QColorDialog` to
    pick a new color. Colors are RGBA float tuples (0-1 range, alpha
    always 1.0), matching this app's existing color convention
    (`color_themes.py`) rather than `QColor`."""

    colorChanged = Signal()

    def __init__(self, color: tuple, parent=None):
        super().__init__(parent)
        self._color = tuple(color[:3]) + (1.0,)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._swatch = QLabel()
        self._swatch.setFixedSize(28, 18)
        self._hex_label = QLabel()
        layout.addWidget(self._swatch)
        layout.addWidget(self._hex_label)
        layout.addStretch(1)
        self._refresh()

    def _refresh(self):
        qc = self._qcolor()
        self._swatch.setStyleSheet(f"background-color: {qc.name()}; border: 1px solid black;")
        self._hex_label.setText(qc.name().upper())

    def _qcolor(self) -> QColor:
        r, g, b, _a = self._color
        return QColor.fromRgbF(r, g, b)

    def color(self) -> tuple:
        return self._color

    def setColor(self, color: tuple):
        self._color = tuple(color[:3]) + (1.0,)
        self._refresh()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            chosen = QColorDialog.getColor(self._qcolor(), self, "Choose Color")
            if chosen.isValid():
                self._color = (chosen.redF(), chosen.greenF(), chosen.blueF(), 1.0)
                self._refresh()
                self.colorChanged.emit()
        super().mousePressEvent(event)


class ColorSchemeEditorDialog(QDialog):
    """Name + 4 color-swatch rows (Background/Object/Axes/Vertex -- the
    last one labeled just "Vertex" in the UI even though the underlying
    data key is `unselected_vertex`). Used for New/Copy/Edit alike in
    `ColorSchemeManagerDialog`; the caller passes the starting name/colors
    and reads them back via `result_name()`/`result_colors()` after
    `exec()` returns Accepted."""

    _LABELS = [("background", "Background:"), ("object", "Object:"),
               ("axes", "Axes:"), ("unselected_vertex", "Vertex:")]

    def __init__(self, name: str, colors: dict, existing_names: set, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Color Scheme")
        self.setModal(True)
        self._existing_names = existing_names  # every OTHER scheme's name (collision check)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(8)
        self._name_edit = QLineEdit(name)
        form.addRow("Name:", self._name_edit)

        self._swatches: dict[str, _ColorSwatchButton] = {}
        for key, label in self._LABELS:
            swatch = _ColorSwatchButton(colors[key])
            self._swatches[key] = swatch
            form.addRow(label, swatch)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self):
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Color Scheme", "Name can't be empty.")
            return
        if name in self._existing_names:
            QMessageBox.warning(self, "Color Scheme", f'"{name}" is already in use.')
            return
        self.accept()

    def result_name(self) -> str:
        return self._name_edit.text().strip()

    def result_colors(self) -> dict:
        return {key: self._swatches[key].color() for key, _ in self._LABELS}


class _ColorSchemePreview(Viewport):
    """A real, live `Viewport` showing a simple octahedron (reusing
    `_diamond_faces`'s existing octahedron geometry -- same shape already
    used for `_PathViewport`'s "same_angle" bezier marker) with dodecahedron
    vertex markers at its 6 corners, so `ColorSchemeManagerDialog`'s list
    selection can be previewed in the actual rendering pipeline rather than
    a static swatch mockup.

    The octahedron mesh is uploaded once with `color=None`, which already
    tracks the live theme's object color fresh every draw (`renderer.
    _default_color`) -- no re-upload needed when the scheme changes, same
    as any other data-viewer mesh. Vertex markers bake their color directly
    into the uploaded triangle data (same as every other marker in this
    app), so those alone are rebuilt in `apply_scheme`."""

    _RADIUS = 1.0
    _MARKER_RADIUS = 0.16

    def __init__(self, parent=None):
        super().__init__(parent, selectable=False)
        self.setMinimumSize(200, 200)
        # apply_scheme() is typically called (via ColorSchemeManagerDialog's
        # _update_preview) before GL has ever initialized -- the dialog
        # populates its list, which selects a row and previews it, all
        # synchronously in __init__, well before the widget is first shown/
        # painted. schedule_load only remembers ONE pending callback, so
        # apply_scheme must NOT call schedule_load itself (that would
        # clobber the _load_mesh callback registered below and the mesh
        # would never get uploaded) -- it just records the latest requested
        # colors, and _load_mesh applies them once GL is actually ready.
        self._pending_colors = COLOR_THEMES[DEFAULT_COLOR_THEME]
        self.schedule_load(self._load_mesh)

    def _load_mesh(self):
        tris = _diamond_faces(self._RADIUS, False)
        positions, normals = [], []
        for v0, v1, v2 in tris:
            n = np.cross(v1 - v0, v2 - v0)
            ln = np.linalg.norm(n)
            if ln > 0:
                n = n / ln
            positions.extend([v0, v1, v2])
            normals.extend([n, n, n])
        self._renderer.upload_mesh(
            np.array(positions, dtype=np.float32), np.array(normals, dtype=np.float32)
        )
        bb = np.array([self._RADIUS] * 3, dtype=np.float32)
        self.frame_scene(-bb, bb)
        self._apply_colors_now(self._pending_colors)

    def apply_scheme(self, colors: dict):
        self._pending_colors = colors
        if self._ctx is not None:
            self._apply_colors_now(colors)
        self.update()

    def _apply_colors_now(self, colors: dict):
        self._renderer.bg_color = colors["background"]
        self._renderer._default_color = colors["object"]
        self._renderer.axes_color = colors["axes"]
        self._renderer.unselected_vertex_color = colors["unselected_vertex"]
        self._rebuild_markers(colors["unselected_vertex"])

    def _rebuild_markers(self, color: tuple):
        self._renderer.clear_points()
        unit_faces = _dodecahedron_faces(1.0, False)
        marker_color = np.array(color[:3], dtype=np.float32)
        marker_tris = []
        for pt in (np.array(v, dtype=np.float32) for v in (
            (self._RADIUS, 0, 0), (-self._RADIUS, 0, 0),
            (0, self._RADIUS, 0), (0, -self._RADIUS, 0),
            (0, 0, self._RADIUS), (0, 0, -self._RADIUS),
        )):
            marker_tris.extend(_lit_marker_triangles(pt, self._MARKER_RADIUS, unit_faces, marker_color))
        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))


class ColorSchemeManagerDialog(QDialog):
    """List every scheme (built-in + custom), with New/Copy/Edit/Delete/
    Import/Export buttons -- same list-plus-button-row shape as
    `LibraryManagerWindow` (`library_manager.py`), not a new UI pattern.
    Built-in themes are shown (so they're browsable/exportable/copyable)
    but not editable or deletable -- `_DEFAULTS`-derived data, not user
    data."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Color Schemes")
        self.setMinimumSize(700, 400)
        self.resize(760, 440)

        self._custom = load_custom_schemes()

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._update_button_states)
        self._list.currentItemChanged.connect(self._update_preview)
        left_layout.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._btn_new = QPushButton("New...")
        self._btn_new.clicked.connect(self._on_new)
        self._btn_copy = QPushButton("Copy...")
        self._btn_copy.clicked.connect(self._on_copy)
        self._btn_edit = QPushButton("Edit...")
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        for b in (self._btn_new, self._btn_copy, self._btn_edit, self._btn_delete):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        self._btn_import = QPushButton("Import...")
        self._btn_import.clicked.connect(self._on_import)
        self._btn_export = QPushButton("Export...")
        self._btn_export.clicked.connect(self._on_export)
        btn_row.addWidget(self._btn_import)
        btn_row.addWidget(self._btn_export)
        left_layout.addLayout(btn_row)

        splitter.addWidget(left)

        self._preview = _ColorSchemePreview()
        splitter.addWidget(self._preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 440])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        outer.addWidget(buttons)

        self._reload_list()

    def _update_preview(self, *_):
        name = self._selected_name()
        colors = all_schemes().get(name) if name else None
        self._preview.apply_scheme(colors or COLOR_THEMES[DEFAULT_COLOR_THEME])

    # -- list population --------------------------------------------------

    def _reload_list(self, select: str = None):
        self._list.blockSignals(True)
        self._list.clear()
        for name in sorted(all_schemes()):
            label = f"{name} (built-in)" if is_builtin(name) else name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._list.addItem(item)
            if name == select:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        if select is None and self._list.count() and self._list.currentRow() < 0:
            self._list.setCurrentRow(0)
        self._update_button_states()
        self._update_preview()

    def _selected_name(self) -> str:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_button_states(self, *_):
        name = self._selected_name()
        editable = name is not None and not is_builtin(name)
        self._btn_copy.setEnabled(name is not None)
        self._btn_edit.setEnabled(editable)
        self._btn_delete.setEnabled(editable)
        self._btn_export.setEnabled(name is not None)

    # -- actions ------------------------------------------------------------

    def _save_and_reload(self, select: str):
        save_custom_schemes(self._custom)
        self._reload_list(select=select)

    def _on_new(self):
        default_colors = {k: (0.5, 0.5, 0.5, 1.0) for k in SCHEME_COLOR_KEYS}
        name = unique_scheme_name("New Scheme", all_schemes())
        self._open_editor(name, default_colors)

    def _on_copy(self):
        src = self._selected_name()
        if src is None:
            return
        colors = all_schemes()[src]
        name = unique_scheme_name(f"{src} copy", all_schemes())
        self._open_editor(name, colors)

    def _on_edit(self):
        name = self._selected_name()
        if name is None or is_builtin(name):
            return
        self._open_editor(name, self._custom[name], editing=name)

    def _open_editor(self, name: str, colors: dict, editing: str = None):
        existing = set(all_schemes()) - ({editing} if editing else set())
        dialog = ColorSchemeEditorDialog(name, colors, existing, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dialog.result_name()
        new_colors = dialog.result_colors()
        if editing and editing != new_name:
            del self._custom[editing]
        self._custom[new_name] = new_colors
        self._save_and_reload(new_name)

    def _on_delete(self):
        name = self._selected_name()
        if name is None or is_builtin(name):
            return
        reply = QMessageBox.question(
            self, "Delete Color Scheme", f'Delete "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        del self._custom[name]
        self._save_and_reload(select=None)

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Color Scheme", "", "Color Scheme (*.json)")
        if not path:
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            name = str(data["name"])
            colors = {k: tuple(data[k]) for k in SCHEME_COLOR_KEYS}
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self, "Import Color Scheme", f"Couldn't read this file:\n{e}")
            return
        name = unique_scheme_name(name, all_schemes())
        self._custom[name] = colors
        self._save_and_reload(select=name)

    def _on_export(self):
        name = self._selected_name()
        if name is None:
            return
        colors = all_schemes()[name]
        path, _ = QFileDialog.getSaveFileName(self, "Export Color Scheme", f"{name}.json", "Color Scheme (*.json)")
        if not path:
            return
        data = {"name": name, **{k: list(colors[k]) for k in SCHEME_COLOR_KEYS}}
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
