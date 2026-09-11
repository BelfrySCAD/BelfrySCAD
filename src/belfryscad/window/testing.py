"""The Testing pane: run a directory of .scadtest files, with coverage.

Tests are a project-wide concept, not a per-file one, so this is driven by a
directory rather than the current tab -- Design > Run Tests… picks one and
this pane runs everything under it. It is an ordinary dock, not a dialog:
a run takes as long as it takes, and the user needs the editor, the console
and the coverage overlay while it happens.

Coverage is collected here or nowhere. A single render no longer collects it
(there is no "Render with Coverage"), because coverage of one run says very
little -- what it is actually good for is "what does my test suite miss".

The overlay itself lives in CodeEditor (set_coverage/clear_coverage), driven
by MainWindow._apply_coverage_overlays; this pane owns the checkbox that
turns it on and the CoverageReport that feeds it.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QMenu, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from belfryscad.coverage import CoverageReport


def _test_names(path: str) -> list[str]:
    """The test names in one .scadtest, or nothing if it will not parse --
    listing a broken file with no children beats refusing to list it."""
    from belfryscad.scadtest import parse_scadtest_file
    try:
        return [t.name for t in parse_scadtest_file(path)]
    except Exception:   # noqa: BLE001 -- a malformed suite is the run's problem
        return []


def find_test_files(directory: str) -> list[str]:
    """Every .scadtest under `directory`, recursively, in a stable order."""
    found = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        found.extend(os.path.join(root, f) for f in sorted(files) if f.endswith(".scadtest"))
    return found


class TestRunWorker(QObject):
    """Runs .scadtest files, optionally with coverage, one evaluator per test.

    Lives on a QThread; every signal is queued back to the GUI thread. Results
    are emitted per file as they finish, so a long run fills the table in
    rather than landing all at once.
    """
    logged = Signal(str)
    file_done = Signal(object)    # {"path", "tests": [(name, passed, messages)], "coverage": CoverageReport|None}
    finished = Signal(object)     # merged CoverageReport (temp snippets dropped), or None
    done = Signal()

    def __init__(self, files: list[str], coverage: bool = True):
        super().__init__()
        self._files = list(files)
        self._coverage = coverage
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        from belfryscad.docsgen.runner import _TEMP_PREFIX
        from belfryscad.scadtest import parse_scadtest_file, run_test
        merged = CoverageReport() if self._coverage else None
        try:
            for path in self._files:
                if self._cancel:
                    self.logged.emit("Cancelled.")
                    break
                try:
                    tests = parse_scadtest_file(path)
                except Exception as e:  # noqa: BLE001 -- report, keep going with the next file
                    self.logged.emit(f"Error parsing {path}: {e}")
                    self.file_done.emit({"path": path, "tests": [], "coverage": None,
                                         "error": str(e)})
                    continue
                # Per-file coverage as well as the merge: the table shows what
                # each test file covers on its own, which is the number that
                # tells a library author which suite is thin.
                per_file = CoverageReport() if self._coverage else None
                rows = []
                for tc in tests:
                    if self._cancel:
                        break
                    result = run_test(tc, coverage=self._coverage)
                    if per_file is not None and result.coverage:
                        per_file.merge_spans(result.coverage["spans"])
                    rows.append((tc.name, result.passed, list(result.messages)))
                    if not result.passed:
                        self.logged.emit(f"  {tc.name} FAILED")
                        for msg in result.messages:
                            self.logged.emit(f"    {msg}")
                if per_file is not None:
                    per_file.drop_origins(lambda o: os.path.basename(o).startswith(_TEMP_PREFIX))
                    per_file.drop_nocov()   # /* nocov */ in the source
                    merged.merge(per_file)
                self.file_done.emit({"path": path, "tests": rows, "coverage": per_file})
            self.finished.emit(merged)
        except Exception as e:  # noqa: BLE001
            self.logged.emit(f"Run Tests failed: {e}")
            self.finished.emit(None)
        finally:
            self.done.emit()


class TestingPane(QWidget):
    """Directory picker + run button + results tree. Owns no threads; the
    main window runs the worker and feeds results back in."""

    run_requested = Signal()
    pick_dir_requested = Signal()
    overlay_toggled = Signal(bool)
    report_requested = Signal()
    edit_requested = Signal(str, str)      # (.scadtest path, test name) -- edit an existing one
    add_requested = Signal(str, object, str)   # (path, anchor test or None, "end"/"before"/"after")
    delete_requested = Signal(str, str)    # (.scadtest path, test name)
    run_file_requested = Signal(str)       # run just this .scadtest
    new_file_requested = Signal()

    _COLUMNS = ("Tests", "Passed", "Passed %", "Coverage %")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._directory = None
        self._dir_count = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        self._dir_label = QLabel("No test directory chosen")
        self._dir_label.setMinimumWidth(80)
        top.addWidget(self._dir_label, 1)
        self._choose_btn = QPushButton("Choose…")
        self._choose_btn.clicked.connect(self.pick_dir_requested)
        top.addWidget(self._choose_btn)
        layout.addLayout(top)

        controls = QHBoxLayout()
        self._coverage_box = QCheckBox("Collect coverage")
        self._coverage_box.setChecked(True)
        self._coverage_box.setToolTip("Coverage is only collected when tests run.")
        controls.addWidget(self._coverage_box)
        self._overlay_box = QCheckBox("Highlight coverage in editors")
        self._overlay_box.setChecked(True)
        self._overlay_box.setToolTip(
            "Tint covered and uncovered code in every open tab of a file the tests touched.")
        self._overlay_box.toggled.connect(self.overlay_toggled)
        controls.addWidget(self._overlay_box)
        controls.addStretch(1)
        layout.addLayout(controls)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(self._COLUMNS))
        self._tree.setHeaderLabels(list(self._COLUMNS))
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(self._COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._tree, 1)

        # One button row under the table: New File on the left, the totals
        # beside it, and the two that act on the whole run pushed right.
        bottom = QHBoxLayout()
        self._new_file_btn = QPushButton("New File…")
        self._new_file_btn.setToolTip("Create a new .scadtest file in this directory")
        self._new_file_btn.clicked.connect(self.new_file_requested)
        self._new_file_btn.setEnabled(False)
        bottom.addWidget(self._new_file_btn)
        self._total_label = QLabel("")
        # The label takes the slack, which is what keeps the two buttons hard
        # right; a bare addStretch would leave it at its text width and clip.
        self._total_label.setMinimumWidth(0)
        bottom.addWidget(self._total_label, 1)
        self._report_btn = QPushButton("Report…")
        self._report_btn.setToolTip("The per-file report, with every uncovered span")
        self._report_btn.clicked.connect(self.report_requested)
        self._report_btn.setEnabled(False)
        bottom.addWidget(self._report_btn)
        self._run_btn = QPushButton("Run Tests")
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self.run_requested)
        self._run_btn.setEnabled(False)
        bottom.addWidget(self._run_btn)
        layout.addLayout(bottom)

        self._totals = [0, 0]   # passed, total

    # -- state ---------------------------------------------------------

    def directory(self) -> str | None:
        return self._directory

    def set_directory(self, path: str, files):
        """Show `path` and list its suites straight away, before any run --
        Add Test needs a file to aim at, and an empty tree gives it none."""
        files = list(files)
        self._directory = path
        self._dir_count = len(files)
        self._relabel_dir()
        self._run_btn.setEnabled(bool(files))
        self._new_file_btn.setEnabled(True)
        self.clear_results()
        for file_path in files:
            item = QTreeWidgetItem([os.path.relpath(file_path, path), "", "", ""])
            item.setData(0, Qt.ItemDataRole.UserRole, file_path)
            # The tests themselves too, not just the file: the tree is a view
            # of the suite, and a run fills in the results. Without this,
            # editing or deleting a test would need a run first, and every
            # edit resets the tree.
            for name in _test_names(file_path):
                item.addChild(QTreeWidgetItem([name, "", "", ""]))
            self._tree.addTopLevelItem(item)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relabel_dir()

    def _relabel_dir(self):
        """Elide the path rather than let it set the dock's minimum width --
        a real tests directory is a long absolute path, and a QLabel holding
        one stops the dock being narrowed at all. Full path in the tooltip."""
        if not self._directory:
            return
        plural = "s" if self._dir_count != 1 else ""
        text = f"{self._directory}  ({self._dir_count} test file{plural})"
        self._dir_label.setToolTip(text)
        fm = self._dir_label.fontMetrics()
        width = max(self._dir_label.width(), 80)
        self._dir_label.setText(fm.elidedText(text, Qt.TextElideMode.ElideMiddle, width))

    def coverage_enabled(self) -> bool:
        return self._coverage_box.isChecked()

    def overlay_enabled(self) -> bool:
        return self._overlay_box.isChecked()

    def set_running(self, running: bool):
        self._run_btn.setText("Cancel" if running else "Run Tests")
        self._choose_btn.setEnabled(not running)
        self._coverage_box.setEnabled(not running)

    def is_running(self) -> bool:
        return self._run_btn.text() == "Cancel"

    # -- results -------------------------------------------------------

    def clear_results(self):
        self._tree.clear()
        self._total_label.setText("")
        self._totals = [0, 0]
        self._report_btn.setEnabled(False)

    def _on_item_double_clicked(self, item, _column):
        """Double-clicking a TEST opens it for editing. Double-clicking a
        file row does nothing -- the file is TOML scaffolding, and the thing
        worth editing is a test inside it."""
        parent = item.parent()
        if parent is None:
            return
        path = parent.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.edit_requested.emit(path, item.text(0))

    def selected_test(self):
        """(path, name) of the selected TEST row, or None -- a file row is
        not a test, and Delete only ever removes one test."""
        item = self._tree.currentItem()
        if item is None or item.parent() is None:
            return None
        path = item.parent().data(0, Qt.ItemDataRole.UserRole)
        return (path, item.text(0)) if path else None

    def _show_context_menu(self, point):
        menu = self.context_menu_for(self._tree.itemAt(point))
        if menu is not None:
            menu.exec(self._tree.viewport().mapToGlobal(point))

    def context_menu_for(self, item):
        """The menu for a row, or None for empty space. Built separately from
        showing it so it can be inspected without exec() -- a QMenu's exec is
        a C++ method and cannot be monkeypatched, so a test that shows one
        just hangs."""
        if item is None:
            return None
        self._tree.setCurrentItem(item)
        parent = item.parent()
        menu = QMenu(self._tree)
        if parent is None:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if not path:
                return None
            menu.addAction("Run Tests in this File").triggered.connect(
                lambda _=False: self.run_file_requested.emit(path))
            menu.addSeparator()
            menu.addAction("Add Test").triggered.connect(
                lambda _=False: self.add_requested.emit(path, None, "end"))
        else:
            path = parent.data(0, Qt.ItemDataRole.UserRole)
            if not path:
                return None
            name = item.text(0)
            menu.addAction("Add Test Before").triggered.connect(
                lambda _=False: self.add_requested.emit(path, name, "before"))
            menu.addAction("Add Test After").triggered.connect(
                lambda _=False: self.add_requested.emit(path, name, "after"))
            menu.addSeparator()
            menu.addAction("Delete Test").triggered.connect(
                lambda _=False: self.delete_requested.emit(path, name))
        return menu

    def selected_file(self):
        """The .scadtest of whichever row is selected, or the only one in the
        tree when nothing is selected."""
        item = self._tree.currentItem()
        if item is not None:
            top = item.parent() or item
            return top.data(0, Qt.ItemDataRole.UserRole)
        if self._tree.topLevelItemCount() == 1:
            return self._tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)
        return None

    def add_file_result(self, result: dict, base: str | None = None):
        path = result["path"]
        rows = result["tests"]
        passed = sum(1 for _, ok, _ in rows if ok)
        total = len(rows)
        name = os.path.relpath(path, base) if base else os.path.basename(path)
        # Update the row in place rather than swapping a fresh item in.
        # takeTopLevelItem destroys the old item and its children, and if the
        # view's current item is anywhere in that subtree the pointer is left
        # dangling -- a later setCurrentItem then segfaults. Reproduced.
        item = None
        for i in range(self._tree.topLevelItemCount()):
            if self._tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == path:
                item = self._tree.topLevelItem(i)
                item.takeChildren()
                break
        if item is None:
            item = QTreeWidgetItem()
            self._tree.addTopLevelItem(item)
        for column, value in enumerate((name, f"{passed}/{total}", _pct(passed, total), "")):
            item.setText(column, value)
        item.setData(0, Qt.ItemDataRole.UserRole, path)
        if result.get("coverage") is not None:
            item.setText(3, f"{result['coverage'].total().percent:.1f}%")
        if result.get("error"):
            item.setText(1, "error")
            item.setToolTip(0, result["error"])
        for test_name, ok, messages in rows:
            child = QTreeWidgetItem([test_name, "pass" if ok else "FAIL", "", ""])
            if not ok:
                child.setToolTip(0, "\n".join(messages) or "failed")
            item.addChild(child)
        # Open a file that has something to look at, and only that one.
        # setExpanded is ignored on an item that is not in a tree yet, so
        # this has to come after addTopLevelItem.
        if passed < total:
            item.setExpanded(True)
        self._totals[0] += passed
        self._totals[1] += total

    def set_totals(self, coverage_percent: float | None):
        passed, total = self._totals
        parts = [f"{passed} of {total} tests passed ({_pct(passed, total)})"]
        if coverage_percent is not None:
            parts.append(f"coverage {coverage_percent:.1f}%")
        self._total_label.setText("   ".join(parts))
        self._report_btn.setEnabled(coverage_percent is not None)


def _pct(hit: int, total: int) -> str:
    return "100.0%" if total == 0 else f"{100.0 * hit / total:.1f}%"
