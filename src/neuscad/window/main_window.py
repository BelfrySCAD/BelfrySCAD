from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QSplitter,
    QTabWidget, QPlainTextEdit, QToolBar, QStatusBar,
    QLabel, QMessageBox, QFileDialog, QToolButton, QButtonGroup,
)
from PySide6.QtGui import QAction, QKeySequence, QFont, QIcon, QUndoCommand
from PySide6.QtCore import Qt, QSize, QSettings
import time

from neuscad.window.editor import CodeEditor
from neuscad.window.viewport import Viewport
from neuscad.window.debugger import DebuggerPane, DebugSession

import re
from pathlib import Path

_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"
_TOOL_ICONS = {
    0: "tool-translate.svg",
    1: "tool-rotate.svg",
    2: "tool-scale.svg",
}


class _TextEditCmd(QUndoCommand):
    """Undo command for raw text edits in the code editor."""
    _MERGE_WINDOW = 3.0   # seconds: edits this close are merged into one undo step

    def __init__(self, tab, editor, before, cursor_before, after, cursor_after):
        super().__init__("Edit")
        self._tab = tab
        self._editor = editor
        self._before = before
        self._cursor_before = cursor_before
        self._after = after
        self._cursor_after = cursor_after
        self._t = time.monotonic()
        self._first_redo = True   # push() calls redo() immediately; skip it

    def id(self):
        return 2000

    def mergeWith(self, other):
        if (not isinstance(other, _TextEditCmd)
                or other._tab is not self._tab
                or other._t - self._t > self._MERGE_WINDOW):
            return False
        self._after = other._after
        self._cursor_after = other._cursor_after
        self._t = other._t
        return True

    def _set_cursor(self, pos):
        cursor = self._editor.textCursor()
        cursor.setPosition(min(pos, len(self._editor.toPlainText())))
        self._editor.setTextCursor(cursor)

    def undo(self):
        self._tab._suppress_text_undo = True
        self._editor.setPlainText(self._before)
        self._tab._suppress_text_undo = False
        self._tab._last_text = self._before
        self._tab._last_cursor = self._cursor_before
        self._set_cursor(self._cursor_before)

    def redo(self):
        if self._first_redo:
            self._first_redo = False
            return   # text is already correct; user just typed it
        self._tab._suppress_text_undo = True
        self._editor.setPlainText(self._after)
        self._tab._suppress_text_undo = False
        self._tab._last_text = self._after
        self._tab._last_cursor = self._cursor_after
        self._set_cursor(self._cursor_after)


class _GizmoCmd(QUndoCommand):
    def __init__(self, tab, editor, before, after, render_fn, new_node_start, restore_fn, merge_id, label):
        super().__init__(label)
        self._tab = tab
        self._editor = editor
        self._before = before
        self._after = after
        self._render = render_fn
        self._new_node_start = new_node_start
        self._restore = restore_fn
        self._merge_id = merge_id

    def id(self):
        return self._merge_id

    def mergeWith(self, other):
        if (not isinstance(other, _GizmoCmd)
                or other._tab is not self._tab
                or other._merge_id != self._merge_id):
            return False
        self._after = other._after
        self._render = other._render
        self._new_node_start = other._new_node_start
        return True

    def undo(self):
        self._tab._suppress_text_undo = True
        self._editor.setPlainText(self._before)
        self._tab._suppress_text_undo = False
        self._tab._last_text = self._before
        self._render()
        self._tab.viewport._renderer.selected_id = None
        self._tab.editor.clear_selection()
        self._tab.viewport.update()

    def redo(self):
        self._tab._suppress_text_undo = True
        self._editor.setPlainText(self._after)
        self._tab._suppress_text_undo = False
        self._tab._last_text = self._after
        self._render()
        self._restore(self._tab, self._new_node_start)


class DocumentTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.editor = CodeEditor()
        self.viewport = Viewport()
        self.tools_strip = self._make_tools_strip()

        right_splitter = QSplitter(Qt.Orientation.Horizontal)
        right_splitter.addWidget(self.viewport)
        right_splitter.addWidget(self.tools_strip)
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 0)
        right_splitter.setSizes([800, 48])

        splitter.addWidget(self.editor)
        splitter.addWidget(right_splitter)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([400, 800])

        layout.addWidget(splitter)

        self.file_path = None
        self.is_modified = False

    def _make_tools_strip(self):
        strip = QWidget()
        strip.setFixedWidth(48)
        strip.setObjectName("ToolsStrip")

        layout = QVBoxLayout(strip)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(4)

        self._tool_group = QButtonGroup(strip)
        self._tool_group.setExclusive(True)

        for tool_id, label, tooltip in (
            (0, "T", "Translate"),
            (1, "R", "Rotate"),
            (2, "S", "Scale"),
        ):
            btn = QToolButton()
            btn.setToolTip(tooltip)
            btn.setCheckable(True)
            btn.setFixedSize(36, 36)
            icon_path = _ICONS_DIR / _TOOL_ICONS[tool_id]
            if icon_path.exists():
                btn.setIcon(QIcon(str(icon_path)))
                btn.setIconSize(QSize(26, 26))
            else:
                btn.setText(label)
                btn.setFont(QFont("Helvetica", 13, QFont.Weight.Bold))
            self._tool_group.addButton(btn, tool_id)
            layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch()
        self.active_tool: int | None = None
        self._tool_group.idToggled.connect(self._on_tool_toggled)

        return strip

    def _on_tool_toggled(self, tool_id: int, checked: bool):
        self.active_tool = tool_id if checked else None
        self.viewport.set_active_tool(tool_id if checked else -1)

    def display_name(self):
        if self.file_path:
            import os
            name = os.path.basename(self.file_path)
        else:
            name = "Untitled"
        return name + ("*" if self.is_modified else "")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NeuSCAD")
        self.resize(1400, 900)

        self._undo_stack = self._create_undo_stack()
        self._setup_ui()
        self._setup_menus()
        self._setup_shortcuts()
        self._new_document()

    def _create_undo_stack(self):
        from PySide6.QtGui import QUndoStack
        return QUndoStack(self)

    # ------------------------------------------------------------------
    # UI assembly
    # ------------------------------------------------------------------

    def _setup_ui(self):
        self._toolbar = self._make_toolbar()
        self.addToolBar(self._toolbar)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._tab_changed)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setFont(QFont("Menlo", 11))
        self._console.setObjectName("Console")

        self._debugger_pane = DebuggerPane()
        self._debugger_pane.hide()
        self._debug_session: DebugSession | None = None

        self._debugger_pane.continue_requested.connect(self._on_debug_continue)
        self._debugger_pane.step_into_requested.connect(self._on_debug_step_into)
        self._debugger_pane.step_over_requested.connect(self._on_debug_step_over)
        self._debugger_pane.step_out_requested.connect(self._on_debug_step_out)
        self._debugger_pane.stop_requested.connect(self._on_debug_stop)

        self._bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._bottom_splitter.addWidget(self._console)
        self._bottom_splitter.addWidget(self._debugger_pane)
        self._bottom_splitter.setStretchFactor(0, 1)
        self._bottom_splitter.setStretchFactor(1, 0)

        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.addWidget(self._tabs)
        self._main_splitter.addWidget(self._bottom_splitter)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        self._main_splitter.setSizes([600, 150])
        self._main_splitter.setCollapsible(0, False)
        self._main_splitter.setCollapsible(1, True)

        layout.addWidget(self._main_splitter)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._coord_label = QLabel("")
        self._status_bar.addWidget(self._coord_label)

    @staticmethod
    def _toolbar_icon(name: str) -> QIcon:
        path = _ICONS_DIR / f"toolbar-{name}.svg"
        return QIcon(str(path)) if path.exists() else QIcon()

    def _make_toolbar(self):
        tb = QToolBar("Main")
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        self._act_open = QAction(self._toolbar_icon("open"), "Open", self)
        self._act_open.setToolTip("Open (Ctrl+O)")
        self._act_open.triggered.connect(self._open_file)
        tb.addAction(self._act_open)

        self._act_export = QAction(self._toolbar_icon("export"), "Export", self)
        self._act_export.setToolTip("Export…")
        self._act_export.triggered.connect(self._export)
        tb.addAction(self._act_export)

        self._act_render = QAction(self._toolbar_icon("render"), "Render", self)
        self._act_render.setToolTip("Render (F6)")
        self._act_render.triggered.connect(self._render)
        tb.addAction(self._act_render)

        self._act_debug_run = QAction(self._toolbar_icon("debug"), "Debug", self)
        self._act_debug_run.setToolTip("Debug (F5)")
        self._act_debug_run.triggered.connect(self._start_debug)
        tb.addAction(self._act_debug_run)

        tb.addSeparator()

        self._act_undo = self._undo_stack.createUndoAction(self, "Undo")
        self._act_undo.setIcon(self._toolbar_icon("undo"))
        self._act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self._act_undo.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        tb.addAction(self._act_undo)

        self._act_redo = self._undo_stack.createRedoAction(self, "Redo")
        self._act_redo.setIcon(self._toolbar_icon("redo"))
        self._act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self._act_redo.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        tb.addAction(self._act_redo)

        return tb

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def _setup_menus(self):
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("File")
        self._add_action(file_menu, "New", self._new_document, QKeySequence.StandardKey.New)
        self._add_action(file_menu, "Open…", self._open_file, QKeySequence.StandardKey.Open)
        self._recent_menu = file_menu.addMenu("Open Recent")
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        self._add_action(file_menu, "Close", self._close_current_tab, QKeySequence.StandardKey.Close)
        self._add_action(file_menu, "Save", self._save_file, QKeySequence.StandardKey.Save)
        self._add_action(file_menu, "Save As…", self._save_file_as, QKeySequence.StandardKey.SaveAs)
        file_menu.addSeparator()
        self._add_action(file_menu, "Export…", self._export)
        file_menu.addSeparator()
        self._add_action(file_menu, "Quit", self.close, QKeySequence.StandardKey.Quit)

        # Edit
        edit_menu = mb.addMenu("Edit")
        edit_menu.addAction(self._act_undo)
        edit_menu.addAction(self._act_redo)
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Cut", self._edit_cut, QKeySequence.StandardKey.Cut)
        self._add_action(edit_menu, "Copy", self._edit_copy, QKeySequence.StandardKey.Copy)
        self._add_action(edit_menu, "Paste", self._edit_paste, QKeySequence.StandardKey.Paste)
        self._add_action(edit_menu, "Select All", self._edit_select_all, QKeySequence.StandardKey.SelectAll)
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Expand Selection", self._selection_expand)
        self._add_action(edit_menu, "Contract Selection", self._selection_contract)
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Indent", self._indent, QKeySequence("Tab"))
        self._add_action(edit_menu, "Undent", self._undent, QKeySequence("Shift+Tab"))
        self._add_action(edit_menu, "Comment", self._comment, QKeySequence("Ctrl+/"))
        self._add_action(edit_menu, "Uncomment", self._uncomment, QKeySequence("Ctrl+Shift+/"))
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Find…", self._find, QKeySequence.StandardKey.Find)
        self._add_action(edit_menu, "Find & Replace…", self._find_replace, QKeySequence.StandardKey.Replace)

        # Design
        design_menu = mb.addMenu("Design")
        self._act_render_menu = self._add_action(design_menu, "Render", self._render, QKeySequence("F6"))
        design_menu.addSeparator()
        insert_menu = design_menu.addMenu("Insert Primitive")
        for prim in ("Cube", "Sphere", "Cylinder", "Cone"):
            insert_menu.addAction(prim)
        bool_menu = design_menu.addMenu("Boolean Operation")
        for op in ("Union", "Difference", "Intersection"):
            bool_menu.addAction(op)

        # View
        view_menu = mb.addMenu("View")
        self._act_show_toolbar = self._add_checkable(view_menu, "Show Toolbar", True, self._toolbar.setVisible)
        self._act_show_tabs = self._add_checkable(view_menu, "Show Tab Bar", True, self._tabs.tabBar().setVisible)
        self._act_show_editor = self._add_checkable(view_menu, "Show Code Editor", True, self._toggle_editor)
        self._act_show_tools = self._add_checkable(view_menu, "Show Tools Strip", True, self._toggle_tools_strip)
        self._act_show_console = self._add_checkable(view_menu, "Show Console", True, self._toggle_console)
        self._act_show_debugger = self._add_checkable(view_menu, "Show Debugger", False, self._toggle_debugger)
        self._console_height = 150
        view_menu.addSeparator()
        for label, preset, key in (
            ("Top",       "top",    "Ctrl+4"),
            ("Bottom",    "bottom", "Ctrl+5"),
            ("Left",      "left",   "Ctrl+6"),
            ("Right",     "right",  "Ctrl+7"),
            ("Front",     "front",  "Ctrl+8"),
            ("Back",      "back",   "Ctrl+9"),
            ("Isometric", "iso",    "Ctrl+0"),
            ("View All",  "all",    "Shift+Ctrl+V"),
        ):
            self._add_action(view_menu, label,
                             lambda p=preset: self._set_view(p),
                             QKeySequence(key))
        view_menu.addSeparator()
        self._act_show_axes = self._add_checkable(view_menu, "Show Axes", True, self._toggle_axes)
        self._act_show_edges = self._add_checkable(view_menu, "Show Edges", False, self._toggle_edges)
        self._act_show_scale = self._add_checkable(view_menu, "Show Scale Markers", True, self._toggle_scale_markers)
        self._act_show_cross = self._add_checkable(view_menu, "Show Crosshairs", False, self._toggle_crosshairs)
        self._act_show_status = self._add_checkable(view_menu, "Show Status Bar", True, self._status_bar.setVisible)

        # Window
        window_menu = mb.addMenu("Window")
        self._add_action(window_menu, "Minimize", self.showMinimized, QKeySequence("Ctrl+M"))
        self._add_action(window_menu, "Zoom", self.showMaximized)
        window_menu.addSeparator()
        self._add_action(window_menu, "Move Tab to New Window", self._tear_off_tab)
        window_menu.addSeparator()
        self._add_action(window_menu, "Bring All to Front", self._bring_all_to_front)

    def _add_action(self, menu, label, slot=None, shortcut=None):
        act = QAction(label, self)
        if shortcut:
            act.setShortcut(shortcut)
        if slot:
            act.triggered.connect(lambda checked=False, s=slot: s())
        menu.addAction(act)
        return act

    def _add_checkable(self, menu, label, checked, slot):
        act = QAction(label, self)
        act.setCheckable(True)
        act.setChecked(checked)
        if slot:
            act.toggled.connect(slot)
        menu.addAction(act)
        return act

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _setup_shortcuts(self):
        from PySide6.QtGui import QShortcut

        def shortcut(key, slot):
            s = QShortcut(QKeySequence(key), self)
            s.activated.connect(slot)

        shortcut("Ctrl+1", lambda: self._act_show_edges.toggle())
        shortcut("Ctrl+2", lambda: self._act_show_axes.toggle())
        shortcut("Ctrl+3", lambda: self._act_show_cross.toggle())
        shortcut("Ctrl++", self._font_size_increase)
        shortcut("Ctrl+-", self._font_size_decrease)
        shortcut("Ctrl+[", lambda: self._zoom_viewport(-1))
        shortcut("Ctrl+]", lambda: self._zoom_viewport(1))
        shortcut("F5", self._start_debug)
        shortcut("F10", self._on_debug_step_over)
        shortcut("F11", self._on_debug_step_into)
        shortcut("F12", self._on_debug_step_out)

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def _new_document(self):
        tab = DocumentTab()
        tab.id_to_node = {}
        tab._last_text = ""
        tab._last_cursor = 0
        tab._suppress_text_undo = False
        tab.editor.document().contentsChanged.connect(
            lambda t=tab: self._on_editor_changed(t)
        )
        tab.viewport.selection_changed.connect(
            lambda orig_id, t=tab: self._on_selection_changed(t, orig_id)
        )
        tab.viewport.translate_committed.connect(
            lambda dx, dy, dz, t=tab: self._on_translate_committed(t, dx, dy, dz)
        )
        tab.viewport.rotate_committed.connect(
            lambda axis, deg, t=tab: self._on_rotate_committed(t, axis, deg)
        )
        tab.viewport.scale_committed.connect(
            lambda axis, factor, uniform, t=tab: self._on_scale_committed(t, axis, factor, uniform)
        )
        idx = self._tabs.addTab(tab, tab.display_name())
        self._tabs.setCurrentIndex(idx)

    def _current_tab(self):
        return self._tabs.currentWidget()

    def _tab_changed(self, index):
        pass

    def _close_tab(self, index):
        tab = self._tabs.widget(index)
        if tab and tab.is_modified:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                f"Save changes to {tab.display_name()}?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            if reply == QMessageBox.StandardButton.Save:
                self._tabs.setCurrentIndex(index)
                if not self._save_file():
                    return
        self._tabs.removeTab(index)
        if self._tabs.count() == 0:
            self._new_document()

    def _close_current_tab(self):
        self._close_tab(self._tabs.currentIndex())

    def _tear_off_tab(self):
        pass  # TODO: detach tab into separate window

    def _on_editor_changed(self, tab):
        tab.is_modified = True
        idx = self._tabs.indexOf(tab)
        if idx >= 0:
            self._tabs.setTabText(idx, tab.display_name())
        if getattr(tab, '_suppress_text_undo', False):
            return
        current = tab.editor.toPlainText()
        cursor_after = tab.editor.textCursor().position()
        before = getattr(tab, '_last_text', current)
        cursor_before = getattr(tab, '_last_cursor', 0)
        tab._last_text = current
        tab._last_cursor = cursor_after
        if current != before:
            self._undo_stack.push(
                _TextEditCmd(tab, tab.editor, before, cursor_before, current, cursor_after)
            )

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open File", "", "OpenSCAD Files (*.scad);;All Files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Open Error", str(e))
            return
        tab = DocumentTab()
        tab.id_to_node = {}
        tab.file_path = path
        tab._last_text = text
        tab._last_cursor = 0
        tab._suppress_text_undo = False
        tab.editor.setPlainText(text)
        tab.is_modified = False
        tab.editor.document().contentsChanged.connect(
            lambda t=tab: self._on_editor_changed(t)
        )
        tab.viewport.selection_changed.connect(
            lambda orig_id, t=tab: self._on_selection_changed(t, orig_id)
        )
        tab.viewport.translate_committed.connect(
            lambda dx, dy, dz, t=tab: self._on_translate_committed(t, dx, dy, dz)
        )
        tab.viewport.rotate_committed.connect(
            lambda axis, deg, t=tab: self._on_rotate_committed(t, axis, deg)
        )
        tab.viewport.scale_committed.connect(
            lambda axis, factor, uniform, t=tab: self._on_scale_committed(t, axis, factor, uniform)
        )
        idx = self._tabs.addTab(tab, tab.display_name())
        self._tabs.setCurrentIndex(idx)
        self._update_recent_files(path)
        self._render()

    def _save_file(self):
        tab = self._current_tab()
        if not tab:
            return False
        if not tab.file_path:
            return self._save_file_as()
        return self._write_file(tab, tab.file_path)

    def _save_file_as(self):
        tab = self._current_tab()
        if not tab:
            return False
        path, _ = QFileDialog.getSaveFileName(
            self, "Save File", "", "OpenSCAD Files (*.scad);;All Files (*)"
        )
        if not path:
            return False
        return self._write_file(tab, path)

    def _write_file(self, tab, path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(tab.editor.toPlainText())
        except OSError as e:
            QMessageBox.critical(self, "Save Error", str(e))
            return False
        tab.file_path = path
        tab.is_modified = False
        idx = self._tabs.indexOf(tab)
        if idx >= 0:
            self._tabs.setTabText(idx, tab.display_name())
        self._update_recent_files(path)
        self._render()
        return True

    # ------------------------------------------------------------------
    # Recent files
    # ------------------------------------------------------------------

    _MAX_RECENT = 10

    def _update_recent_files(self, path: str):
        settings = QSettings("NeuSCAD", "NeuSCAD")
        recents = settings.value("recentFiles", [], type=list)
        path = str(Path(path).resolve())
        if path in recents:
            recents.remove(path)
        recents.insert(0, path)
        recents = recents[: self._MAX_RECENT]
        settings.setValue("recentFiles", recents)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        settings = QSettings("NeuSCAD", "NeuSCAD")
        recents = settings.value("recentFiles", [], type=list)
        if not recents:
            placeholder = QAction("(empty)", self)
            placeholder.setEnabled(False)
            self._recent_menu.addAction(placeholder)
            return
        for path in recents:
            act = QAction(Path(path).name, self)
            act.setToolTip(path)
            act.triggered.connect(lambda checked=False, p=path: self._open_recent(p))
            self._recent_menu.addAction(act)
        self._recent_menu.addSeparator()
        clear_act = QAction("Clear Menu", self)
        clear_act.triggered.connect(self._clear_recent_files)
        self._recent_menu.addAction(clear_act)

    def _open_recent(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Open Error", str(e))
            settings = QSettings("NeuSCAD", "NeuSCAD")
            recents = settings.value("recentFiles", [], type=list)
            if path in recents:
                recents.remove(path)
            settings.setValue("recentFiles", recents)
            self._rebuild_recent_menu()
            return
        tab = DocumentTab()
        tab.id_to_node = {}
        tab.file_path = path
        tab._last_text = text
        tab._last_cursor = 0
        tab._suppress_text_undo = False
        tab.editor.setPlainText(text)
        tab.is_modified = False
        tab.editor.document().contentsChanged.connect(
            lambda t=tab: self._on_editor_changed(t)
        )
        tab.viewport.selection_changed.connect(
            lambda orig_id, t=tab: self._on_selection_changed(t, orig_id)
        )
        tab.viewport.translate_committed.connect(
            lambda dx, dy, dz, t=tab: self._on_translate_committed(t, dx, dy, dz)
        )
        tab.viewport.rotate_committed.connect(
            lambda axis, deg, t=tab: self._on_rotate_committed(t, axis, deg)
        )
        tab.viewport.scale_committed.connect(
            lambda axis, factor, uniform, t=tab: self._on_scale_committed(t, axis, factor, uniform)
        )
        idx = self._tabs.addTab(tab, tab.display_name())
        self._tabs.setCurrentIndex(idx)
        self._update_recent_files(path)
        self._render()

    def _clear_recent_files(self):
        settings = QSettings("NeuSCAD", "NeuSCAD")
        settings.setValue("recentFiles", [])
        self._rebuild_recent_menu()

    def _export(self):
        tab = self._current_tab()
        if not tab:
            return
        self._render()
        bodies = getattr(tab, '_bodies', None)
        if not bodies:
            QMessageBox.warning(self, "Export", "No geometry to export. Render first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export", "",
            "STL Files (*.stl);;OBJ Files (*.obj);;3MF Files (*.3mf)"
        )
        if not path:
            return

        ext = Path(path).suffix.lower()
        try:
            if ext == ".3mf":
                self._write_3mf(path, bodies)
            else:
                import manifold3d as m3d
                all_manifolds = [b.body for b in bodies if not b.body.is_empty()]
                if not all_manifolds:
                    QMessageBox.warning(self, "Export", "No geometry to export.")
                    return
                mesh = m3d.Manifold.compose(all_manifolds).to_mesh()
                if ext == ".obj":
                    self._write_obj(path, mesh)
                else:
                    if not path.endswith(".stl"):
                        path += ".stl"
                    self._write_stl(path, mesh)
            self.log(f"Exported to {path}")
        except OSError as e:
            QMessageBox.critical(self, "Export Error", str(e))

    @staticmethod
    def _write_stl(path: str, mesh):
        import struct
        import numpy as np

        verts = np.asarray(mesh.vert_properties[:, :3], dtype=np.float32)
        tris = np.asarray(mesh.tri_verts, dtype=np.int32)

        v0 = verts[tris[:, 0]]
        v1 = verts[tris[:, 1]]
        v2 = verts[tris[:, 2]]

        normals = np.cross(v1 - v0, v2 - v0).astype(np.float32)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals /= np.where(lengths > 0, lengths, 1.0)

        dtype = np.dtype([
            ("normal", np.float32, (3,)),
            ("v0",     np.float32, (3,)),
            ("v1",     np.float32, (3,)),
            ("v2",     np.float32, (3,)),
            ("attr",   np.uint16),
        ])
        data = np.zeros(len(tris), dtype=dtype)
        data["normal"] = normals
        data["v0"] = v0
        data["v1"] = v1
        data["v2"] = v2

        with open(path, "wb") as f:
            f.write(b"\0" * 80)
            f.write(struct.pack("<I", len(tris)))
            f.write(data.tobytes())

    @staticmethod
    def _write_obj(path: str, mesh):
        import numpy as np

        verts = np.asarray(mesh.vert_properties[:, :3], dtype=np.float32)
        tris = np.asarray(mesh.tri_verts, dtype=np.int32)

        with open(path, "w", encoding="utf-8") as f:
            for v in verts:
                f.write(f"v {v[0]:.6g} {v[1]:.6g} {v[2]:.6g}\n")
            f.write("\n")
            for tri in tris:
                f.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")

    @staticmethod
    def _write_3mf(path: str, bodies):
        import lib3mf
        import numpy as np

        _FA3 = type(lib3mf.Position().Coordinates)
        _UI3 = type(lib3mf.Triangle().Indices)

        def _identity_transform():
            t = lib3mf.Transform()
            _col = type(t.Fields[0])
            t.Fields[0] = _col(1, 0, 0)
            t.Fields[1] = _col(0, 1, 0)
            t.Fields[2] = _col(0, 0, 1)
            t.Fields[3] = _col(0, 0, 0)
            return t

        wrapper = lib3mf.Wrapper()
        model = wrapper.CreateModel()

        for colored_body in bodies:
            if colored_body.body.is_empty():
                continue

            mesh3d = colored_body.body.to_mesh()
            verts = np.asarray(mesh3d.vert_properties[:, :3], dtype=np.float32)
            tris  = np.asarray(mesh3d.tri_verts, dtype=np.int32)
            if len(tris) == 0:
                continue

            mesh_obj = model.AddMeshObject()

            positions = []
            for v in verts:
                p = lib3mf.Position()
                p.Coordinates = _FA3(float(v[0]), float(v[1]), float(v[2]))
                positions.append(p)

            triangles = []
            for t in tris:
                tri = lib3mf.Triangle()
                tri.Indices = _UI3(int(t[0]), int(t[1]), int(t[2]))
                triangles.append(tri)

            mesh_obj.SetGeometry(positions, triangles)

            rgba = colored_body.color or (0.8, 0.8, 0.8, 1.0)
            cg = model.AddColorGroup()
            c = lib3mf.Color()
            c.Red   = max(0, min(255, int(rgba[0] * 255)))
            c.Green = max(0, min(255, int(rgba[1] * 255)))
            c.Blue  = max(0, min(255, int(rgba[2] * 255)))
            c.Alpha = max(0, min(255, int(rgba[3] * 255)))
            color_id = cg.AddColor(c)
            cg_uid   = cg.GetUniqueResourceID()

            props = []
            for _ in range(len(tris)):
                tp = lib3mf.TriangleProperties()
                tp.ResourceID  = cg_uid
                tp.PropertyIDs = _UI3(color_id, color_id, color_id)
                props.append(tp)
            mesh_obj.SetAllTriangleProperties(props)

            model.AddBuildItem(mesh_obj, _identity_transform())

        writer = model.QueryWriter("3mf")
        writer.WriteToFile(path)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _render(self):
        tab = self._current_tab()
        if not tab:
            return
        source = tab.editor.toPlainText()
        if not source.strip():
            return

        # Clear console for this render run
        self._console.clear()

        from openscad_parser.ast import getASTfromString, getASTfromFile, getASTfromLibraryFile, build_scopes
        from openscad_parser.ast.nodes import UseStatement, ModuleDeclaration, FunctionDeclaration
        from neuscad.engine.evaluator import Evaluator, EvalError
        import numpy as np
        import io, sys as _sys, time as _time

        _t0 = _time.perf_counter()

        # --- Parse ---
        import tempfile
        _tmp = None
        try:
            buf = io.StringIO()
            old_stdout = _sys.stdout
            _sys.stdout = buf
            if tab.file_path:
                parse_path = tab.file_path
            else:
                # Write to a temp file so openscad_parser can resolve system library paths
                _tmp = tempfile.NamedTemporaryFile(
                    suffix=".scad", mode="w", encoding="utf-8", delete=False
                )
                _tmp.write(source)
                _tmp.close()
                parse_path = _tmp.name
            nodes = getASTfromFile(parse_path)
            _sys.stdout = old_stdout
            captured = buf.getvalue()
        except Exception as e:
            _sys.stdout = old_stdout
            self.log(f"Parse error: {e}")
            return
        finally:
            if _tmp is not None:
                import os as _os
                try:
                    _os.unlink(_tmp.name)
                except OSError:
                    pass

        if captured:
            self.log(captured.rstrip())

        if nodes is None:
            self._parse_error_to_editor(tab, captured)
            return

        # Resolve `use` statements: prepend module/function declarations from used files
        current_file = tab.file_path or parse_path
        injected = []
        for node in nodes:
            if isinstance(node, UseStatement):
                try:
                    lib_nodes, _ = getASTfromLibraryFile(current_file, node.filepath)
                    if lib_nodes:
                        injected.extend(
                            n for n in lib_nodes
                            if isinstance(n, (ModuleDeclaration, FunctionDeclaration))
                        )
                except Exception as e:
                    self.log(f"use error: {e}")
        if injected:
            nodes = injected + [n for n in nodes if not isinstance(n, UseStatement)]

        tab.editor.clear_errors()
        root_scope = build_scopes(nodes)

        # --- Evaluate ---
        evaluator = Evaluator(echo_fn=self.log)
        try:
            bodies, id_to_node = evaluator.evaluate(nodes, root_scope)
            tab.id_to_node = id_to_node
        except EvalError as e:
            self.log(f"Eval error: {e}")
            return
        except Exception as e:
            import traceback
            self.log(f"Runtime error: {e}\n{traceback.format_exc()}")
            return

        if not bodies:
            self.log("Render: no geometry produced.")
            return

        # --- Upload to viewport ---
        try:
            tab.viewport.load_geometry(bodies)
        except Exception as e:
            import traceback
            self.log(f"GPU upload error: {e}\n{traceback.format_exc()}")
            return

        tab._bodies = bodies

        # Frame camera and report bounds
        try:
            import manifold3d as m3d
            all_bodies = [b.body for b in bodies if not b.body.is_empty()]
            if all_bodies:
                composed = m3d.Manifold.compose(all_bodies)
                bb = composed.bounding_box()   # returns (xmin,ymin,zmin,xmax,ymax,zmax)
                bb_min = np.array([bb[0], bb[1], bb[2]], dtype=np.float32)
                bb_max = np.array([bb[3], bb[4], bb[5]], dtype=np.float32)
                tab.viewport.frame_scene(bb_min, bb_max)
                extent = float(np.linalg.norm(bb_max - bb_min))
                elapsed_ms = (_time.perf_counter() - _t0) * 1000
                self.log(
                    f"Render OK — bounds [{bb[0]:.2f},{bb[1]:.2f},{bb[2]:.2f}] to "
                    f"[{bb[3]:.2f},{bb[4]:.2f},{bb[5]:.2f}]  size {extent:.2f}  "
                    f"({elapsed_ms:.0f} ms)"
                )
        except Exception as e:
            import traceback
            self.log(f"Post-render error: {e}\n{traceback.format_exc()}")

    def _parse_error_to_editor(self, tab, captured: str):
        """Parse the error text from openscad_parser and mark the editor."""
        import re
        m = re.search(r"at line (\d+), column (\d+)", captured)
        if m:
            line, col = int(m.group(1)), int(m.group(2))
            tab.editor.set_error_location(line, col)

    def log(self, text):
        self._console.appendPlainText(text)

    # ------------------------------------------------------------------
    # Edit operations
    # ------------------------------------------------------------------

    def _edit_cut(self):
        if e := self._current_editor():
            e.cut()

    def _edit_copy(self):
        if e := self._current_editor():
            e.copy()

    def _edit_paste(self):
        if e := self._current_editor():
            e.paste()

    def _edit_select_all(self):
        if e := self._current_editor():
            e.selectAll()

    def _selection_expand(self):
        pass  # TODO: walk selection up AST

    def _selection_contract(self):
        pass  # TODO: walk selection down AST

    def _indent(self):
        if e := self._current_editor():
            cursor = e.textCursor()
            cursor.insertText("    ")

    def _undent(self):
        pass  # TODO: remove leading spaces

    def _comment(self):
        if e := self._current_editor():
            self._toggle_line_comment(e, add=True)

    def _uncomment(self):
        if e := self._current_editor():
            self._toggle_line_comment(e, add=False)

    def _toggle_line_comment(self, editor, add):
        cursor = editor.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        cursor.setPosition(start)
        cursor.movePosition(cursor.MoveOperation.StartOfLine)
        cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
        cursor.movePosition(cursor.MoveOperation.EndOfLine, cursor.MoveMode.KeepAnchor)
        text = cursor.selectedText()
        lines = text.split(" ")  # Qt paragraph separator
        if add:
            lines = ["// " + l for l in lines]
        else:
            lines = [l[3:] if l.startswith("// ") else l for l in lines]
        cursor.insertText(" ".join(lines))

    def _find(self):
        pass  # TODO: find bar

    def _find_replace(self):
        pass  # TODO: find & replace dialog

    def _current_editor(self):
        tab = self._current_tab()
        return tab.editor if tab else None

    # ------------------------------------------------------------------
    # View operations
    # ------------------------------------------------------------------

    def _toggle_editor(self, visible):
        tab = self._current_tab()
        if tab:
            tab.editor.setVisible(visible)

    def _toggle_tools_strip(self, visible):
        tab = self._current_tab()
        if tab:
            tab.tools_strip.setVisible(visible)

    def _toggle_console(self, visible):
        if visible:
            self._main_splitter.setSizes([
                self._main_splitter.height() - self._console_height,
                self._console_height,
            ])
        else:
            sizes = self._main_splitter.sizes()
            if sizes[1] > 0:
                self._console_height = sizes[1]
            self._main_splitter.setSizes([self._main_splitter.height(), 0])

    def _toggle_debugger(self, visible):
        self._debugger_pane.setVisible(visible)
        if visible:
            # Ensure the bottom section is visible
            sizes = self._main_splitter.sizes()
            if sizes[1] == 0:
                h = self._console_height
                self._main_splitter.setSizes([self._main_splitter.height() - h, h])

    # ------------------------------------------------------------------
    # Debug session
    # ------------------------------------------------------------------

    def _start_debug(self):
        tab = self._current_tab()
        if not tab:
            return
        # While paused, F5 acts as Continue
        if self._debug_session and self._debug_session.is_running():
            self._on_debug_continue()
            return

        source = tab.editor.toPlainText()
        if not source.strip():
            return

        self._console.clear()

        from openscad_parser.ast import getASTfromFile, getASTfromLibraryFile, build_scopes
        from openscad_parser.ast.nodes import UseStatement, ModuleDeclaration, FunctionDeclaration
        import tempfile, io, sys as _sys

        _tmp = None
        try:
            buf = io.StringIO()
            old_stdout = _sys.stdout
            _sys.stdout = buf
            if tab.file_path:
                parse_path = tab.file_path
            else:
                _tmp = tempfile.NamedTemporaryFile(
                    suffix=".scad", mode="w", encoding="utf-8", delete=False
                )
                _tmp.write(source)
                _tmp.close()
                parse_path = _tmp.name
            nodes = getASTfromFile(parse_path)
            _sys.stdout = old_stdout
            captured = buf.getvalue()
        except Exception as e:
            _sys.stdout = old_stdout
            self.log(f"Parse error: {e}")
            return
        finally:
            if _tmp is not None:
                import os as _os
                try:
                    _os.unlink(_tmp.name)
                except OSError:
                    pass

        if captured:
            self.log(captured.rstrip())
        if nodes is None:
            self._parse_error_to_editor(tab, captured)
            return

        current_file = tab.file_path or parse_path
        injected = []
        for node in nodes:
            if isinstance(node, UseStatement):
                try:
                    lib_nodes, _ = getASTfromLibraryFile(current_file, node.filepath)
                    if lib_nodes:
                        injected.extend(
                            n for n in lib_nodes
                            if isinstance(n, (ModuleDeclaration, FunctionDeclaration))
                        )
                except Exception as e:
                    self.log(f"use error: {e}")
        if injected:
            nodes = injected + [n for n in nodes if not isinstance(n, UseStatement)]

        tab.editor.clear_errors()
        root_scope = build_scopes(nodes)

        # Convert 0-indexed block numbers to 1-indexed line numbers
        breakpoints = {bn + 1 for bn in tab.editor._breakpoints}

        # Show the debugger pane
        self._debugger_pane.show()
        self._act_show_debugger.setChecked(True)
        sizes = self._main_splitter.sizes()
        if sizes[1] == 0:
            h = self._console_height
            self._main_splitter.setSizes([self._main_splitter.height() - h, h])

        self._debug_session = DebugSession(self)
        self._debug_session.paused.connect(
            lambda line, loc, stk, t=tab: self._on_debug_paused(t, line, loc, stk)
        )
        self._debug_session.finished.connect(
            lambda bodies, id2node, t=tab: self._on_debug_finished(t, bodies, id2node)
        )
        self._debug_session.errored.connect(self._on_debug_error)

        self._debugger_pane.set_running()
        self._debug_session.start(nodes, root_scope, breakpoints, self.log)

    def _on_debug_paused(self, tab, line: int, locals_dict: dict, call_stack: list):
        self._debugger_pane.set_paused(line, locals_dict, call_stack)
        tab.editor.set_execution_line(line)

    def _on_debug_finished(self, tab, bodies, id_to_node):
        tab.id_to_node = id_to_node
        tab.editor.clear_execution_line()
        self._debugger_pane.set_idle()
        self._debug_session = None
        if not bodies:
            self.log("Debug: no geometry produced.")
            return
        try:
            tab.viewport.load_geometry(bodies)
        except Exception as e:
            import traceback
            self.log(f"GPU upload error: {e}\n{traceback.format_exc()}")
            return
        try:
            import manifold3d as m3d
            import numpy as np
            all_bodies = [b.body for b in bodies if not b.body.is_empty()]
            if all_bodies:
                composed = m3d.Manifold.compose(all_bodies)
                bb = composed.bounding_box()
                bb_min = np.array([bb[0], bb[1], bb[2]], dtype=np.float32)
                bb_max = np.array([bb[3], bb[4], bb[5]], dtype=np.float32)
                tab.viewport.frame_scene(bb_min, bb_max)
                self.log("Debug: completed.")
        except Exception:
            pass

    def _on_debug_error(self, msg: str):
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
        self._debugger_pane.set_idle()
        self._debug_session = None
        self.log(f"Debug error: {msg}")

    def _on_debug_continue(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
        self._debugger_pane.set_running()
        self._debug_session.resume("continue", mods)

    def _on_debug_step_into(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
        self._debugger_pane.set_running()
        self._debug_session.resume("step_into", mods)

    def _on_debug_step_over(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
        self._debugger_pane.set_running()
        self._debug_session.resume("step_over", mods)

    def _on_debug_step_out(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
        self._debugger_pane.set_running()
        self._debug_session.resume("step_out", mods)

    def _on_debug_stop(self):
        if not self._debug_session:
            return
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
        self._debug_session.stop()
        self._debug_session = None
        self._debugger_pane.set_idle()

    def _toggle_axes(self, visible):
        tab = self._current_tab()
        if tab:
            tab.viewport._renderer.show_axes = visible
            tab.viewport.update()

    def _toggle_edges(self, visible):
        pass  # TODO: pass to renderer

    def _toggle_scale_markers(self, visible):
        pass  # TODO: pass to renderer

    def _toggle_crosshairs(self, visible):
        pass  # TODO: pass to renderer

    def _set_view(self, preset):
        tab = self._current_tab()
        if tab:
            tab.viewport.set_view_preset(preset)

    def _font_size_increase(self):
        if e := self._current_editor():
            f = e.font()
            f.setPointSize(f.pointSize() + 1)
            e.setFont(f)

    def _font_size_decrease(self):
        if e := self._current_editor():
            f = e.font()
            if f.pointSize() > 6:
                f.setPointSize(f.pointSize() - 1)
                e.setFont(f)

    def _zoom_viewport(self, direction):
        tab = self._current_tab()
        if tab:
            tab.viewport.zoom(direction)

    # ------------------------------------------------------------------
    # Window
    # ------------------------------------------------------------------

    def _bring_all_to_front(self):
        self.raise_()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_selection_changed(self, tab, orig_id: int):
        if orig_id < 0:
            tab.editor.clear_selection()
            return
        node = tab.id_to_node.get(orig_id)
        if node is None:
            tab.editor.clear_selection()
            return
        tab.editor.set_selection(node.position.start_offset, node.position.end_offset)

    # ------------------------------------------------------------------
    # Translate gizmo commit
    # ------------------------------------------------------------------

    def _on_translate_committed(self, tab, dx: float, dy: float, dz: float):
        orig_id = tab.viewport._renderer.selected_id
        if orig_id is None:
            return
        node = tab.id_to_node.get(orig_id)
        if node is None:
            return

        source = tab.editor.toPlainText()
        start = node.position.start_offset

        def _fmt(v: float) -> str:
            return f"{v:.4g}"

        # Detect an existing translate([x, y, z]) immediately before this node
        prefix = source[:start]
        m = re.search(
            r'translate\s*\(\s*\[\s*([^,\]]+?)\s*,\s*([^,\]]+?)\s*,\s*([^,\]]+?)\s*\]\s*\)\s*$',
            prefix
        )

        merged = False
        if m:
            try:
                ex, ey, ez = float(m.group(1)), float(m.group(2)), float(m.group(3))
                merged = True
            except ValueError:
                pass

        if merged:
            nx, ny, nz = ex + dx, ey + dy, ez + dz
            new_translate = f"translate([{_fmt(nx)}, {_fmt(ny)}, {_fmt(nz)}]) "
            match_start = m.start()
            new_source = source[:match_start] + new_translate + source[start:]
            new_node_start = match_start + len(new_translate)
        else:
            insert = f"translate([{_fmt(dx)}, {_fmt(dy)}, {_fmt(dz)}]) "
            new_source = source[:start] + insert + source[start:]
            new_node_start = start + len(insert)

        cmd = _GizmoCmd(
            tab, tab.editor, source, new_source, self._render,
            new_node_start, self._restore_selection_after_translate,
            merge_id=1001, label="Translate",
        )
        self._undo_stack.push(cmd)

    def _restore_selection_after_translate(self, tab, new_node_start: int):
        for orig_id, node in tab.id_to_node.items():
            if node.position.start_offset == new_node_start:
                tab.viewport._renderer.selected_id = orig_id
                tab.editor.set_selection(node.position.start_offset, node.position.end_offset)
                tab.viewport.update()
                return
        tab.viewport._renderer.selected_id = None
        tab.editor.clear_selection()
        tab.viewport.update()

    # ------------------------------------------------------------------
    # Rotate gizmo commit
    # ------------------------------------------------------------------

    def _on_rotate_committed(self, tab, axis: int, angle_deg: float):
        orig_id = tab.viewport._renderer.selected_id
        if orig_id is None:
            return
        node = tab.id_to_node.get(orig_id)
        if node is None:
            return

        source = tab.editor.toPlainText()
        start = node.position.start_offset

        def _fmt(v: float) -> str:
            return f"{v:.4g}"

        prefix = source[:start]
        m = re.search(
            r'rotate\s*\(\s*\[\s*([^,\]]+?)\s*,\s*([^,\]]+?)\s*,\s*([^,\]]+?)\s*\]\s*\)\s*$',
            prefix
        )

        merged = False
        if m:
            try:
                ex, ey, ez = float(m.group(1)), float(m.group(2)), float(m.group(3))
                merged = True
            except ValueError:
                pass

        if merged:
            vals = [ex, ey, ez]
            vals[axis] += angle_deg
            new_rotate = f"rotate([{_fmt(vals[0])}, {_fmt(vals[1])}, {_fmt(vals[2])}]) "
            match_start = m.start()
            new_source = source[:match_start] + new_rotate + source[start:]
            new_node_start = match_start + len(new_rotate)
        else:
            vals = [0.0, 0.0, 0.0]
            vals[axis] = angle_deg
            insert = f"rotate([{_fmt(vals[0])}, {_fmt(vals[1])}, {_fmt(vals[2])}]) "
            new_source = source[:start] + insert + source[start:]
            new_node_start = start + len(insert)

        cmd = _GizmoCmd(
            tab, tab.editor, source, new_source, self._render,
            new_node_start, self._restore_selection_after_translate,
            merge_id=1002, label="Rotate",
        )
        self._undo_stack.push(cmd)

    # ------------------------------------------------------------------
    # Scale gizmo commit
    # ------------------------------------------------------------------

    def _on_scale_committed(self, tab, axis: int, factor: float, uniform: bool):
        orig_id = tab.viewport._renderer.selected_id
        if orig_id is None:
            return
        node = tab.id_to_node.get(orig_id)
        if node is None:
            return

        source = tab.editor.toPlainText()
        start = node.position.start_offset

        def _fmt(v: float) -> str:
            return f"{v:.4g}"

        prefix = source[:start]
        m = re.search(
            r'scale\s*\(\s*\[\s*([^,\]]+?)\s*,\s*([^,\]]+?)\s*,\s*([^,\]]+?)\s*\]\s*\)\s*$',
            prefix
        )

        merged = False
        if m:
            try:
                ex, ey, ez = float(m.group(1)), float(m.group(2)), float(m.group(3))
                merged = True
            except ValueError:
                pass

        if merged:
            vals = [ex, ey, ez]
            if uniform:
                vals = [v * factor for v in vals]
            else:
                vals[axis] *= factor
            new_scale = f"scale([{_fmt(vals[0])}, {_fmt(vals[1])}, {_fmt(vals[2])}]) "
            match_start = m.start()
            new_source = source[:match_start] + new_scale + source[start:]
            new_node_start = match_start + len(new_scale)
        else:
            if uniform:
                vals = [factor, factor, factor]
            else:
                vals = [1.0, 1.0, 1.0]
                vals[axis] = factor
            insert = f"scale([{_fmt(vals[0])}, {_fmt(vals[1])}, {_fmt(vals[2])}]) "
            new_source = source[:start] + insert + source[start:]
            new_node_start = start + len(insert)

        cmd = _GizmoCmd(
            tab, tab.editor, source, new_source, self._render,
            new_node_start, self._restore_selection_after_translate,
            merge_id=1003, label="Scale",
        )
        self._undo_stack.push(cmd)

    # ------------------------------------------------------------------
    # Coordinate display
    # ------------------------------------------------------------------

    def show_clicked_coords(self, x, y, z):
        self._coord_label.setText(f"x: {x:.3f}  y: {y:.3f}  z: {z:.3f}")
