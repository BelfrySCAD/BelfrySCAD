from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QVBoxLayout, QWidget, QTabWidget,
    QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox, QDialogButtonBox, QLabel, QSlider,
)
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtCore import QSettings, Qt

from belfryscad.window.color_themes import COLOR_THEMES, DEFAULT_COLOR_THEME

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
        self._color_theme = QComboBox()
        self._color_theme.addItems(sorted(COLOR_THEMES))
        idx = self._color_theme.findText(current_theme)
        self._color_theme.setCurrentIndex(idx if idx >= 0 else 0)
        self._color_theme.currentTextChanged.connect(
            lambda v: self._emit("viewport/colorTheme", v)
        )
        vp_form.addRow("Color theme:", self._color_theme)

        tabs.addTab(viewport_tab, "Viewport")
        tabs.tabBar().moveTab(1, 0)  # Viewport first

        # --- Close button ---
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        outer.addWidget(buttons)

    def _emit(self, key, value):
        save_preferences({key: value})
        if self._on_change:
            self._on_change()
