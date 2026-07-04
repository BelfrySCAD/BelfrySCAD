from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QVBoxLayout,
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(340)

        s = QSettings("BelfrySCAD", "BelfrySCAD")

        outer = QVBoxLayout(self)
        outer.setSpacing(16)
        outer.setContentsMargins(20, 20, 20, 20)

        # --- Editor section ---
        editor_label = QLabel("<b>Editor</b>")
        outer.addWidget(editor_label)

        form = QFormLayout()
        form.setSpacing(8)
        form.setContentsMargins(12, 0, 0, 0)

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
        form.addRow("Font:", self._font_family)

        # Font size
        self._font_size = QSpinBox()
        self._font_size.setRange(8, 30)
        self._font_size.setSuffix(" pt")
        self._font_size.setValue(s.value("editor/fontSize", _DEFAULTS["editor/fontSize"], type=int))
        form.addRow("Font size:", self._font_size)

        # Indent size
        self._indent_size = QSpinBox()
        self._indent_size.setRange(1, 8)
        self._indent_size.setSuffix(" spaces")
        self._indent_size.setValue(s.value("editor/indentSize", _DEFAULTS["editor/indentSize"], type=int))
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
        guide_row.addWidget(self._show_guide)
        guide_row.addWidget(self._guide_column)
        guide_row.addStretch()
        form.addRow("Column guide:", guide_row)

        outer.addLayout(form)

        # --- Viewport section ---
        outer.addWidget(QLabel("<b>Viewport</b>"))

        vp_form = QFormLayout()
        vp_form.setSpacing(8)
        vp_form.setContentsMargins(12, 0, 0, 0)

        current_ipd = s.value("viewport/viewerIPD", _DEFAULTS["viewport/viewerIPD"], type=float)
        self._viewer_ipd = QDoubleSpinBox()
        self._viewer_ipd.setRange(40.0, 100.0)
        self._viewer_ipd.setSuffix(" mm")
        self._viewer_ipd.setDecimals(1)
        self._viewer_ipd.setValue(current_ipd)
        self._viewer_ipd.setToolTip("Distance between the centres of your pupils.")
        vp_form.addRow("Eye separation (IPD):", self._viewer_ipd)

        current_sdist = s.value("viewport/viewerScreenDist", _DEFAULTS["viewport/viewerScreenDist"], type=float)
        self._viewer_screen_dist = QDoubleSpinBox()
        self._viewer_screen_dist.setRange(300.0, 1500.0)
        self._viewer_screen_dist.setSuffix(" mm")
        self._viewer_screen_dist.setDecimals(0)
        self._viewer_screen_dist.setValue(current_sdist)
        self._viewer_screen_dist.setToolTip("Distance from your eyes to the screen.")
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
        scale_row.addWidget(self._stereo_scale)
        scale_row.addWidget(self._stereo_scale_label)
        vp_form.addRow("Stereo depth scale:", scale_row)

        current_theme = s.value("viewport/colorTheme", _DEFAULTS["viewport/colorTheme"])
        self._color_theme = QComboBox()
        self._color_theme.addItems(sorted(COLOR_THEMES))
        idx = self._color_theme.findText(current_theme)
        self._color_theme.setCurrentIndex(idx if idx >= 0 else 0)
        vp_form.addRow("Color theme:", self._color_theme)

        outer.addLayout(vp_form)

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def get_values(self) -> dict:
        return {
            "editor/fontFamily": self._font_family.currentText(),
            "editor/fontSize": self._font_size.value(),
            "editor/indentSize": self._indent_size.value(),
            "editor/showColumnGuide": self._show_guide.isChecked(),
            "editor/columnGuide": self._guide_column.value(),
            "viewport/viewerIPD": self._viewer_ipd.value(),
            "viewport/viewerScreenDist": self._viewer_screen_dist.value(),
            "viewport/stereoDepthScale": self._stereo_scale.value() / 100.0,
            "viewport/colorTheme": self._color_theme.currentText(),
        }
