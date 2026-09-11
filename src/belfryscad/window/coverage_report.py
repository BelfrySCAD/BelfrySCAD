"""The coverage report window.

The overlay lives in CodeEditor (set_coverage/clear_coverage), driven by
MainWindow._apply_coverage_overlays; the test run that produces a report at
all lives in window/testing.py. This is just the modeless text window that
shows the per-file numbers and every uncovered span.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

from belfryscad.coverage import CoverageReport, format_report


class CoverageReportDialog(QDialog):
    """Modeless (a modal dialog would swallow the viewport's shortcuts) and
    self-deleting; the main window keeps no reference."""

    def __init__(self, report: CoverageReport, base: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Coverage Report")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        text.setPlainText(format_report(report, base=base, uncovered=True, worst_first=True))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
