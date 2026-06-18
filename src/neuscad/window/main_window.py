from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QSplitter,
    QTabWidget, QPlainTextEdit, QToolBar, QStatusBar,
    QLabel, QMessageBox, QFileDialog, QToolButton, QButtonGroup,
    QDockWidget, QStackedWidget, QProgressBar, QApplication,
)
from PySide6.QtGui import QAction, QKeySequence, QFont, QIcon, QUndoCommand, QTextCursor
from PySide6.QtCore import Qt, QSize, QSettings, QThread, QObject, Signal, Slot
import threading
import time

from neuscad.window.editor import CodeEditor
from neuscad.window.viewport import Viewport
from neuscad.window.debugger import DebuggerPane, DebugSession
from neuscad.window.animate import AnimatePane
from neuscad.window.preferences import PreferencesDialog, load_preference, save_preferences

import re
from pathlib import Path
from typing import Optional

_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"
_TOOL_ICONS = {
    0: "tool-translate.svg",
    1: "tool-rotate.svg",
    2: "tool-scale.svg",
}


def _fmt_elapsed(elapsed_ms: float) -> str:
    if elapsed_ms >= 1000:
        return f"({elapsed_ms / 1000:.3f}s)"
    return f"({elapsed_ms:.0f} ms)"


def _resolve_use_scopes(nodes, current_file, log_fn):
    """Resolve `use <file>` statements per OpenSCAD semantics.

    Each top-level `UseStatement` is replaced by the used file's *own*
    module and function declarations — its top-level geometry and variable
    assignments are not injected, so `current_file`'s own variable namespace
    stays isolated from (and invisible to) the used file's globals.
    Declarations that the used file itself pulled in via a nested `use` are
    not re-exported ("nested use has no effect on the base file's
    environment").

    Returns `(processed_nodes, own_nodes, root_scope)`:
    - `processed_nodes` — what `current_file` should be evaluated as: its
      own nodes plus the declarations injected via its `use` statements.
    - `own_nodes` — `current_file`'s own nodes (minus `UseStatement`s),
      excluding anything injected via `use`; this is what gets exposed to
      whoever in turn `use`s `current_file`.
    - `root_scope` — built from `processed_nodes`, then each injected
      declaration is re-anchored to its own file's root scope (computed
      recursively), giving it access to its own file's globals without
      exposing them to `current_file`.
    """
    from openscad_parser.ast import getASTfromLibraryFile, build_scopes
    from openscad_parser.ast.nodes import UseStatement, ModuleDeclaration, FunctionDeclaration

    injected = []
    reanchor = []
    for node in nodes:
        if not isinstance(node, UseStatement):
            continue
        try:
            fp = node.filepath.val if hasattr(node.filepath, 'val') else node.filepath
            # `include`d files are flattened into `nodes`, so a `use` statement
            # may have originated from a different file than `current_file` —
            # resolve relative paths against where it was actually written.
            origin = getattr(getattr(node, 'position', None), 'origin', None)
            lib_nodes, lib_path = getASTfromLibraryFile(origin or current_file, fp, include_comments=False)
        except Exception as e:
            msg = str(e)
            if "not found" not in msg and "No such file" not in msg:
                log_fn(f"use error: {e}")
            continue
        if not lib_nodes:
            continue
        _, lib_own_nodes, lib_root_scope = _resolve_use_scopes(lib_nodes, lib_path, log_fn)
        lib_injected = [
            n for n in lib_own_nodes
            if isinstance(n, (ModuleDeclaration, FunctionDeclaration))
        ]
        injected.extend(lib_injected)
        if lib_injected:
            reanchor.append((lib_injected, lib_root_scope))

    own_nodes = [n for n in nodes if not isinstance(n, UseStatement)]
    processed_nodes = injected + own_nodes
    root_scope = build_scopes(processed_nodes)
    for lib_injected, lib_root_scope in reanchor:
        for n in lib_injected:
            n.build_scope(lib_root_scope)
    return processed_nodes, own_nodes, root_scope


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

        # Editor, console, and debugger pane live in dock stacks, not here —
        # kept as attributes so MainWindow can access them via tab.editor /
        # tab.console / tab.debugger_pane.
        self.editor = CodeEditor()
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Menlo", 11))
        self.debugger_pane = DebuggerPane()
        self.debug_session: DebugSession | None = None
        self.animate_pane = AnimatePane()
        self._dump_dir: Optional[str] = None

        self.viewport = Viewport()
        self.tools_strip = self._make_tools_strip()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.viewport)
        splitter.addWidget(self.tools_strip)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([800, 48])

        layout.addWidget(splitter)

        self.file_path = None
        self.is_modified = False
        self.root_scope = None

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


