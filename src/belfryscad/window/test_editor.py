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
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from belfryscad.scadtest import TestCase


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
        layout.addLayout(form)

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
        layout.addWidget(self._stack, 1)

        vars_box = QGroupBox("Variable overrides (TOML, one per line)")
        vars_layout = QVBoxLayout(vars_box)
        self._set_vars = _mono(QPlainTextEdit(_format_set_vars(test.set_vars if test else {})))
        self._set_vars.setPlaceholderText('size = 10\nlabel = "cap"\nflags = [1, 2]')
        self._set_vars.setMaximumHeight(90)
        vars_layout.addWidget(self._set_vars)
        layout.addWidget(vars_box)

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
        ex.addRow(self._no_echoes)
        self._warnings = _mono(QPlainTextEdit("\n".join(test.assert_warnings) if test else ""))
        self._warnings.setPlaceholderText("one expected WARNING per line")
        self._warnings.setMaximumHeight(70)
        ex.addRow("Warnings", self._warnings)
        self._no_warnings = QCheckBox("…and no others")
        self._no_warnings.setChecked(test.assert_no_warnings if test else True)
        ex.addRow(self._no_warnings)
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
            set_vars = _parse_set_vars(self._set_vars.toPlainText())
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

    def _complain(self, message: str):
        QMessageBox.warning(self, "Test", message)


def _lines(text: str) -> list:
    return [ln for ln in (l.strip() for l in text.splitlines()) if ln]


def _format_set_vars(set_vars: dict) -> str:
    from belfryscad.scadtest import _toml_literal
    return "\n".join(f"{k} = {_toml_literal(v)}" for k, v in (set_vars or {}).items())


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
