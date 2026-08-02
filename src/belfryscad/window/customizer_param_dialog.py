"""ParameterEditorDialog -- the Customizer Parameter Editor's GUI: lets the
user define a brand-new Customizer parameter (name, description, tab, type,
and type-specific constraint fields) or edit an existing one's value/
description/constraint in place, then hands off to customizer.py's
insert_parameter/replace_parameter to generate the OpenSCAD source text.

Renaming a parameter or moving it to a different tab via "Edit" are both
out of scope here -- renaming would need to rewrite every use-site in the
script; moving tabs would need to relocate its source lines. Both fields
are read-only when editing (see CustomizerPane's own TODO-tracked scope
note for a possible follow-up).
"""

from typing import Any, Optional

from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from belfryscad.window.customizer import (
    ParameterDef, _build_constraint, _coerce_option_value, _format_value,
    _parse_constraint, _parse_literal, _valid_param_name,
)

_TYPES = ["Number", "Slider", "Checkbox", "Dropdown", "String", "Vector"]


class ParameterEditorDialog(QDialog):
    def __init__(self, existing_tabs: list[str], existing_names: list[str] = (),
                 default_tab: str = 'Parameters', editing: Optional[ParameterDef] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Parameter" if editing else "Add Parameter")
        self.setMinimumWidth(480)
        self._editing = editing is not None
        self._existing_names = set(existing_names)
        self._result: Optional[tuple] = None

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._name_edit = QLineEdit()
        self._name_edit.setMinimumWidth(300)
        form.addRow("Name:", self._name_edit)
        self._desc_edit = QLineEdit()
        self._desc_edit.setMinimumWidth(300)
        form.addRow("Description:", self._desc_edit)

        self._tab_combo = QComboBox()
        self._tab_combo.setEditable(True)
        self._tab_combo.setMinimumWidth(300)
        for name in dict.fromkeys(list(existing_tabs) + ['Parameters', 'Global']):
            self._tab_combo.addItem(name)
        form.addRow("Tab:", self._tab_combo)

        self._type_combo = QComboBox()
        self._type_combo.setMinimumWidth(300)
        self._type_combo.addItems(_TYPES)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Type:", self._type_combo)

        self._stack = QStackedWidget()
        self._build_pages()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._stack)
        layout.addWidget(buttons)

        if editing:
            self._name_edit.setText(editing.name)
            self._name_edit.setEnabled(False)
            self._desc_edit.setText('' if editing.description == editing.name else editing.description)
            idx = self._tab_combo.findText(editing.tab)
            if idx >= 0:
                self._tab_combo.setCurrentIndex(idx)
            self._tab_combo.setEnabled(False)
            self._prefill_from(editing)
        else:
            idx = self._tab_combo.findText(default_tab)
            if idx >= 0:
                self._tab_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    # Page construction
    # ------------------------------------------------------------------

    def _make_page(self, rows) -> QWidget:
        # A label of None spans the field across both columns (no
        # indent); "" still occupies the (blank) label column, so the
        # field lines up with labeled rows above/below it.
        w = QWidget()
        f = QFormLayout(w)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f.setVerticalSpacing(4)
        for label, field in rows:
            if label is None:
                f.addRow(field)
            else:
                f.addRow(label, field)
        return w

    def _spin(self, minimum=-1e9, maximum=1e9, value=0.0, decimals=6) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(minimum, maximum)
        s.setDecimals(decimals)
        s.setValue(value)
        s.setMinimumWidth(120)
        return s

    def _build_pages(self):
        self._number_default = QLineEdit()
        self._number_default.setPlaceholderText("e.g. 5 or 2.5")
        self._number_default.setMinimumWidth(300)
        self._stack.addWidget(self._make_page([("Default:", self._number_default)]))

        self._slider_min = self._spin(value=0.0)
        self._slider_max = self._spin(value=100.0)
        self._slider_step = self._spin(minimum=0.0001, value=1.0)
        self._slider_default = self._spin(value=0.0)
        self._slider_int = QCheckBox("Integer values")
        self._slider_int.setChecked(True)
        self._stack.addWidget(self._make_page([
            ("Min:", self._slider_min), ("Max:", self._slider_max),
            ("Step:", self._slider_step), ("Default:", self._slider_default),
            (None, self._slider_int),
        ]))

        self._check_default = QCheckBox("Default checked")
        self._stack.addWidget(self._make_page([(None, self._check_default)]))

        self._dropdown_table = QTableWidget(0, 2)
        self._dropdown_table.setHorizontalHeaderLabels(["Label", "Value"])
        self._dropdown_table.verticalHeader().setVisible(False)
        self._dropdown_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._dropdown_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._dropdown_table.setColumnHidden(1, True)  # simple (unpaired) mode by default
        self._dropdown_table.setMinimumWidth(300)
        self._dropdown_table.setMinimumHeight(120)
        self._dropdown_table.itemChanged.connect(self._refresh_dropdown_default)
        self._dropdown_add_row_btn = QPushButton("+")
        self._dropdown_add_row_btn.setFixedWidth(28)
        self._dropdown_add_row_btn.setFlat(True)
        self._dropdown_add_row_btn.setToolTip("Add an option")
        self._dropdown_add_row_btn.clicked.connect(self._on_dropdown_add_row)
        self._dropdown_remove_row_btn = QPushButton("−")
        self._dropdown_remove_row_btn.setFixedWidth(28)
        self._dropdown_remove_row_btn.setFlat(True)
        self._dropdown_remove_row_btn.setToolTip("Remove the selected option")
        self._dropdown_remove_row_btn.clicked.connect(self._on_dropdown_remove_row)
        self._dropdown_paired_check = QCheckBox("Separate label from value")
        self._dropdown_paired_check.setToolTip(
            "Off: each option's displayed text is also its value (e.g. [a, b, c]).\n"
            "On: give each option its own internal value, distinct from what's displayed."
        )
        self._dropdown_paired_check.toggled.connect(self._on_dropdown_mode_toggled)
        dropdown_btn_row = QHBoxLayout()
        dropdown_btn_row.addWidget(self._dropdown_add_row_btn)
        dropdown_btn_row.addWidget(self._dropdown_remove_row_btn)
        dropdown_btn_row.addStretch()
        dropdown_btn_row.addWidget(self._dropdown_paired_check)
        dropdown_btn_widget = QWidget()
        dropdown_btn_widget.setLayout(dropdown_btn_row)
        self._dropdown_default = QComboBox()
        self._dropdown_default.setMinimumWidth(300)
        self._stack.addWidget(self._make_page([
            ("Options:", self._dropdown_table), ("", dropdown_btn_widget),
            ("Default:", self._dropdown_default),
        ]))

        self._string_default = QLineEdit()
        self._string_default.setMinimumWidth(300)
        self._string_maxlen_check = QCheckBox("Limit length")
        self._string_maxlen_spin = QSpinBox()
        self._string_maxlen_spin.setRange(1, 1000)
        self._string_maxlen_spin.setEnabled(False)
        self._string_maxlen_check.toggled.connect(self._string_maxlen_spin.setEnabled)
        maxlen_row = QHBoxLayout()
        maxlen_row.addWidget(self._string_maxlen_check)
        maxlen_row.addWidget(self._string_maxlen_spin)
        maxlen_widget = QWidget()
        maxlen_widget.setLayout(maxlen_row)
        self._stack.addWidget(self._make_page([
            ("Default:", self._string_default), ("Max length:", maxlen_widget),
        ]))

        self._vector_components = QLineEdit()
        self._vector_components.setPlaceholderText("10, 20, 30")
        self._vector_components.setMinimumWidth(300)
        self._vector_range_check = QCheckBox("Apply range to each component")
        self._vector_min = self._spin(value=0.0)
        self._vector_max = self._spin(value=100.0)
        self._vector_step = self._spin(minimum=0.0001, value=1.0)
        for w in (self._vector_min, self._vector_max, self._vector_step):
            w.setEnabled(False)
            self._vector_range_check.toggled.connect(w.setEnabled)
        self._stack.addWidget(self._make_page([
            ("Components:", self._vector_components), (None, self._vector_range_check),
            ("Min:", self._vector_min), ("Max:", self._vector_max), ("Step:", self._vector_step),
        ]))

    # ------------------------------------------------------------------
    # Behavior
    # ------------------------------------------------------------------

    def _on_type_changed(self, idx: int):
        self._stack.setCurrentIndex(idx)

    def _on_dropdown_mode_toggled(self, checked: bool):
        self._dropdown_table.setColumnHidden(1, not checked)
        self._refresh_dropdown_default()

    def _on_dropdown_add_row(self):
        row = self._dropdown_table.rowCount()
        self._dropdown_table.insertRow(row)
        item = QTableWidgetItem()
        self._dropdown_table.setItem(row, 0, item)
        self._dropdown_table.setItem(row, 1, QTableWidgetItem())
        self._dropdown_table.setCurrentCell(row, 0)
        self._dropdown_table.editItem(item)

    def _on_dropdown_remove_row(self):
        rows = sorted({idx.row() for idx in self._dropdown_table.selectedIndexes()}, reverse=True)
        if not rows:
            row = self._dropdown_table.currentRow()
            rows = [row] if row >= 0 else []
        for row in rows:
            self._dropdown_table.removeRow(row)
        self._refresh_dropdown_default()

    def _set_dropdown_rows(self, options: list[tuple[str, str]]):
        # Column 0 always shows the label text (equal to the value in
        # simple/unpaired mode) -- never blanked, unlike the old paired-
        # only table where a blank meant "same as the (only shown) value".
        self._dropdown_table.blockSignals(True)
        self._dropdown_table.setRowCount(0)
        for value, label in options:
            row = self._dropdown_table.rowCount()
            self._dropdown_table.insertRow(row)
            self._dropdown_table.setItem(row, 0, QTableWidgetItem(label))
            self._dropdown_table.setItem(row, 1, QTableWidgetItem(value))
        self._dropdown_table.blockSignals(False)
        self._refresh_dropdown_default()

    def _dropdown_options_list(self) -> list[tuple[str, str]]:
        paired = self._dropdown_paired_check.isChecked()
        options = []
        for row in range(self._dropdown_table.rowCount()):
            label_item = self._dropdown_table.item(row, 0)
            label_text = label_item.text().strip() if label_item else ''
            if not paired:
                # Simple mode: the label IS the value (e.g. real OpenSCAD's
                # bare [a, b, c] constraint) -- the Value column is hidden
                # and ignored regardless of any stale text left in it from
                # a prior paired-mode edit.
                if not label_text:
                    continue
                options.append((label_text, label_text))
                continue
            value_item = self._dropdown_table.item(row, 1)
            value = value_item.text().strip() if value_item else ''
            if not value:
                continue
            options.append((value, label_text or value))
        return options

    def _refresh_dropdown_default(self):
        current = self._dropdown_default.currentData()
        self._dropdown_default.clear()
        for value, label in self._dropdown_options_list():
            self._dropdown_default.addItem(label, value)
        if current is not None:
            idx = self._dropdown_default.findData(current)
            if idx >= 0:
                self._dropdown_default.setCurrentIndex(idx)

    def _prefill_from(self, param: ParameterDef):
        spec = _parse_constraint(param.constraint, param.default)
        val = param.default
        if isinstance(val, bool):
            self._type_combo.setCurrentText("Checkbox")
            self._check_default.setChecked(val)
        elif isinstance(val, list):
            self._type_combo.setCurrentText("Vector")
            self._vector_components.setText(', '.join(_format_value(v) for v in val))
            if spec['type'] == 'slider':
                self._vector_range_check.setChecked(True)
                self._vector_min.setValue(spec['min'])
                self._vector_max.setValue(spec['max'])
                self._vector_step.setValue(spec['step'])
        elif spec['type'] == 'dropdown':
            self._type_combo.setCurrentText("Dropdown")
            self._dropdown_paired_check.setChecked(any(v != lbl for v, lbl in spec['options']))
            self._set_dropdown_rows(spec['options'])
            idx = self._dropdown_default.findData(str(val))
            if idx >= 0:
                self._dropdown_default.setCurrentIndex(idx)
        elif isinstance(val, str):
            self._type_combo.setCurrentText("String")
            self._string_default.setText(val)
            if spec['type'] == 'string':
                self._string_maxlen_check.setChecked(True)
                self._string_maxlen_spin.setValue(spec['maxlen'])
        elif spec['type'] == 'slider':
            self._type_combo.setCurrentText("Slider")
            self._slider_min.setValue(spec['min'])
            self._slider_max.setValue(spec['max'])
            self._slider_step.setValue(spec['step'])
            self._slider_default.setValue(val)
            self._slider_int.setChecked(isinstance(val, int))
        else:
            self._type_combo.setCurrentText("Number")
            self._number_default.setText(_format_value(val))

    def _on_accept(self):
        result = self._validate_and_build()
        if result is None:
            return
        self._result = result
        self.accept()

    def _warn(self, title: str, text: str):
        QMessageBox.warning(self, title, text)

    def _validate_and_build(self) -> Optional[tuple]:
        name = self._name_edit.text().strip()
        if not self._editing:
            if not (_valid_param_name(name) and name.isidentifier()):
                self._warn("Invalid Name", "Enter a valid variable name (letters, digits, "
                                            "underscore; not a keyword; not $-prefixed).")
                return None
            if name in self._existing_names:
                self._warn("Duplicate Name", f"{name!r} is already used by another parameter.")
                return None

        description = self._desc_edit.text().strip()
        tab = self._tab_combo.currentText().strip() or 'Parameters'
        type_name = self._type_combo.currentText()

        if type_name == "Number":
            val = _parse_literal(self._number_default.text().strip())
            if val is None or isinstance(val, (bool, str, list)):
                self._warn("Invalid Default", "Enter a number, e.g. 5 or 2.5.")
                return None
            return name, val, description, tab, ''

        if type_name == "Slider":
            lo, hi, step = self._slider_min.value(), self._slider_max.value(), self._slider_step.value()
            if hi <= lo:
                self._warn("Invalid Range", "Max must be greater than min.")
                return None
            default = self._slider_default.value()
            if not (lo <= default <= hi):
                self._warn("Invalid Default", "Default must be within min/max.")
                return None
            if self._slider_int.isChecked():
                lo, hi, step, default = int(lo), int(hi), int(step) or 1, int(default)
            constraint = _build_constraint('slider', min=lo, max=hi, step=step)
            return name, default, description, tab, constraint

        if type_name == "Checkbox":
            return name, self._check_default.isChecked(), description, tab, ''

        if type_name == "Dropdown":
            options = self._dropdown_options_list()
            if len(options) < 2:
                self._warn("Invalid Options", "Enter at least two options, e.g. a:Label A, b:Label B.")
                return None
            idx = self._dropdown_default.currentIndex()
            if idx < 0:
                self._warn("Invalid Default", "Choose a default option.")
                return None
            default = _coerce_option_value(self._dropdown_default.itemData(idx))
            constraint = _build_constraint('dropdown', options=options)
            return name, default, description, tab, constraint

        if type_name == "String":
            default = self._string_default.text()
            maxlen = self._string_maxlen_spin.value() if self._string_maxlen_check.isChecked() else 0
            if maxlen and len(default) > maxlen:
                self._warn("Invalid Default", "Default text is longer than the max length.")
                return None
            constraint = _build_constraint('string', maxlen=maxlen)
            return name, default, description, tab, constraint

        if type_name == "Vector":
            parts = [p.strip() for p in self._vector_components.text().split(',') if p.strip()]
            if not (2 <= len(parts) <= 4):
                self._warn("Invalid Components", "Enter 2 to 4 comma-separated numbers, e.g. 10, 20, 30.")
                return None
            try:
                components: list[Any] = [float(p) if '.' in p else int(p) for p in parts]
            except ValueError:
                self._warn("Invalid Components", "Each component must be a number.")
                return None
            constraint = ''
            if self._vector_range_check.isChecked():
                lo, hi, step = self._vector_min.value(), self._vector_max.value(), self._vector_step.value()
                if hi <= lo:
                    self._warn("Invalid Range", "Max must be greater than min.")
                    return None
                constraint = _build_constraint('vector', min=lo, max=hi, step=step)
            return name, components, description, tab, constraint

        return None

    def result_values(self) -> Optional[tuple]:
        """(name, default, description, tab, constraint), or None if the
        dialog was cancelled."""
        return self._result