class _RenderCallback(QObject):
    """Lives in the main thread; receives cross-thread signals from _RenderWorker.

    Because this object is never moved to the worker thread, Qt auto-detects the
    thread boundary and uses QueuedConnection, routing all slots to the main thread.
    """

    def __init__(self, main_window, tab, render_id: int, parent=None):
        super().__init__(parent)
        self._mw = main_window
        self._tab = tab
        self._render_id = render_id

    @Slot(str)
    def on_logged(self, msg: str):
        if self._render_id == self._mw._render_id:
            self._mw.log_to_tab(self._tab, msg)

    @Slot(str)
    def on_parse_errored(self, captured: str):
        if self._render_id == self._mw._render_id:
            self._mw.log_to_tab(self._tab, captured.rstrip())
            self._mw._parse_error_to_editor(self._tab, captured)

    @Slot(object, object)
    def on_ast_ready(self, nodes, root_scope):
        if self._render_id == self._mw._render_id:
            self._tab.root_scope = root_scope

    @Slot(object, object, float)
    def on_finished(self, bodies, id_to_node, elapsed_ms: float):
        self._mw._on_render_done(self._tab, bodies, id_to_node, elapsed_ms, self._render_id)

    @Slot()
    def on_done(self):
        self._mw._set_render_busy(False)
        if self._render_id == self._mw._render_id:
            self._mw._on_render_thread_done(self._tab)


