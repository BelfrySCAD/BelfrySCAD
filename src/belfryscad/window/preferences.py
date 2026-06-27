from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QVBoxLayout,
    QComboBox, QSpinBox, QCheckBox, QDialogButtonBox, QLabel, QSlider,
)
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtCore import QSettings, Qt

_DEFAULTS = {
    "editor/fontFamily": "Menlo",
    "editor/fontSize": 13,
    "editor/indentSize": 4,
    "editor/showColumnGuide": True,
    "editor/columnGuide": 80,
    "viewport/stereoEyeSep": 6.5,  # percentage of camera distance
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

        sep_row = QHBoxLayout()
        sep_row.setSpacing(8)
        # Slider range 10–200, each tick = 0.1%, so value 65 = 6.5%
        self._eye_sep = QSlider(Qt.Orientation.Horizontal)
        self._eye_sep.setRange(10, 200)
        self._eye_sep.setTickInterval(10)
        current_sep = s.value("viewport/stereoEyeSep", _DEFAULTS["viewport/stereoEyeSep"], type=float)
        self._eye_sep.setValue(int(round(current_sep * 10)))
        self._eye_sep_label = QLabel(f"{current_sep:.1f}%")
        self._eye_sep_label.setMinimumWidth(40)
        self._eye_sep.valueChanged.connect(
            lambda v: self._eye_sep_label.setText(f"{v / 10.0:.1f}%")
        )
        sep_row.addWidget(self._eye_sep)
        sep_row.addWidget(self._eye_sep_label)
        vp_form.addRow("Stereo eye separation:", sep_row)

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
            "viewport/stereoEyeSep": self._eye_sep.value() / 10.0,
        }
