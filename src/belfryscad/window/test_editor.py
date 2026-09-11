"""The test editor dialog: every field of one `[[test]]` block.

Reached by double-clicking a test in the Testing pane, and by Add Test — the
same dialog either way, so a new test is written exactly like an edited one.

Everything a TestCase carries is here on purpose. A field the dialog cannot
express is a field the user has to go and hand-edit the .scadtest for, which
would defeat having a dialog at all.

The two list-ish fields take TOML rather than inventing a syntax: `set_vars`
holds TOML values (a string, a number, a list) and is parsed with tomllib, so
what the user types means exactly what it would mean in the file.
"""
from __future__ import annotations

import tomllib

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from belfryscad.scadtest import TestCase, _toml_literal


def _mono(widget):
    widget.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return widget


class TestEditDialog(QDialog):
    """Edit (or create) one test. `result_test()` is the TestCase, valid only
    after exec() returns Accepted."""

    __test__ = False        # not a pytest class, despite the name

    def __init__(self, test: TestCase | None = None, existing_names=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Test" if test else "New Test")
        self.setModal(True)
        self.resize(640, 620)
        self._existing = {n for n in existing_names if not (test and n == test.name)}
        self._result = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._name = QLineEdit(test.name if test else "")
        form.addRow("Name", self._name)

        self._timeout = QSpinBox()
        self._timeout.setRange(1, 86400)
        self._timeout.setSuffix(" s")
        self._timeout.setValue(test.timeout if test else 60)
        form.addRow("Timeout", self._timeout)

        self._source = QComboBox()
        self._source.addItems(["Inline script", "Script file"])
        form.addRow("Source", self._source)

        self._stack = QStackedWidget()
        self._script = _mono(QPlainTextEdit(test.script if test and test.script else ""))
        self._script.setPlaceholderText("include <lib.scad>\nused();")
        self._stack.addWidget(self._script)
        file_page = QWidget()
        file_row = QHBoxLayout(file_page)
        file_row.setContentsMargins(0, 0, 0, 0)
        self._script_file = QLineEdit(test.script_file if test and test.script_file else "")
        self._script_file.setPlaceholderText("relative to the .scadtest file")
        file_row.addWidget(QLabel("File"))
        file_row.addWidget(self._script_file, 1)
        self._stack.addWidget(file_page)
        self._source.currentIndexChanged.connect(self._stack.setCurrentIndex)
        if test and test.script_file:
            self._source.setCurrentIndex(1)
        form.addRow("", self._stack)        # indented under the Source dropdown
        layout.addLayout(form, 1)

        vars_box = QGroupBox("Variable overrides")
        vars_layout = QHBoxLayout(vars_box)
        self._vars = QTableWidget(0, 2)
        self._vars.setHorizontalHeaderLabels(["Name", "Value (TOML)"])
        self._vars.verticalHeader().setVisible(False)
        self._vars.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._vars.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._vars.setMinimumHeight(110)
        self._vars.setMaximumHeight(170)
        _mono(self._vars)
        vars_layout.addWidget(self._vars, 1)
        var_buttons = QVBoxLayout()
        add_var = QPushButton("+")
        add_var.setFixedWidth(32)
        add_var.setToolTip("Add a variable override")
        add_var.clicked.connect(lambda: self._add_var_row("", ""))
        remove_var = QPushButton("−")
        remove_var.setFixedWidth(32)
        remove_var.setToolTip("Remove the selected override")
        remove_var.clicked.connect(self._remove_var_row)
        var_buttons.addWidget(add_var)
        var_buttons.addWidget(remove_var)
        var_buttons.addStretch(1)
        vars_layout.addLayout(var_buttons)
        layout.addWidget(vars_box)
        for name, value in (test.set_vars if test else {}).items():
            self._add_var_row(name, _toml_literal(value))

        expect = QGroupBox("Expectations")
        ex = QFormLayout(expect)
        self._expect_success = QCheckBox("The script must evaluate without error")
        self._expect_success.setChecked(test.expect_success if test else True)
        ex.addRow(self._expect_success)
        self._echoes = _mono(QPlainTextEdit("\n".join(test.assert_echoes) if test else ""))
        self._echoes.setPlaceholderText("one expected ECHO per line")
        self._echoes.setMaximumHeight(70)
        ex.addRow("Echoes", self._echoes)
        self._no_echoes = QCheckBox("…and no others")
        self._no_echoes.setChecked(test.assert_no_echoes if test else True)
        ex.addRow("", self._no_echoes)      # field column, under the Echoes box
        self._warnings = _mono(QPlainTextEdit("\n".join(test.assert_warnings) if test else ""))
        self._warnings.setPlaceholderText("one expected WARNING per line")
        self._warnings.setMaximumHeight(70)
        ex.addRow("Warnings", self._warnings)
        self._no_warnings = QCheckBox("…and no others")
        self._no_warnings.setChecked(test.assert_no_warnings if test else True)
        ex.addRow("", self._no_warnings)
        layout.addWidget(expect)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._name.setFocus()

    def result_test(self) -> TestCase | None:
        return self._result

    def _accept(self):
        name = self._name.text().strip()
        if not name:
            return self._complain("A test needs a name.")
        if name in self._existing:
            return self._complain(f"There is already a test called {name!r} in this file.")

        inline = self._source.currentIndex() == 0
        script = self._script.toPlainText() if inline else None
        script_file = None if inline else self._script_file.text().strip()
        if inline and not script.strip():
            return self._complain("The script is empty.")
        if not inline and not script_file:
            return self._complain("Name the script file, or switch to an inline script.")

        try:
            set_vars = self._collect_vars()
        except ValueError as e:
            return self._complain(str(e))

        self._result = TestCase(
            name=name,
            script=script,
            script_file=script_file,
            timeout=self._timeout.value(),
            set_vars=set_vars,
            expect_success=self._expect_success.isChecked(),
            assert_echoes=_lines(self._echoes.toPlainText()),
            assert_no_echoes=self._no_echoes.isChecked(),
            assert_warnings=_lines(self._warnings.toPlainText()),
            assert_no_warnings=self._no_warnings.isChecked(),
        )
        self.accept()

    def _add_var_row(self, name: str, value: str):
        row = self._vars.rowCount()
        self._vars.insertRow(row)
        self._vars.setItem(row, 0, QTableWidgetItem(name))
        self._vars.setItem(row, 1, QTableWidgetItem(value))
        if not name:
            self._vars.setCurrentCell(row, 0)
            self._vars.editItem(self._vars.item(row, 0))

    def _remove_var_row(self):
        row = self._vars.currentRow()
        if row >= 0:
            self._vars.removeRow(row)

    def _collect_vars(self) -> dict:
        """The table as a dict, values parsed as TOML so they mean what they
        would mean in the file. Blank rows are ignored -- clicking + and
        changing your mind should not be an error."""
        lines = []
        for row in range(self._vars.rowCount()):
            name = (self._vars.item(row, 0).text() if self._vars.item(row, 0) else "").strip()
            value = (self._vars.item(row, 1).text() if self._vars.item(row, 1) else "").strip()
            if not name and not value:
                continue
            if not name:
                raise ValueError(f"Row {row + 1} has a value but no variable name.")
            if not value:
                raise ValueError(f"{name} has no value.")
            lines.append(f"{name} = {value}")
        return _parse_set_vars("\n".join(lines))

    def _complain(self, message: str):
        QMessageBox.warning(self, "Test", message)


def _lines(text: str) -> list:
    return [ln for ln in (l.strip() for l in text.splitlines()) if ln]


def _parse_set_vars(text: str) -> dict:
    """`name = <toml value>` per line. Parsed as TOML so the values mean what
    they would mean in the file itself."""
    body = "\n".join(_lines(text))
    if not body:
        return {}
    try:
        return dict(tomllib.loads(body))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Variable overrides are not valid TOML: {e}") from e
