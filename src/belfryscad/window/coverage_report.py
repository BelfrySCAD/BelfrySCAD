"""Coverage in the GUI: the report window and the run-tests worker.

The overlay itself lives in CodeEditor (set_coverage/clear_coverage) and is
driven by MainWindow._apply_coverage_overlays; this module holds the two
things that are neither editor nor main window: a modeless text window for
the per-file report, and a QObject that runs .scadtest files with coverage
on a worker thread and hands back the merged CoverageReport.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
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


class TestCoverageWorker(QObject):
    """Runs .scadtest files with coverage on, one evaluator per test, and
    merges the results. Lives on a QThread; every signal is queued back."""
    logged = Signal(str)
    finished = Signal(object)   # the merged CoverageReport (temp snippets dropped), or None on error
    done = Signal()

    def __init__(self, files: list[str]):
        super().__init__()
        self._files = list(files)

    def run(self):
        from belfryscad.docsgen.runner import _TEMP_PREFIX
        from belfryscad.scadtest import parse_scadtest_file, run_test
        merged = CoverageReport()
        passed = failed = 0
        try:
            for path in self._files:
                try:
                    tests = parse_scadtest_file(path)
                except Exception as e:  # noqa: BLE001 -- report, keep going with the next file
                    self.logged.emit(f"Error parsing {path}: {e}")
                    continue
                self.logged.emit(os.path.basename(path))
                for tc in tests:
                    result = run_test(tc, coverage=True)
                    if result.coverage:
                        merged.merge_spans(result.coverage["spans"])
                    if result.passed:
                        passed += 1
                    else:
                        failed += 1
                        self.logged.emit(f"  {tc.name} FAILED")
                        for msg in result.messages:
                            self.logged.emit(f"    {msg}")
            merged.drop_origins(lambda o: os.path.basename(o).startswith(_TEMP_PREFIX))
            self.logged.emit(f"{passed} of {passed + failed} tests passed, {failed} failed.")
            self.finished.emit(merged)
        except Exception as e:  # noqa: BLE001
            self.logged.emit(f"Run Tests with Coverage failed: {e}")
            self.finished.emit(None)
        finally:
            self.done.emit()