class _RenderWorker(QObject):
    """Runs parse + evaluate in a background thread. All signals are queued to the main thread."""
    logged = Signal(str)
    parse_errored = Signal(str)          # captured stdout; triggers editor error marking
    ast_ready = Signal(object, object)   # (nodes, root_scope) — emitted after successful parse
    finished = Signal(object, object, float)  # (bodies, id_to_node, elapsed_ms)
    done = Signal()                      # always emitted at end of run(), for thread cleanup

    def __init__(self, source: str, file_path, cancel: threading.Event, viewport_params: dict | None = None):
        super().__init__()
        self._source = source
        self._file_path = file_path
        self._cancel = cancel
        self._viewport_params = viewport_params or {}

    @Slot()
    def run(self):
        try:
            self._do_render()
        finally:
            self.done.emit()

    def _do_render(self):
        import io, sys as _sys, time as _time, os as _os, tempfile, traceback
        from openscad_parser.ast import getASTfromFile
        from neuscad.engine.evaluator import Evaluator, EvalError, to_renderable_bodies

        _t0 = _time.perf_counter()

        # --- Parse ---
        _tmp = None
        try:
            buf = io.StringIO()
            old_stdout = _sys.stdout
            _sys.stdout = buf
            if self._file_path:
                parse_path = self._file_path
            else:
                _tmp = tempfile.NamedTemporaryFile(
                    suffix=".scad", mode="w", encoding="utf-8", delete=False
                )
                _tmp.write(self._source)
                _tmp.close()
                parse_path = _tmp.name
            nodes = getASTfromFile(parse_path, include_comments=False)
            _sys.stdout = old_stdout
            captured = buf.getvalue()
        except Exception as e:
            _sys.stdout = old_stdout
            self.logged.emit(f"Parse error: {e}")
            return
        finally:
            if _tmp is not None:
                try:
                    _os.unlink(_tmp.name)
                except OSError:
                    pass

        if captured:
            self.logged.emit(captured.rstrip())

        if nodes is None:
            self.parse_errored.emit(captured)
            return

        if self._cancel.is_set():
            return

        # Resolve `use` statements and build scopes
        current_file = self._file_path or parse_path
        try:
            nodes, _own, root_scope = _resolve_use_scopes(nodes, current_file, self.logged.emit)
        except RecursionError:
            self.logged.emit("Error: AST too deeply nested (recursion limit exceeded during scope build).")
            return

        self.ast_ready.emit(nodes, root_scope)

        if self._cancel.is_set():
            return

        # --- Evaluate ---
        evaluator = Evaluator(echo_fn=self.logged.emit)
        try:
            bodies, id_to_node = evaluator.evaluate(nodes, root_scope, self._viewport_params)
        except RecursionError:
            elapsed_ms = (_time.perf_counter() - _t0) * 1000
            self.logged.emit(f"Error: AST too deeply nested (recursion limit exceeded during evaluation).  {_fmt_elapsed(elapsed_ms)}")
            return
        except EvalError as e:
            elapsed_ms = (_time.perf_counter() - _t0) * 1000
            self.logged.emit(f"Eval error:  {_fmt_elapsed(elapsed_ms)}\n{e}")
            return
        except Exception as e:
            elapsed_ms = (_time.perf_counter() - _t0) * 1000
            self.logged.emit(f"Runtime error:  {_fmt_elapsed(elapsed_ms)}\n{e}\n{traceback.format_exc()}")
            return

        if self._cancel.is_set():
            return

        elapsed_ms = (_time.perf_counter() - _t0) * 1000

        if not bodies:
            self.logged.emit(f"Render: no geometry produced.  {_fmt_elapsed(elapsed_ms)}")
            return

        bodies = to_renderable_bodies(bodies)
        self.finished.emit(bodies, id_to_node, elapsed_ms)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NeuSCAD")
        self.resize(1400, 900)

        self._undo_stack = self._create_undo_stack()
        self._render_cancel: threading.Event | None = None
        self._render_id: int = 0
        self._render_jobs: list = []  # (worker, callback, thread) kept alive until thread.finished
        self._setup_ui()
        self._setup_menus()
        self._setup_shortcuts()
        self._new_document()
        self._restore_settings()

    def _create_undo_stack(self):
        from PySide6.QtGui import QUndoStack
        return QUndoStack(self)

    # ------------------------------------------------------------------
    # UI assembly
    # ------------------------------------------------------------------

    def _setup_ui(self):
        self._toolbar = self._make_toolbar()
        self.addToolBar(self._toolbar)

        # Viewport tabs are the central widget; all panels live in dock widgets.
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self._tabs)

        # --- Editor dock (left by default) ---
        self._editor_stack = QStackedWidget()
        self._editor_dock = QDockWidget("Editor", self)
        self._editor_dock.setObjectName("EditorDock")
        self._editor_dock.setWidget(self._editor_stack)
        self._editor_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._editor_dock)

        # --- Console dock (bottom) ---
        self._console_stack = QStackedWidget()
        self._console_dock = QDockWidget("Console", self)
        self._console_dock.setObjectName("ConsoleDock")
        self._console_dock.setWidget(self._console_stack)
        self._console_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._console_dock)

        # --- Debugger dock (bottom, beside console) ---
        self._debugger_stack = QStackedWidget()
        self._debugger_dock = QDockWidget("Debugger", self)
        self._debugger_dock.setObjectName("DebuggerDock")
        self._debugger_dock.setWidget(self._debugger_stack)
        self._debugger_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._debugger_dock)
        self._debugger_dock.hide()
        self._debugger_dock.dockLocationChanged.connect(self._on_debugger_dock_location_changed)
        self._debugger_dock.topLevelChanged.connect(self._on_debugger_top_level_changed)

        # --- Animate dock (bottom, beside console) ---
        self._animate_stack = QStackedWidget()
        self._animate_dock = QDockWidget("Animate", self)
        self._animate_dock.setObjectName("AnimateDock")
        self._animate_dock.setWidget(self._animate_stack)
        self._animate_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._animate_dock)
        self._animate_dock.hide()

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._camera_label = QLabel("")
        self._status_bar.addWidget(self._camera_label)

        self._coord_label = QLabel("")
        self._status_bar.addWidget(self._coord_label)

        self._render_progress = QProgressBar()
        self._render_progress.setRange(0, 0)  # indeterminate / busy mode
        self._render_progress.setFixedWidth(120)
        self._render_progress.setTextVisible(False)
        self._render_progress.hide()
        self._status_bar.addPermanentWidget(self._render_progress)

    @staticmethod
    def _toolbar_icon(name: str) -> QIcon:
        path = _ICONS_DIR / f"toolbar-{name}.svg"
        return QIcon(str(path)) if path.exists() else QIcon()

    def _make_toolbar(self):
        tb = QToolBar("Main")
        tb.setObjectName("MainToolBar")
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        self._act_new = QAction(self._toolbar_icon("new"), "New", self)
        self._act_new.setToolTip("New (Ctrl+N)")
        self._act_new.triggered.connect(self._new_document)
        tb.addAction(self._act_new)

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

        self._act_debug_tb = QAction(self._toolbar_icon("debug"), "Debug", self)
        self._act_debug_tb.setToolTip("Debug (F5)")
        self._act_debug_tb.triggered.connect(self._start_debug)
        tb.addAction(self._act_debug_tb)

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
        edit_menu.addSeparator()
        self._act_word_wrap = QAction("Word Wrap", self, checkable=True)
        self._act_word_wrap.triggered.connect(self._toggle_word_wrap)
        edit_menu.addAction(self._act_word_wrap)
        edit_menu.addSeparator()
        prefs_act = self._add_action(edit_menu, "Preferences…", self._open_preferences, QKeySequence("Ctrl+,"))
        prefs_act.setMenuRole(QAction.MenuRole.PreferencesRole)

        # Design
        design_menu = mb.addMenu("Design")
        self._act_render_menu = self._add_action(design_menu, "Render", self._render, QKeySequence("F6"))
        design_menu.addSeparator()
        self._add_action(design_menu, "Flush Caches", self._flush_caches)
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

        self._act_show_editor = self._editor_dock.toggleViewAction()
        self._act_show_editor.setText("Show Code Editor")
        view_menu.addAction(self._act_show_editor)

        self._act_show_tools = self._add_checkable(view_menu, "Show Tools Strip", True, self._toggle_tools_strip)

        self._act_show_console = self._console_dock.toggleViewAction()
        self._act_show_console.setText("Show Console")
        view_menu.addAction(self._act_show_console)

        self._act_show_debugger = self._debugger_dock.toggleViewAction()
        self._act_show_debugger.setText("Show Debugger")
        view_menu.addAction(self._act_show_debugger)

        self._act_show_animate = self._animate_dock.toggleViewAction()
        self._act_show_animate.setText("Show Animate")
        view_menu.addAction(self._act_show_animate)

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
        self._act_perspective = self._add_checkable(view_menu, "Perspective", True, self._toggle_perspective)
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
        tab.viewport.camera_changed.connect(self._update_camera_label)
        self._editor_stack.addWidget(tab.editor)
        self._console_stack.addWidget(tab.console)
        self._debugger_stack.addWidget(tab.debugger_pane)
        self._animate_stack.addWidget(tab.animate_pane)
        tab.animate_pane.frame_changed.connect(lambda t, tb=tab: self._on_animate_frame(tb, t))
        tab.animate_pane.dump_started.connect(
            lambda tb=tab: self._on_dump_started(tb), Qt.ConnectionType.QueuedConnection
        )
        tab.animate_pane.dump_finished.connect(lambda tb=tab: self._on_dump_finished(tb))
        tab.debugger_pane.set_splitter_orientation(self._current_debugger_splitter_orientation())
        tab.debugger_pane.continue_requested.connect(self._on_debug_continue)
        tab.debugger_pane.pause_requested.connect(self._on_debug_pause)
        tab.debugger_pane.step_into_requested.connect(self._on_debug_step_into)
        tab.debugger_pane.step_over_requested.connect(self._on_debug_step_over)
        tab.debugger_pane.step_out_requested.connect(self._on_debug_step_out)
        tab.debugger_pane.restart_requested.connect(self._on_debug_restart)
        tab.debugger_pane.stop_requested.connect(self._on_debug_stop)
        tab.editor.go_to_definition_requested.connect(
            lambda word, t=tab: self._go_to_definition(t, word)
        )
        if hasattr(self, '_act_perspective'):
            self._apply_perspective_to_tab(tab)
            self._apply_preferences_to_tab(
                tab,
                QFont(load_preference("editor/fontFamily"), load_preference("editor/fontSize", int)),
                load_preference("editor/indentSize", int),
                load_preference("editor/showColumnGuide", bool),
                load_preference("editor/columnGuide", int),
            )
            self._apply_word_wrap_to_tab(tab)
        idx = self._tabs.addTab(tab, tab.display_name())
        self._tabs.setCurrentIndex(idx)

    def _current_tab(self):
        return self._tabs.currentWidget()

    def _update_camera_label(self):
        tab = self._tabs.currentWidget()
        if tab is None:
            self._camera_label.setText("")
            return
        import numpy as np
        cam = tab.viewport._renderer.camera
        vpt = np.asarray(cam.target)
        vpr_x = ((90.0 - float(cam.elevation)) % 360.0 + 360.0) % 360.0
        vpr_z = ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0
        vpd = float(cam.distance)
        self._camera_label.setText(
            f"$vpt = [{vpt[0]:.2f}, {vpt[1]:.2f}, {vpt[2]:.2f}],  "
            f"$vpr = [{vpr_x:.2f}, 0.00, {vpr_z:.2f}],  "
            f"$vpd = {vpd:.2f}"
        )

    def _tab_changed(self, index):
        tab = self._tabs.widget(index)
        if tab:
            self._editor_stack.setCurrentWidget(tab.editor)
            self._console_stack.setCurrentWidget(tab.console)
            self._debugger_stack.setCurrentWidget(tab.debugger_pane)
            self._animate_stack.setCurrentWidget(tab.animate_pane)
            self._update_camera_label()
        # Animation playback re-renders the active tab on every frame, so
        # pause any other tab's animation while it's not visible.
        for i in range(self._tabs.count()):
            other = self._tabs.widget(i)
            if other is not None and other is not tab:
                other.animate_pane.pause()

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
        if tab:
            tab.animate_pane.pause()
            self._editor_stack.removeWidget(tab.editor)
            self._console_stack.removeWidget(tab.console)
            self._debugger_stack.removeWidget(tab.debugger_pane)
            self._animate_stack.removeWidget(tab.animate_pane)
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

    def _create_and_add_tab(self, path: str, text: str) -> 'DocumentTab':
        """Create a fully-connected DocumentTab for an existing file and add it to the UI."""
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
        tab.viewport.camera_changed.connect(self._update_camera_label)
        self._editor_stack.addWidget(tab.editor)
        self._console_stack.addWidget(tab.console)
        self._debugger_stack.addWidget(tab.debugger_pane)
        self._animate_stack.addWidget(tab.animate_pane)
        tab.animate_pane.frame_changed.connect(lambda t, tb=tab: self._on_animate_frame(tb, t))
        tab.animate_pane.dump_started.connect(
            lambda tb=tab: self._on_dump_started(tb), Qt.ConnectionType.QueuedConnection
        )
        tab.animate_pane.dump_finished.connect(lambda tb=tab: self._on_dump_finished(tb))
        tab.debugger_pane.set_splitter_orientation(self._current_debugger_splitter_orientation())
        tab.debugger_pane.continue_requested.connect(self._on_debug_continue)
        tab.debugger_pane.pause_requested.connect(self._on_debug_pause)
        tab.debugger_pane.step_into_requested.connect(self._on_debug_step_into)
        tab.debugger_pane.step_over_requested.connect(self._on_debug_step_over)
        tab.debugger_pane.step_out_requested.connect(self._on_debug_step_out)
        tab.debugger_pane.restart_requested.connect(self._on_debug_restart)
        tab.debugger_pane.stop_requested.connect(self._on_debug_stop)
        tab.editor.go_to_definition_requested.connect(
            lambda word, t=tab: self._go_to_definition(t, word)
        )
        self._apply_perspective_to_tab(tab)
        self._apply_preferences_to_tab(
            tab,
            QFont(load_preference("editor/fontFamily"), load_preference("editor/fontSize", int)),
            load_preference("editor/indentSize", int),
            load_preference("editor/showColumnGuide", bool),
            load_preference("editor/columnGuide", int),
        )
        self._apply_word_wrap_to_tab(tab)
        idx = self._tabs.addTab(tab, tab.display_name())
        self._tabs.setCurrentIndex(idx)
        return tab

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
        self._create_and_add_tab(path, text)
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
        self._create_and_add_tab(path, text)
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

    def _viewport_params(self, tab) -> dict:
        """Snapshot camera state and animation time as OpenSCAD $vp*/$t special variables."""
        params = {"$t": tab.animate_pane.current_t()}
        try:
            cam = tab.viewport._renderer.camera
            params.update({
                "$vpt": cam.target.tolist(),
                "$vpr": [
                    ((90.0 - float(cam.elevation)) % 360.0 + 360.0) % 360.0,
                    0.0,
                    ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0,
                ],
                "$vpd": float(cam.distance),
            })
        except Exception:
            pass
        return params

    def _render(self, tab=None):
        if tab is None:
            tab = self._current_tab()
        if not tab:
            return
        source = tab.editor.toPlainText()
        if not source.strip():
            return

        # Cancel any in-progress render (cooperative: worker checks the event between steps)
        if self._render_cancel is not None:
            self._render_cancel.set()

        self._render_id += 1
        render_id = self._render_id
        tab.editor.clear_errors()
        tab.console.clear()
        self.log_to_tab(tab, "Rendering…")

        cancel = threading.Event()
        self._render_cancel = cancel
        self._set_render_busy(True)

        worker = _RenderWorker(source, tab.file_path, cancel, self._viewport_params(tab))
        callback = _RenderCallback(self, tab, render_id, parent=self)
        thread = QThread(self)
        worker.moveToThread(thread)

        # Animation playback can start a new render before the previous one's
        # thread has finished (or even started); keep every in-flight
        # worker/callback/thread alive until its thread.finished fires, so
        # Qt never tries to invoke a slot on an object Python has already GC'd.
        job = (worker, callback, thread)
        self._render_jobs.append(job)

        def _cleanup_job(job=job):
            if job in self._render_jobs:
                self._render_jobs.remove(job)

        # callback lives in the main thread; Qt auto-uses QueuedConnection for all
        # of these cross-thread connections, so all slots run on the main thread.
        thread.started.connect(worker.run)
        worker.logged.connect(callback.on_logged)
        worker.parse_errored.connect(callback.on_parse_errored)
        worker.ast_ready.connect(callback.on_ast_ready)
        worker.finished.connect(callback.on_finished)
        worker.done.connect(callback.on_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(_cleanup_job)

        thread.start()

    def _on_animate_frame(self, tab, t: float):
        if tab.animate_pane.is_dumping():
            tab._dump_frame = tab.animate_pane.current_step()
        self._render(tab)

    def _on_dump_started(self, tab):
        if tab._dump_dir is None:
            path = QFileDialog.getExistingDirectory(self, "Dump Animation Frames To")
            if not path:
                tab.animate_pane.pause()
                return
            tab._dump_dir = path
        self.log_to_tab(tab, f"Dumping animation frames to {tab._dump_dir}")

    def _on_dump_finished(self, tab):
        self.log_to_tab(tab, "Animation frame dump complete.")

    def _set_render_busy(self, busy: bool):
        if busy:
            self._render_progress.show()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            self._render_progress.hide()
            QApplication.restoreOverrideCursor()

    def _on_render_done(self, tab, bodies, id_to_node, elapsed_ms: float, render_id: int):
        if render_id != self._render_id:
            return  # superseded by a later render; discard

        tab.id_to_node = id_to_node
        try:
            tab.viewport.load_geometry(bodies)
        except Exception as e:
            import traceback
            self.log_to_tab(tab, f"GPU upload error: {e}\n{traceback.format_exc()}")
            return

        tab._bodies = bodies

        try:
            import manifold3d as m3d
            import numpy as np
            all_bodies = [b.body for b in bodies if not b.body.is_empty()]
            if all_bodies:
                composed = m3d.Manifold.compose(all_bodies)
                bb = composed.bounding_box()
                bb_min = np.array([bb[0], bb[1], bb[2]], dtype=np.float32)
                bb_max = np.array([bb[3], bb[4], bb[5]], dtype=np.float32)
                # Animation playback keeps the camera fixed across frames; only
                # auto-fit on explicit (non-animation) renders.
                if not tab.animate_pane.is_playing():
                    tab.viewport.frame_scene(bb_min, bb_max)
                extent = float(np.linalg.norm(bb_max - bb_min))
                self.log_to_tab(
                    tab,
                    f"Render OK — bounds [{bb[0]:.2f},{bb[1]:.2f},{bb[2]:.2f}] to "
                    f"[{bb[3]:.2f},{bb[4]:.2f},{bb[5]:.2f}]  size {extent:.2f}  "
                    f"{_fmt_elapsed(elapsed_ms)}"
                )
        except Exception as e:
            import traceback
            self.log_to_tab(tab, f"Post-render error: {e}\n{traceback.format_exc()}")

    def _on_render_thread_done(self, tab):
        """Called once the render worker thread has fully finished.

        Dumping is paced from here (rather than from _on_render_done, which
        runs while the worker thread is still tearing down) so the next
        frame's worker thread never starts while the previous one is still
        touching the parser — see AnimatePane.play()/advance_frame().
        """
        if tab.animate_pane.is_dumping() and tab._dump_dir:
            try:
                frame = getattr(tab, "_dump_frame", tab.animate_pane.current_step())
                image = tab.viewport.grabFramebuffer()
                filename = f"frame{frame:04d}.png"
                image.save(str(Path(tab._dump_dir) / filename))
                self.log_to_tab(tab, f"Dumped {filename}")
            except Exception as e:
                self.log_to_tab(tab, f"Frame dump error: {e}")
            tab.animate_pane.advance_frame()

    def _flush_caches(self):
        """Discard each tab's pre-calculated AST scope/node table and openscad_parser's AST cache."""
        from openscad_parser.ast import clear_ast_cache
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab:
                tab.root_scope = None
                tab.id_to_node = {}
        clear_ast_cache()
        self.log("Flushed AST caches — render or debug to rebuild.")

    def _parse_error_to_editor(self, tab, captured: str):
        """Parse the error text from openscad_parser and mark the editor."""
        import re
        m = re.search(r"at line (\d+), column (\d+)", captured)
        if m:
            line, col = int(m.group(1)), int(m.group(2))
            tab.editor.set_error_location(line, col)

    def log(self, text: str):
        tab = self._current_tab()
        if tab:
            tab.console.appendPlainText(text)

    def log_to_tab(self, tab, text: str):
        tab.console.appendPlainText(text)

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
        if not (e := self._current_editor()):
            return
        cursor = e.textCursor()
        spaces = " " * e._indent_size
        doc = e.document()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            start_bn = doc.findBlock(cursor.selectionStart()).blockNumber()
            end_bn = doc.findBlock(cursor.selectionEnd()).blockNumber()
            end_cur = QTextCursor(doc)
            end_cur.setPosition(cursor.selectionEnd())
            if end_cur.atBlockStart() and end_bn > start_bn:
                end_bn -= 1
            for bn in range(start_bn, end_bn + 1):
                bc = QTextCursor(doc.findBlockByNumber(bn))
                bc.insertText(spaces)
        else:
            bc = QTextCursor(cursor.block())
            bc.insertText(spaces)
        cursor.endEditBlock()

    def _undent(self):
        if not (e := self._current_editor()):
            return
        cursor = e.textCursor()
        n = e._indent_size
        doc = e.document()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            start_bn = doc.findBlock(cursor.selectionStart()).blockNumber()
            end_bn = doc.findBlock(cursor.selectionEnd()).blockNumber()
            end_cur = QTextCursor(doc)
            end_cur.setPosition(cursor.selectionEnd())
            if end_cur.atBlockStart() and end_bn > start_bn:
                end_bn -= 1
            for bn in range(start_bn, end_bn + 1):
                block = doc.findBlockByNumber(bn)
                text = block.text()
                n_sp = min(n, len(text) - len(text.lstrip()))
                if n_sp > 0:
                    bc = QTextCursor(block)
                    bc.movePosition(bc.MoveOperation.Right, bc.MoveMode.KeepAnchor, n_sp)
                    bc.removeSelectedText()
        else:
            block = cursor.block()
            text = block.text()
            n_sp = min(n, len(text) - len(text.lstrip()))
            if n_sp > 0:
                bc = QTextCursor(block)
                bc.movePosition(bc.MoveOperation.Right, bc.MoveMode.KeepAnchor, n_sp)
                bc.removeSelectedText()
        cursor.endEditBlock()

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
        tab = self._current_tab()
        if tab:
            tab.editor.show_find(replace=False)

    def _find_replace(self):
        tab = self._current_tab()
        if tab:
            tab.editor.show_find(replace=True)

    def _go_to_definition(self, tab, word: str):
        scope = getattr(tab, 'root_scope', None)
        if scope is None:
            self.log_to_tab(tab, f"Go to Definition: no AST available (render or debug first)")
            return

        node = (scope.lookup_variable(word)
                or scope.lookup_function(word)
                or scope.lookup_module(word))

        if node is None:
            self.log_to_tab(tab, f"Go to Definition: no definition found for '{word}'")
            return

        pos = node.position
        def_line = pos.line
        def_file = getattr(pos, 'origin', None)

        # Determine which tab contains the definition
        target_tab = None
        if not def_file or not tab.file_path:
            target_tab = tab
        else:
            def_resolved = str(Path(def_file).resolve())
            tab_resolved = str(Path(tab.file_path).resolve())
            if def_resolved == tab_resolved:
                target_tab = tab
            else:
                for i in range(self._tabs.count()):
                    t = self._tabs.widget(i)
                    if t and t.file_path and str(Path(t.file_path).resolve()) == def_resolved:
                        target_tab = t
                        self._tabs.setCurrentIndex(i)
                        break
                if target_tab is None:
                    try:
                        with open(def_file, "r", encoding="utf-8") as f:
                            text = f.read()
                    except OSError as e:
                        self.log_to_tab(tab, f"Go to Definition: cannot open '{def_file}': {e}")
                        return
                    target_tab = self._create_and_add_tab(def_file, text)

        editor = target_tab.editor
        block = editor.document().findBlockByLineNumber(def_line - 1)
        if block.isValid():
            cursor = editor.textCursor()
            cursor.setPosition(block.position())
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()
        self._editor_dock.show()
        self._editor_dock.raise_()
        self._editor_stack.setCurrentWidget(editor)

    def _current_editor(self):
        tab = self._current_tab()
        return tab.editor if tab else None

    # ------------------------------------------------------------------
    # View operations
    # ------------------------------------------------------------------

    def _toggle_tools_strip(self, visible):
        tab = self._current_tab()
        if tab:
            tab.tools_strip.setVisible(visible)

    # ------------------------------------------------------------------
    # Debug session
    # ------------------------------------------------------------------

    def _start_debug(self):
        tab = self._current_tab()
        if not tab:
            return
        # While paused, F5 acts as Continue
        if tab.debug_session and tab.debug_session.is_running():
            self._on_debug_continue()
            return

        source = tab.editor.toPlainText()
        if not source.strip():
            return

        tab.console.clear()

        from openscad_parser.ast import getASTfromFile
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
            nodes = getASTfromFile(parse_path, include_comments=False)
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
        try:
            nodes, _own, root_scope = _resolve_use_scopes(nodes, current_file, self.log)
        except RecursionError:
            self.log("Error: AST too deeply nested (recursion limit exceeded during scope build).")
            return

        tab.editor.clear_errors()
        tab.root_scope = root_scope

        # Convert 0-indexed block numbers to 1-indexed line numbers
        breakpoints = {bn + 1 for bn in tab.editor._breakpoints}

        # Show the debugger dock and bring it to the front
        self._debugger_dock.show()
        self._debugger_dock.raise_()

        tab.debug_session = DebugSession(self)
        tab.debug_session.paused.connect(
            lambda line, frames, stk, t=tab: self._on_debug_paused(t, line, frames, stk)
        )
        tab.debug_session.error_break.connect(
            lambda line, msg, frames, stk, t=tab: self._on_debug_error_break(t, line, msg, frames, stk)
        )
        tab.debug_session.finished.connect(
            lambda bodies, id2node, t=tab: self._on_debug_finished(t, bodies, id2node)
        )
        tab.debug_session.errored.connect(self._on_debug_error)

        tab.debugger_pane.set_running()
        tab.debug_session.start(nodes, root_scope, breakpoints,
                                lambda msg, t=tab: self.log_to_tab(t, msg),
                                self._viewport_params(tab))

    def _on_debug_paused(self, tab, line: int, all_frame_locals: list, call_stack: list):
        tab.debugger_pane.set_paused(line, all_frame_locals, call_stack)
        tab.editor.set_execution_line(line)

    def _on_debug_error_break(self, tab, line: int, msg: str, all_frame_locals: list, call_stack: list):
        tab.debugger_pane.set_error_break(line, msg, all_frame_locals, call_stack)
        tab.editor.set_execution_line(line)

    def _on_debug_finished(self, tab, bodies, id_to_node):
        from neuscad.engine.evaluator import to_renderable_bodies

        tab.id_to_node = id_to_node
        tab.editor.clear_execution_line()
        tab.debugger_pane.set_idle()
        tab.debug_session = None
        if not bodies:
            self.log_to_tab(tab, "Debug: no geometry produced.")
            return

        bodies = to_renderable_bodies(bodies)
        try:
            tab.viewport.load_geometry(bodies)
        except Exception as e:
            import traceback
            self.log_to_tab(tab, f"GPU upload error: {e}\n{traceback.format_exc()}")
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
                self.log_to_tab(tab, "Debug: completed.")
        except Exception:
            pass

    def _on_debug_error(self, msg: str):
        tab = self._current_tab()
        if tab:
            tab.editor.clear_execution_line()
            tab.debugger_pane.set_idle()
            tab.debug_session = None
        self.log(f"Debug error:\n{msg}")

    def _on_debug_continue(self):
        tab = self._current_tab()
        if not tab or not tab.debug_session:
            return
        mods = tab.debugger_pane.get_modifications()
        tab.editor.clear_execution_line()
        tab.debugger_pane.set_running()
        tab.debug_session.resume("continue", mods)

    def _on_debug_pause(self):
        tab = self._current_tab()
        if not tab or not tab.debug_session:
            return
        tab.debug_session.pause()

    def _on_debug_step_into(self):
        tab = self._current_tab()
        if not tab or not tab.debug_session:
            return
        mods = tab.debugger_pane.get_modifications()
        tab.editor.clear_execution_line()
        tab.debugger_pane.set_running()
        tab.debug_session.resume("step_into", mods)

    def _on_debug_step_over(self):
        tab = self._current_tab()
        if not tab or not tab.debug_session:
            return
        mods = tab.debugger_pane.get_modifications()
        tab.editor.clear_execution_line()
        tab.debugger_pane.set_running()
        tab.debug_session.resume("step_over", mods)

    def _on_debug_step_out(self):
        tab = self._current_tab()
        if not tab or not tab.debug_session:
            return
        mods = tab.debugger_pane.get_modifications()
        tab.editor.clear_execution_line()
        tab.debugger_pane.set_running()
        tab.debug_session.resume("step_out", mods)

    def _on_debug_restart(self):
        tab = self._current_tab()
        if not tab:
            return
        if tab.debug_session:
            tab.debug_session.stop()
            tab.debug_session = None
        tab.editor.clear_execution_line()
        self._start_debug()

    def _on_debug_stop(self):
        tab = self._current_tab()
        if not tab or not tab.debug_session:
            return
        tab.editor.clear_execution_line()
        tab.debug_session.stop()
        tab.debug_session = None
        tab.debugger_pane.set_idle()

    def _open_preferences(self):
        dialog = PreferencesDialog(parent=self)
        if dialog.exec() == PreferencesDialog.DialogCode.Accepted:
            save_preferences(dialog.get_values())
            self._apply_preferences()

    def _apply_preferences(self):
        family = load_preference("editor/fontFamily")
        size = load_preference("editor/fontSize", int)
        indent = load_preference("editor/indentSize", int)
        show_guide = load_preference("editor/showColumnGuide", bool)
        guide_col = load_preference("editor/columnGuide", int)
        font = QFont(family, size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab:
                self._apply_preferences_to_tab(tab, font, indent, show_guide, guide_col)

    @staticmethod
    def _apply_preferences_to_tab(tab, font: QFont, indent: int, show_guide: bool, guide_col: int):
        tab.editor.setFont(font)
        tab.editor.set_indent_size(indent)
        tab.editor._column_guide.set_column(guide_col)
        tab.editor._column_guide.setVisible(show_guide)

    def _restore_settings(self):
        s = QSettings("NeuSCAD", "NeuSCAD")
        geometry = s.value("windowGeometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = s.value("windowState")
        if state is not None:
            self.restoreState(state)
        perspective = s.value("perspective", True, type=bool)
        self._act_perspective.blockSignals(True)
        self._act_perspective.setChecked(perspective)
        self._act_perspective.blockSignals(False)
        self._toggle_perspective(perspective)
        word_wrap = s.value("wordWrap", False, type=bool)
        self._act_word_wrap.blockSignals(True)
        self._act_word_wrap.setChecked(word_wrap)
        self._act_word_wrap.blockSignals(False)
        self._toggle_word_wrap(word_wrap)
        self._apply_preferences()

    def closeEvent(self, event):
        # Stop animation playback (no more renders get queued) and let any
        # in-flight render thread finish — Qt aborts if a QThread is
        # destroyed while still running.
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab is not None:
                tab.animate_pane.pause()
        if self._render_cancel is not None:
            self._render_cancel.set()
        deadline = time.monotonic() + 5.0
        while any(t.isRunning() for _, _, t in self._render_jobs) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)

        s = QSettings("NeuSCAD", "NeuSCAD")
        s.setValue("windowGeometry", self.saveGeometry())
        s.setValue("windowState", self.saveState())
        s.setValue("perspective", self._act_perspective.isChecked())
        s.setValue("wordWrap", self._act_word_wrap.isChecked())
        # Flush settings to disk now: the app exits via os._exit() (see
        # main.py), which skips QSettings' normal sync-on-destruction.
        s.sync()
        # Release all Manifold geometry before shutdown so nanobind sees clean
        # refcounts. Do NOT call gc.collect() here: forcing a GC pass that
        # collects nanobind-wrapped Manifold/CrossSection objects shortly
        # after a background render thread has been active can SIGSEGV
        # (nanobind's object collection isn't safe across threads). Plain
        # refcounting from clearing these references is sufficient for
        # nanobind to free the objects during interpreter shutdown.
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab is not None:
                tab._bodies = []
                tab.viewport.load_geometry([])
        super().closeEvent(event)

    def _apply_perspective_to_tab(self, tab):
        tab.viewport._renderer.camera.orthographic = not self._act_perspective.isChecked()

    def _apply_word_wrap_to_tab(self, tab):
        from PySide6.QtWidgets import QPlainTextEdit
        enabled = self._act_word_wrap.isChecked()
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if enabled else QPlainTextEdit.LineWrapMode.NoWrap
        tab.editor.setLineWrapMode(mode)

    def _current_debugger_splitter_orientation(self):
        if self._debugger_dock.isFloating():
            return Qt.Orientation.Horizontal
        area = self.dockWidgetArea(self._debugger_dock)
        if area in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea):
            return Qt.Orientation.Vertical
        return Qt.Orientation.Horizontal

    def _apply_debugger_splitter_orientation(self, orientation):
        for i in range(self._debugger_stack.count()):
            self._debugger_stack.widget(i).set_splitter_orientation(orientation)

    def _on_debugger_dock_location_changed(self, area):
        vertical = area in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea)
        orientation = Qt.Orientation.Vertical if vertical else Qt.Orientation.Horizontal
        self._apply_debugger_splitter_orientation(orientation)

    def _on_debugger_top_level_changed(self, floating: bool):
        if floating:
            self._apply_debugger_splitter_orientation(Qt.Orientation.Horizontal)

    def _toggle_word_wrap(self, enabled: bool):
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab:
                self._apply_word_wrap_to_tab(tab)

    def _toggle_perspective(self, perspective: bool):
        tab = self._current_tab()
        if tab:
            tab.viewport._renderer.camera.orthographic = not perspective
            tab.viewport.update()

    def _toggle_axes(self, visible):
        tab = self._current_tab()
        if tab:
            tab.viewport._renderer.show_axes = visible
            tab.viewport.update()

    def _toggle_edges(self, visible):
        tab = self._current_tab()
        if tab:
            tab.viewport._renderer.show_edges = visible
            tab.viewport.update()

    def _toggle_scale_markers(self, visible):
        tab = self._current_tab()
        if tab:
            tab.viewport._renderer.show_scale_markers = visible
            tab.viewport.update()

    def _toggle_crosshairs(self, visible):
        tab = self._current_tab()
        if tab:
            tab.viewport._renderer.show_crosshairs = visible
            tab.viewport.update()

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
