from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QTabBar, QStackedWidget, QPlainTextEdit, QToolBar, QStatusBar,
    QLabel, QMessageBox, QFileDialog, QToolButton, QButtonGroup,
    QDockWidget, QApplication, QMenu,
)
from PySide6.QtGui import QAction, QKeySequence, QFont, QIcon, QShortcut, QUndoCommand, QTextCursor
from PySide6.QtCore import Qt, QSize, QSettings, QThread, QObject, QTimer, Signal, Slot
import threading
import time

from belfryscad.window.editor import CodeEditor
from belfryscad.window.console import ConsoleWidget
from belfryscad.window.viewport import Viewport
from belfryscad.window.debugger import DebuggerPane, DebugSession, _pretty_assignment
from belfryscad.window.animate import AnimatePane
from belfryscad.window.customizer import CustomizerPane
from belfryscad.window.preferences import PreferencesDialog, load_preference, save_preferences
from belfryscad.window.document_manager import get_document_manager

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
    from openscad_lalr_parser import getASTfromLibraryFile, build_scopes
    from openscad_lalr_parser.nodes import UseStatement, ModuleDeclaration, FunctionDeclaration

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
    def __init__(self, tab, editor, before, after, render_fn, new_node_start, restore_fn,
                 merge_id, label, viewport):
        super().__init__(label)
        self._tab = tab
        self._viewport = viewport
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
        self._viewport._renderer.selected_id = None
        self._editor.clear_selection()
        self._viewport.update()

    def redo(self):
        self._tab._suppress_text_undo = True
        self._editor.setPlainText(self._after)
        self._tab._suppress_text_undo = False
        self._tab._last_text = self._after
        self._render()
        self._restore(self._new_node_start)


class FileTab(QWidget):
    """Per-editor-tab widget: holds only the CodeEditor and per-file metadata."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = CodeEditor()
        layout.addWidget(self.editor)
        self.file_path = None
        self.is_modified = False
        self.root_scope = None
        self._last_text = ""
        self._last_cursor = 0
        self._suppress_text_undo = False

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

    def __init__(self, main_window, file_tab, render_id: int, parent=None):
        super().__init__(parent)
        self._mw = main_window
        self._file_tab = file_tab
        self._render_id = render_id

    @Slot(str)
    def on_logged(self, msg: str):
        if self._render_id == self._mw._render_id:
            self._mw._console.append_output(msg)

    @Slot(str)
    def on_parse_errored(self, captured: str):
        if self._render_id == self._mw._render_id:
            self._mw._console.append_output(captured.rstrip())
            self._mw._parse_error_to_editor(self._file_tab, captured)

    @Slot(object, object)
    def on_ast_ready(self, nodes, root_scope):
        if self._render_id == self._mw._render_id:
            self._file_tab.root_scope = root_scope
            self._file_tab.editor.update_user_names(root_scope)

    @Slot(object, object, float, object)
    def on_finished(self, bodies, id_to_node, elapsed_ms: float, final_vp: dict):
        self._mw._on_render_done(self._file_tab, bodies, id_to_node, elapsed_ms, self._render_id, final_vp)

    @Slot()
    def on_done(self):
        self._mw._set_render_busy(False)
        if self._render_id == self._mw._render_id:
            self._mw._on_render_thread_done(self._file_tab)


class _RenderWorker(QObject):
    """Runs parse + evaluate in a background thread. All signals are queued to the main thread."""
    logged = Signal(str)
    parse_errored = Signal(str)          # captured stdout; triggers editor error marking
    ast_ready = Signal(object, object)   # (nodes, root_scope) — emitted after successful parse
    finished = Signal(object, object, float, object)  # (bodies, id_to_node, elapsed_ms, final_vp)
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
        from openscad_lalr_parser import getASTfromFile
        from belfryscad.engine.evaluator import Evaluator, EvalError, to_renderable_bodies

        _t0 = _time.perf_counter()

        # --- Parse ---
        _tmp = None
        try:
            buf = io.StringIO()
            old_stdout = _sys.stdout
            _sys.stdout = buf
            _tmp = tempfile.NamedTemporaryFile(
                suffix=".scad", mode="w", encoding="utf-8", delete=False,
                dir=_os.path.dirname(self._file_path) if self._file_path else None,
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
        final_vp = {}
        if evaluator._root_ctx is not None:
            dyn = evaluator._root_ctx.dyn
            for k in ("$vpt", "$vpr", "$vpd", "$vpf"):
                if k in dyn:
                    v = dyn[k]
                    final_vp[k] = v.tolist() if hasattr(v, "tolist") else v
        self.finished.emit(bodies, id_to_node, elapsed_ms, final_vp)


class _DetachedTabBar(QWidget):
    """A `QTabWidget`-compatible facade whose `QTabBar` (`self.tab_bar`) is a
    free-standing widget the caller places wherever it likes — e.g. in a
    toolbar spanning the full window width, rather than confined to
    whatever dock happens to contain the tab pages — while the pages
    themselves live in a `QStackedWidget` that *is* this widget's own
    layout (so `self` can drop into a dock exactly where a plain
    `QTabWidget` used to). Implements only the subset of `QTabWidget`'s
    API `MainWindow` actually uses, so no call site needs to change."""

    currentChanged = Signal(int)
    tabCloseRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tab_bar = QTabBar()
        self._stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self.tab_bar.currentChanged.connect(self._on_bar_current_changed)
        self.tab_bar.tabCloseRequested.connect(self.tabCloseRequested)
        self.tab_bar.tabMoved.connect(self._on_tab_moved)

    def _on_bar_current_changed(self, index: int):
        self._stack.setCurrentIndex(index)
        self.currentChanged.emit(index)

    def _on_tab_moved(self, from_index: int, to_index: int):
        widget = self._stack.widget(from_index)
        self._stack.removeWidget(widget)
        self._stack.insertWidget(to_index, widget)

    def addTab(self, widget: QWidget, label: str) -> int:
        stack_index = self._stack.addWidget(widget)
        bar_index = self.tab_bar.addTab(label)
        assert stack_index == bar_index, "tab_bar and stack indices diverged"
        return bar_index

    def removeTab(self, index: int):
        widget = self._stack.widget(index)
        if widget is not None:
            self._stack.removeWidget(widget)
        self.tab_bar.removeTab(index)

    def widget(self, index: int) -> QWidget | None:
        return self._stack.widget(index)

    def count(self) -> int:
        return self._stack.count()

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def setCurrentIndex(self, index: int):
        self.tab_bar.setCurrentIndex(index)
        # QTabBar doesn't emit currentChanged when the index is unchanged,
        # but the stack still needs to reflect it the first time a page is
        # added at the already-current index.
        self._stack.setCurrentIndex(index)

    def currentWidget(self) -> QWidget | None:
        return self._stack.currentWidget()

    def setCurrentWidget(self, widget: QWidget):
        self.setCurrentIndex(self._stack.indexOf(widget))

    def indexOf(self, widget: QWidget) -> int:
        return self._stack.indexOf(widget)

    def setTabText(self, index: int, text: str):
        self.tab_bar.setTabText(index, text)

    def setTabsClosable(self, closable: bool):
        self.tab_bar.setTabsClosable(closable)

    def setMovable(self, movable: bool):
        self.tab_bar.setMovable(movable)

    def setDocumentMode(self, doc_mode: bool):
        self.tab_bar.setDocumentMode(doc_mode)

    def setTabPosition(self, position):
        pass  # the tab bar always renders in its own strip; only "North" is ever requested

    def tabBar(self) -> QTabBar:
        return self.tab_bar


class MainWindow(QMainWindow):
    # Increment whenever the dock layout structure changes so stale saved
    # states are discarded rather than applied on top of the new layout.
    _LAYOUT_VERSION = 4

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BelfrySCAD")
        self.resize(1400, 900)

        self.setAcceptDrops(True)
        self._undo_stack = self._create_undo_stack()
        self._render_cancel: threading.Event | None = None
        self._render_id: int = 0
        self._render_jobs: list = []  # (worker, callback, thread) kept alive until thread.finished
        # Window-level render results (shared by viewport, export, gizmo, selection)
        self.id_to_node: dict = {}
        self._bodies = None
        self._rendered_tab: FileTab | None = None  # tab that produced the current viewport geometry
        self._dump_dir: Optional[str] = None
        self._dump_frame: int = 0
        self._first_show = True
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

        # Tab bar strip: a full-width row directly under the toolbar, holding
        # only the editor tab bar. A dedicated QToolBar is used (rather than
        # putting the tab bar inside the Editor dock, as a plain QTabWidget
        # would) because the toolbar area always spans the full window width
        # above the dock/central-widget area, whereas the Editor dock is only
        # as wide as its own dock area — cramped once many tabs are open.
        self._tab_bar_toolbar = QToolBar("Tab Bar")
        self._tab_bar_toolbar.setObjectName("TabBarToolBar")
        self._tab_bar_toolbar.setMovable(False)
        self._tab_bar_toolbar.setFloatable(False)
        self._tab_bar_toolbar.setContentsMargins(0, 0, 0, 0)
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._tab_bar_toolbar)

        # Viewport is the central widget
        self._viewport = Viewport()
        self._viewport.selection_changed.connect(self._on_selection_changed)
        self._viewport.translate_committed.connect(self._on_translate_committed)
        self._viewport.rotate_committed.connect(self._on_rotate_committed)
        self._viewport.scale_committed.connect(self._on_scale_committed)
        self._viewport.camera_changed.connect(self._update_camera_label)
        self._viewport.size_changed.connect(self._update_size_label)
        self.setCentralWidget(self._viewport)

        # Corner ownership and nesting must be set before any addDockWidget calls
        # so Qt builds the splitter tree with the correct structure from the start.
        self.setCorner(Qt.Corner.TopLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.setDockNestingEnabled(True)
        self.setAnimated(False)  # Qt bug: dock drag-to-tab animation crashes via null QVariantAnimation

        # --- Editor dock (left) — added first so left area owns the splitter root ---
        self._tabs = _DetachedTabBar()
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._tab_changed)
        self._tab_bar_toolbar.addWidget(self._tabs.tab_bar)

        self._editor_dock = QDockWidget("Editor", self)
        self._editor_dock.setObjectName("EditorDock")
        self._editor_dock.setWidget(self._tabs)
        self._editor_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._editor_dock)

        # --- Debugger dock (right, top) — added before bottom docks ---
        self._debugger_pane = DebuggerPane()
        self._debug_session: DebugSession | None = None
        self._debug_tab: FileTab | None = None
        self._debugger_dock = QDockWidget("Debugger", self)
        self._debugger_dock.setObjectName("DebuggerDock")
        self._debugger_dock.setWidget(self._debugger_pane)
        self._debugger_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._debugger_dock)
        self._debugger_dock.dockLocationChanged.connect(self._on_debugger_dock_location_changed)
        self._debugger_dock.topLevelChanged.connect(self._on_debugger_top_level_changed)
        self._debugger_pane.continue_requested.connect(self._on_debug_continue)
        self._debugger_pane.pause_requested.connect(self._on_debug_pause)
        self._debugger_pane.step_into_requested.connect(self._on_debug_step_into)
        self._debugger_pane.step_over_requested.connect(self._on_debug_step_over)
        self._debugger_pane.step_out_requested.connect(self._on_debug_step_out)
        self._debugger_pane.restart_requested.connect(self._on_debug_restart)
        self._debugger_pane.stop_requested.connect(self._on_debug_stop)
        self._debugger_pane.print_to_console.connect(self._on_debug_print)
        self._debugger_pane.print_value_to_console.connect(self._on_debug_print_value)
        self._debugger_pane.frame_selected.connect(self._on_debug_frame_selected)
        self._debugger_pane.set_splitter_orientation(self._current_debugger_splitter_orientation())

        for key, btn in (
            (Qt.Key.Key_F5, self._debugger_pane._btn_continue),
            (Qt.Key.Key_F10, self._debugger_pane._btn_step_over),
            (Qt.Key.Key_F11, self._debugger_pane._btn_step_into),
            (Qt.Modifier.SHIFT | Qt.Key.Key_F11, self._debugger_pane._btn_step_out),
            (Qt.Modifier.SHIFT | Qt.Modifier.META | Qt.Key.Key_F5, self._debugger_pane._btn_restart),
            (Qt.Modifier.SHIFT | Qt.Key.Key_F5, self._debugger_pane._btn_stop),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(btn.click)

        # --- Customizer dock (right, bottom — tabbed with Animate) ---
        self._customizer_pane = CustomizerPane()
        self._customizer_pane.source_changed.connect(self._on_customizer_source_changed)

        self._customizer_dock = QDockWidget("Customizer", self)
        self._customizer_dock.setObjectName("CustomizerDock")
        self._customizer_dock.setWidget(self._customizer_pane)
        self._customizer_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._customizer_dock)
        self.splitDockWidget(self._debugger_dock, self._customizer_dock, Qt.Orientation.Vertical)
        self._customizer_dock.hide()

        # --- Animate dock (right, bottom — tabbed with Customizer) ---
        self._animate_pane = AnimatePane()
        self._animate_pane.frame_changed.connect(self._on_animate_frame)
        self._animate_pane.dump_started.connect(self._on_dump_started, Qt.ConnectionType.QueuedConnection)
        self._animate_pane.dump_finished.connect(self._on_dump_finished)

        self._animate_dock = QDockWidget("Animate", self)
        self._animate_dock.setObjectName("AnimateDock")
        self._animate_dock.setWidget(self._animate_pane)
        self._animate_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.tabifyDockWidget(self._customizer_dock, self._animate_dock)
        self._animate_dock.hide()

        # --- Console dock (bottom) ---
        self._console = ConsoleWidget()
        self._console.setReadOnly(True)
        self._console.setFont(QFont("Menlo", 11))
        self._console.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._console.customContextMenuRequested.connect(self._console_context_menu)

        self._console_dock = QDockWidget("Console", self)
        self._console_dock.setObjectName("ConsoleDock")
        self._console_dock.setWidget(self._console)
        self._console_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._console_dock)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._vpt_label = QLabel("")
        self._vpt_label.setToolTip("Viewport Translate ($vpt)")
        self._status_bar.addWidget(self._vpt_label)

        self._vpr_label = QLabel("")
        self._vpr_label.setToolTip("Viewport Rotation ($vpr)")
        self._status_bar.addWidget(self._vpr_label)

        self._vpd_label = QLabel("")
        self._vpd_label.setToolTip("Viewport Distance ($vpd)")
        self._status_bar.addWidget(self._vpd_label)

        self._vpf_label = QLabel("")
        self._vpf_label.setToolTip("Viewport FOV ($vpf)")
        self._status_bar.addWidget(self._vpf_label)

        for _lbl, _var in (
            (self._vpt_label, "$vpt"),
            (self._vpr_label, "$vpr"),
            (self._vpd_label, "$vpd"),
            (self._vpf_label, "$vpf"),
        ):
            _lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            _lbl.customContextMenuRequested.connect(
                lambda pos, lbl=_lbl, var=_var: self._vp_label_context_menu(var, lbl, pos)
            )

        self._coord_label = QLabel("")
        self._status_bar.addWidget(self._coord_label)

        self._size_label = QLabel("")
        self._size_label.setToolTip("Viewport size (pixels)")
        self._status_bar.addPermanentWidget(self._size_label)

        self._fps_label = QLabel("")
        self._status_bar.addPermanentWidget(self._fps_label)
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps)
        self._fps_timer.start(1000)

    def _console_context_menu(self, pos):
        menu = self._console.createStandardContextMenu()
        name_value = self._console.value_at(pos)
        if name_value is not None:
            name, value = name_value
            from belfryscad.window.data_viewers import build_viewer_menu
            view_sub = QMenu(f"View '{name}'…", self._console)
            build_viewer_menu(view_sub, name, value, self._console)
            if not view_sub.isEmpty():
                menu.addSeparator()
                menu.addMenu(view_sub)
        menu.addSeparator()
        menu.addAction("Clear Console", self._console.clear)
        menu.exec(self._console.mapToGlobal(pos))

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

        tb.addSeparator()

        self._act_render = QAction(self._toolbar_icon("render"), "Render", self)
        self._act_render.setToolTip("Render (F6)")
        self._act_render.triggered.connect(self._render)
        tb.addAction(self._act_render)

        self._act_debug_tb = QAction(self._toolbar_icon("debug"), "Debug", self)
        self._act_debug_tb.setToolTip("Debug (Shift+F6)")
        self._act_debug_tb.triggered.connect(self._start_debug)
        tb.addAction(self._act_debug_tb)

        self._act_animate_tb = QAction(self._toolbar_icon("animate"), "Animate", self)
        self._act_animate_tb.setToolTip("Animate (F7)")
        self._act_animate_tb.setShortcut(QKeySequence(Qt.Key.Key_F7))
        self._act_animate_tb.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._act_animate_tb.triggered.connect(self._show_animate)
        tb.addAction(self._act_animate_tb)

        tb.addSeparator()

        self._tool_group = QButtonGroup(tb)
        self._tool_group.setExclusive(True)
        self._active_tool: int | None = None

        for tool_id, label, tooltip in (
            (0, "T", "Translate"),
            (1, "R", "Rotate"),
            (2, "S", "Scale"),
        ):
            btn = QToolButton()
            btn.setToolTip(tooltip)
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setFixedSize(28, 28)
            btn.setStyleSheet(
                "QToolButton { border: none; }"
                "QToolButton:checked { background: palette(highlight); border-radius: 4px; }"
            )
            icon_path = _ICONS_DIR / _TOOL_ICONS[tool_id]
            if icon_path.exists():
                btn.setIcon(QIcon(str(icon_path)))
                btn.setIconSize(QSize(22, 22))
            else:
                btn.setText(label)
                btn.setFont(QFont("Helvetica", 11, QFont.Weight.Bold))
            self._tool_group.addButton(btn, tool_id)
            tb.addWidget(btn)

        self._tool_group.idToggled.connect(self._on_tool_toggled)

        return tb

    def _on_tool_toggled(self, tool_id: int, checked: bool):
        self._active_tool = tool_id if checked else None
        self._viewport.set_active_tool(tool_id if checked else -1)

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
        design_menu.addSeparator()
        self._use_library_menu = design_menu.addMenu("Use Library")
        self._use_library_menu.aboutToShow.connect(self._populate_use_library_menu)
        self._add_action(design_menu, "Manage Libraries…", self._open_library_manager)

        # View
        view_menu = mb.addMenu("View")
        self._act_show_toolbar = self._add_checkable(view_menu, "Show Toolbar", True, self._toolbar.setVisible)
        self._act_show_tabs = self._add_checkable(view_menu, "Show Tab Bar", True, self._tab_bar_toolbar.setVisible)

        self._act_show_editor = self._editor_dock.toggleViewAction()
        self._act_show_editor.setText("Show Editor")
        view_menu.addAction(self._act_show_editor)

        self._act_show_console = self._console_dock.toggleViewAction()
        self._act_show_console.setText("Show Console")
        view_menu.addAction(self._act_show_console)

        self._act_show_debugger = self._debugger_dock.toggleViewAction()
        self._act_show_debugger.setText("Show Debugger")
        view_menu.addAction(self._act_show_debugger)

        self._act_show_animate = self._animate_dock.toggleViewAction()
        self._act_show_animate.setText("Show Animate")
        view_menu.addAction(self._act_show_animate)

        self._act_show_customizer = self._customizer_dock.toggleViewAction()
        self._act_show_customizer.setText("Show Customizer")
        view_menu.addAction(self._act_show_customizer)

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
        self._act_spin = self._add_checkable(view_menu, "Spin", False, self._toggle_spin)
        self._act_spin.setShortcut(QKeySequence("Ctrl+Meta+1"))
        view_menu.addSeparator()
        self._act_perspective = self._add_checkable(view_menu, "Perspective", True, self._toggle_perspective)
        self._act_perspective.setShortcut(QKeySequence("Ctrl+Meta+2"))
        self._act_stereo = self._add_checkable(view_menu, "Stereo (Cross-eye)", False, self._toggle_stereo)
        self._act_stereo.setShortcut(QKeySequence("Ctrl+Meta+3"))
        self._act_show_axes = self._add_checkable(view_menu, "Show Axes", True, self._toggle_axes)
        self._act_show_axes.setShortcut(QKeySequence("Ctrl+2"))
        self._act_show_edges = self._add_checkable(view_menu, "Show Edges", False, self._toggle_edges)
        self._act_show_edges.setShortcut(QKeySequence("Ctrl+1"))
        self._act_show_scale = self._add_checkable(view_menu, "Show Scale Markers", True, self._toggle_scale_markers)
        self._act_show_cross = self._add_checkable(view_menu, "Show Crosshairs", False, self._toggle_crosshairs)
        self._act_show_cross.setShortcut(QKeySequence("Ctrl+3"))
        self._act_show_status = self._add_checkable(view_menu, "Show Status Bar", True, self._status_bar.setVisible)
        view_menu.addSeparator()  # isolates macOS-injected "Enter Full Screen" (which has an icon) in its own section

        # Window
        window_menu = mb.addMenu("Window")
        self._add_action(window_menu, "Minimize", self.showMinimized, QKeySequence("Ctrl+M"))
        self._add_action(window_menu, "Zoom", self.showMaximized)
        window_menu.addSeparator()
        self._add_action(window_menu, "New Window", self._new_window, QKeySequence("Ctrl+Shift+N"))
        self._add_action(window_menu, "Open in New Window…", self._open_in_new_window)
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

        def shortcut(key, slot, app_wide=False):
            s = QShortcut(QKeySequence(key), self)
            if app_wide:
                s.setContext(Qt.ShortcutContext.ApplicationShortcut)
            s.activated.connect(slot)

        shortcut("Ctrl++", self._font_size_increase)
        shortcut("Ctrl+-", self._font_size_decrease)
        shortcut("Ctrl+[", lambda: self._zoom_viewport(-1))
        shortcut("Ctrl+]", lambda: self._zoom_viewport(1))
        shortcut("Shift+F6", self._start_debug)
        shortcut("F10", self._on_debug_step_over)
        shortcut("F11", self._on_debug_step_into)
        shortcut("F12", self._on_debug_step_out)

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def _new_document(self):
        tab = FileTab()
        tab.editor.document().contentsChanged.connect(
            lambda t=tab: self._on_editor_changed(t)
        )
        tab.editor.go_to_definition_requested.connect(
            lambda word, t=tab: self._go_to_definition(t, word)
        )
        tab.editor.print_to_console.connect(self._on_debug_print)
        tab.editor.print_value_to_console.connect(self._on_debug_print_value)
        tab.editor.breakpoints_changed.connect(self._on_breakpoints_changed)
        if hasattr(self, '_act_word_wrap'):
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

    def _current_tab(self) -> FileTab | None:
        return self._tabs.currentWidget()

    def _update_camera_label(self):
        import numpy as np
        cam = self._viewport._renderer.camera
        vpt = np.asarray(cam.target)
        vpr_x = ((90.0 - float(cam.elevation)) % 360.0 + 360.0) % 360.0
        vpr_z = ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0
        vpd = float(cam.distance)
        vpf = float(cam.fov)
        self._vpt_label.setText(f"  Viewport: translate = [{vpt[0]:.2f}, {vpt[1]:.2f}, {vpt[2]:.2f}]")
        self._vpr_label.setText(f"  rotate = [{vpr_x:.1f}, 0.0, {vpr_z:.1f}]")
        self._vpd_label.setText(f"  dist = {vpd:.1f}")
        self._vpf_label.setText(f"  FoV = {vpf:.1f}")

    def _vp_state_strings(self) -> dict:
        import numpy as np
        cam = self._viewport._renderer.camera
        vpt = np.asarray(cam.target)
        vpr_x = ((90.0 - float(cam.elevation)) % 360.0 + 360.0) % 360.0
        vpr_z = ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0
        return {
            "$vpt": f"$vpt = [{vpt[0]:.2f}, {vpt[1]:.2f}, {vpt[2]:.2f}]",
            "$vpr": f"$vpr = [{vpr_x:.1f}, 0.0, {vpr_z:.1f}]",
            "$vpd": f"$vpd = {float(cam.distance):.1f}",
            "$vpf": f"$vpf = {float(cam.fov):.1f}",
        }

    def _vp_label_context_menu(self, var: str, label: QLabel, pos):
        strings = self._vp_state_strings()
        full = "\n".join(f"{s};" for s in strings.values())
        menu = QMenu(self)
        menu.addAction(f"Copy {var}", lambda: QApplication.clipboard().setText(strings[var]))
        menu.addAction("Copy all $vp* values", lambda: QApplication.clipboard().setText(full))
        menu.exec(label.mapToGlobal(pos))

    def _update_size_label(self, _w: int, _h: int):
        w = self._viewport.width()
        h = self._viewport.height()
        self._size_label.setText(f"({w} × {h})  ")

    def _update_fps(self):
        count = self._viewport._frame_count
        self._viewport._frame_count = 0
        self._fps_label.setText(f"{count} FPS")

    def _tab_changed(self, index):
        tab = self._tabs.widget(index)
        if tab:
            self._customizer_pane.set_source(tab.editor.toPlainText())

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
            if self._debug_tab is tab:
                self._on_debug_stop()
            if self._rendered_tab is tab:
                self._rendered_tab = None
            if tab.file_path:
                get_document_manager().unregister(tab.file_path, tab.editor)
        self._tabs.removeTab(index)
        if self._tabs.count() == 0:
            self._new_document()

    def _close_current_tab(self):
        self._close_tab(self._tabs.currentIndex())

    def _tear_off_tab(self):
        tab = self._current_tab()
        if tab is None or self._tabs.count() <= 1:
            return
        file_path = tab.file_path
        text = tab.editor.toPlainText()
        is_modified = tab.is_modified
        self._close_tab(self._tabs.currentIndex())
        win = MainWindow()
        win.show()
        if file_path:
            win.open_file_by_path(file_path)
            if is_modified:
                win_tab = win._current_tab()
                if win_tab:
                    win_tab.editor.setPlainText(text)
                    win_tab.is_modified = True
                    win_tab._last_text = text
        else:
            win_tab = win._current_tab()
            if win_tab:
                win_tab.editor.setPlainText(text)
                win_tab._last_text = text
                if is_modified:
                    win_tab.is_modified = True

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
            if tab.file_path:
                get_document_manager().broadcast_change(tab.file_path, current, tab.editor)
        if tab is self._current_tab():
            self._customizer_pane.set_source(current)

    def _on_customizer_source_changed(self, new_source: str):
        tab = self._current_tab()
        if not tab or tab.editor.toPlainText() == new_source:
            return
        editor = tab.editor
        cursor_pos = editor.textCursor().position()
        editor.setPlainText(new_source)
        cursor = editor.textCursor()
        cursor.setPosition(min(cursor_pos, len(new_source)))
        editor.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def _create_and_add_tab(self, path: str, text: str) -> FileTab:
        """Create a fully-connected FileTab for an existing file and add it to the UI."""
        tab = FileTab()
        tab.file_path = path
        tab._last_text = text
        tab._last_cursor = 0
        tab._suppress_text_undo = False
        tab.editor.setPlainText(text)
        tab.is_modified = False
        tab.editor.document().contentsChanged.connect(
            lambda t=tab: self._on_editor_changed(t)
        )
        tab.editor.go_to_definition_requested.connect(
            lambda word, t=tab: self._go_to_definition(t, word)
        )
        tab.editor.print_to_console.connect(self._on_debug_print)
        tab.editor.print_value_to_console.connect(self._on_debug_print_value)
        tab.editor.breakpoints_changed.connect(self._on_breakpoints_changed)
        self._apply_preferences_to_tab(
            tab,
            QFont(load_preference("editor/fontFamily"), load_preference("editor/fontSize", int)),
            load_preference("editor/indentSize", int),
            load_preference("editor/showColumnGuide", bool),
            load_preference("editor/columnGuide", int),
        )
        self._apply_word_wrap_to_tab(tab)
        get_document_manager().register(path, tab.editor)
        # Replace a lone empty Untitled tab instead of adding alongside it
        if self._tabs.count() == 1:
            old = self._tabs.widget(0)
            if old and not old.file_path and not old.is_modified and not old.editor.toPlainText():
                self._tabs.removeTab(0)
        idx = self._tabs.addTab(tab, tab.display_name())
        self._tabs.setCurrentIndex(idx)
        return tab

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open File", "", "OpenSCAD Files (*.scad);;All Files (*)"
        )
        if not path:
            return
        self.open_file_by_path(path)

    def open_file_by_path(self, path: str):
        """Open a .scad file by path. If already open, switch to its tab."""
        resolved = str(Path(path).resolve())
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab and tab.file_path and str(Path(tab.file_path).resolve()) == resolved:
                self._tabs.setCurrentIndex(i)
                return
        # Use in-memory text if another window has unsaved changes to this file
        in_memory = get_document_manager().get_current_text(resolved)
        if in_memory is not None:
            text = in_memory
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                QMessageBox.critical(self, "Open Error", str(e))
                settings = QSettings("BelfrySCAD", "BelfrySCAD")
                recents = settings.value("recentFiles", [], type=list)
                if path in recents:
                    recents.remove(path)
                    settings.setValue("recentFiles", recents)
                    self._rebuild_recent_menu()
                return
        tab = self._create_and_add_tab(path, text)
        self._update_recent_files(path)
        self._render(tab)

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
        old_path = tab.file_path
        tab.file_path = path
        tab.is_modified = False
        idx = self._tabs.indexOf(tab)
        if idx >= 0:
            self._tabs.setTabText(idx, tab.display_name())
        if old_path and old_path != path:
            get_document_manager().unregister(old_path, tab.editor)
        get_document_manager().register(path, tab.editor)
        self._update_recent_files(path)
        self._render(tab)
        return True

    # ------------------------------------------------------------------
    # Recent files
    # ------------------------------------------------------------------

    _MAX_RECENT = 10

    def _update_recent_files(self, path: str):
        settings = QSettings("BelfrySCAD", "BelfrySCAD")
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
        settings = QSettings("BelfrySCAD", "BelfrySCAD")
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
        self.open_file_by_path(path)

    def _clear_recent_files(self):
        settings = QSettings("BelfrySCAD", "BelfrySCAD")
        settings.setValue("recentFiles", [])
        self._rebuild_recent_menu()

    def _export(self):
        if not self._bodies:
            self._render()
        bodies = self._bodies
        if not bodies:
            QMessageBox.warning(self, "Export", "No geometry to export. Render first.")
            return

        filters = "STL Files (*.stl);;OBJ Files (*.obj)"
        try:
            import lib3mf  # noqa: F401
            filters += ";;3MF Files (*.3mf)"
        except ImportError:
            pass
        path, _ = QFileDialog.getSaveFileName(
            self, "Export", "", filters
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

    def _viewport_params(self) -> dict:
        """Snapshot camera state and animation time as OpenSCAD $vp*/$t special variables."""
        params = {"$t": self._animate_pane.current_t()}
        try:
            cam = self._viewport._renderer.camera
            params.update({
                "$vpt": cam.target.tolist(),
                "$vpr": [
                    ((90.0 - float(cam.elevation)) % 360.0 + 360.0) % 360.0,
                    0.0,
                    ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0,
                ],
                "$vpd": float(cam.distance),
                "$vpf": float(cam.fov),
            })
        except Exception:
            pass
        return params

    def _apply_vp_params(self, vp: dict) -> bool:
        """Apply $vp* values from a script evaluation to the camera.

        Returns True if any camera value actually changed, False if the
        script's values matched the current camera state (no-op).
        """
        import math
        import numpy as np
        cam = self._viewport._renderer.camera
        changed = False

        if "$vpt" in vp:
            v = vp["$vpt"]
            if isinstance(v, (list, tuple)) and len(v) == 3:
                new_target = np.array([float(v[0]), float(v[1]), float(v[2])], dtype=np.float32)
                if not np.allclose(cam.target, new_target):
                    cam.target = new_target
                    changed = True

        if "$vpr" in vp:
            v = vp["$vpr"]
            if isinstance(v, (list, tuple)) and len(v) == 3:
                new_elev = (90.0 - float(v[0])) % 360.0
                new_az   = (float(v[2]) + 270.0) % 360.0
                if not math.isclose(cam.elevation, new_elev) or not math.isclose(cam.azimuth, new_az):
                    cam.elevation = new_elev
                    cam.azimuth   = new_az
                    changed = True

        if "$vpd" in vp:
            new_d = float(vp["$vpd"])
            if not math.isclose(cam.distance, new_d):
                cam.distance = max(0.1, new_d)
                changed = True

        if "$vpf" in vp:
            new_f = float(vp["$vpf"])
            if not math.isclose(cam.fov, new_f):
                cam.fov = max(1.0, min(120.0, new_f))
                changed = True

        if changed:
            self._viewport.camera_changed.emit()
            self._viewport.update()
        return changed

    def _render(self, tab=None):
        if not isinstance(tab, QWidget):
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
        self._console.clear()
        self._viewport.load_geometry([])
        self.log("Rendering…")

        cancel = threading.Event()
        self._render_cancel = cancel
        self._set_render_busy(True)

        worker = _RenderWorker(source, tab.file_path, cancel, self._viewport_params())
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

    def _on_animate_frame(self, t: float):
        if self._animate_pane.is_dumping():
            self._dump_frame = self._animate_pane.current_step()
        self._render()

    def _on_dump_started(self):
        if self._dump_dir is None:
            path = QFileDialog.getExistingDirectory(self, "Dump Animation Frames To")
            if not path:
                self._animate_pane.pause()
                return
            self._dump_dir = path
        self.log(f"Dumping animation frames to {self._dump_dir}")

    def _on_dump_finished(self):
        self.log("Animation frame dump complete.")

    def _set_render_busy(self, busy: bool):
        self._viewport.set_render_busy(busy)
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _on_render_done(self, file_tab, bodies, id_to_node, elapsed_ms: float, render_id: int, final_vp: dict | None = None):
        if render_id != self._render_id:
            return  # superseded by a later render; discard

        self._rendered_tab = file_tab
        self.id_to_node = id_to_node
        try:
            self._viewport.load_geometry(bodies)
        except Exception as e:
            import traceback
            self.log(f"GPU upload error: {e}\n{traceback.format_exc()}")
            return

        self._bodies = bodies

        # If the script set $vp* variables, apply them to the camera and skip auto-fit.
        script_moved_camera = bool(final_vp) and self._apply_vp_params(final_vp)

        try:
            import manifold3d as m3d
            import numpy as np
            all_bodies = [b.body for b in bodies if not b.body.is_empty()]
            if all_bodies:
                composed = m3d.Manifold.compose(all_bodies)
                bb = composed.bounding_box()
                bb_min = np.array([bb[0], bb[1], bb[2]], dtype=np.float32)
                bb_max = np.array([bb[3], bb[4], bb[5]], dtype=np.float32)
                # Skip auto-fit if the script explicitly positioned the camera,
                # or if animation playback is active.
                if not script_moved_camera and not self._animate_pane.is_playing():
                    self._viewport.frame_scene(bb_min, bb_max)
                self.log(
                    f"Render OK — bounds [{bb[0]:.2f},{bb[1]:.2f},{bb[2]:.2f}] to "
                    f"[{bb[3]:.2f},{bb[4]:.2f},{bb[5]:.2f}]  "
                    f"{_fmt_elapsed(elapsed_ms)}"
                )
        except Exception as e:
            import traceback
            self.log(f"Post-render error: {e}\n{traceback.format_exc()}")

    def _on_render_thread_done(self, file_tab):
        """Called once the render worker thread has fully finished.

        Dumping is paced from here (rather than from _on_render_done, which
        runs while the worker thread is still tearing down) so the next
        frame's worker thread never starts while the previous one is still
        touching the parser — see AnimatePane.play()/advance_frame().
        """
        if self._animate_pane.is_dumping() and self._dump_dir:
            try:
                frame = self._dump_frame
                image = self._viewport.grabFramebuffer()
                filename = f"frame{frame:04d}.png"
                image.save(str(Path(self._dump_dir) / filename))
                self.log(f"Dumped {filename}")
            except Exception as e:
                self.log(f"Frame dump error: {e}")
            self._animate_pane.advance_frame()

    def _flush_caches(self):
        """Discard each tab's pre-calculated AST scope/node table and the parser's AST cache."""
        from openscad_lalr_parser import clear_ast_cache
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab:
                tab.root_scope = None
        self.id_to_node = {}
        clear_ast_cache()
        self.log("Flushed AST caches — render or debug to rebuild.")

    def _open_library_manager(self):
        from belfryscad.window.library_manager import LibraryManagerWindow
        if not hasattr(self, '_library_manager') or self._library_manager is None:
            self._library_manager = LibraryManagerWindow(parent=self)
            self._library_manager.destroyed.connect(lambda: setattr(self, '_library_manager', None))
        self._library_manager.show()
        self._library_manager.raise_()
        self._library_manager.activateWindow()

    def _populate_use_library_menu(self):
        from belfryscad.window.library_manager import _library_dir, _load_catalog
        menu = self._use_library_menu
        menu.clear()
        lib_dir = _library_dir()
        catalog = _load_catalog()
        found = False
        for lib in catalog:
            install_as = lib.get("install_as", lib["name"])
            if (lib_dir / install_as).is_dir():
                stmt = lib.get("include_statement", f"use <{install_as}/{install_as}.scad>")
                act = menu.addAction(lib["name"])
                act.triggered.connect(lambda checked=False, s=stmt: self._insert_use_statement(s))
                found = True
        if not found:
            act = menu.addAction("(No libraries installed)")
            act.setEnabled(False)

    def _insert_use_statement(self, statement: str):
        tab = self._current_tab()
        if not tab:
            return
        import re
        editor = tab.editor
        text = editor.toPlainText()
        lines = text.split("\n")
        insert_line = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r"^(use|include)\s+<", stripped):
                insert_line = i + 1
            elif stripped and not stripped.startswith("//"):
                break
        cursor = editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        for _ in range(insert_line):
            cursor.movePosition(cursor.MoveOperation.Down)
        cursor.insertText(statement + "\n")

    def _parse_error_to_editor(self, tab, captured: str):
        """Parse the error text from the parser and mark the editor."""
        import re
        m = re.search(r"at line (\d+), column (\d+)", captured)
        if m:
            line, col = int(m.group(1)), int(m.group(2))
            tab.editor.set_error_location(line, col)

    def log(self, text: str):
        self._console.append_output(text)

    def log_to_tab(self, tab, text: str):
        self._console.append_output(text)

    def log_value_to_tab(self, tab, name: str, value: object):
        self._console.append_value(name, value, _pretty_assignment(name, value))

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
            e._indent_lines()

    def _undent(self):
        if e := self._current_editor():
            e._unindent_lines()

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
        lines = text.split(" ")  # Qt paragraph separator
        if add:
            lines = ["// " + l for l in lines]
        else:
            lines = [l[3:] if l.startswith("// ") else l for l in lines]
        cursor.insertText(" ".join(lines))

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
            self.log(f"Go to Definition: no AST available (render or debug first)")
            return

        node = (scope.lookup_variable(word)
                or scope.lookup_function(word)
                or scope.lookup_module(word))

        if node is None:
            self.log(f"Go to Definition: no definition found for '{word}'")
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
                        self.log(f"Go to Definition: cannot open '{def_file}': {e}")
                        return
                    target_tab = self._create_and_add_tab(def_file, text)

        target_tab.editor.scroll_to_line(def_line)
        idx = self._tabs.indexOf(target_tab)
        if idx >= 0:
            self._tabs.setCurrentIndex(idx)

    def _current_editor(self):
        tab = self._current_tab()
        return tab.editor if tab else None

    # ------------------------------------------------------------------
    # View operations
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Debug session
    # ------------------------------------------------------------------

    def _show_animate(self):
        self._animate_dock.show()
        self._animate_dock.raise_()
        self._animate_pane._fps_edit.setFocus()
        self._animate_pane._fps_edit.selectAll()

    def _collect_breakpoints(self) -> dict[str, set[int]]:
        """Breakpoints from all open tabs (per-file, 1-indexed)."""
        breakpoints: dict[str, set[int]] = {}
        for i in range(self._tabs.count()):
            t = self._tabs.widget(i)
            if t and t.file_path and t.editor._breakpoints:
                bp_set = {bn + 1 for bn in t.editor._breakpoints}
                breakpoints[str(Path(t.file_path).resolve())] = bp_set
        return breakpoints

    def _on_breakpoints_changed(self):
        """A breakpoint was toggled in some tab's editor gutter. If a debug
        session is currently running/paused, push the updated breakpoint
        set to it immediately — otherwise a newly-added breakpoint would
        silently have no effect until the session is restarted."""
        if self._debug_session is not None and self._debug_session.is_running():
            self._debug_session.set_breakpoints(self._collect_breakpoints())

    def _start_debug(self):
        tab = self._current_tab()
        if not tab:
            return
        # While paused, Shift+F6 acts as Continue
        if self._debug_session and self._debug_session.is_running():
            self._on_debug_continue()
            return
        # Stop any existing session before starting a new one
        if self._debug_session:
            self._set_debug_busy(False)
            self._debug_session.paused.disconnect()
            self._debug_session.error_break.disconnect()
            self._debug_session.finished.disconnect()
            self._debug_session.errored.disconnect()
            self._debug_session.logged.disconnect()
            self._debug_session.logged_value.disconnect()
            self._debug_session.stop()
            self._debug_session = None

        source = tab.editor.toPlainText()
        if not source.strip():
            return

        self._console.clear()

        from openscad_lalr_parser import getASTfromFile
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

        breakpoints = self._collect_breakpoints()

        # Show the debugger dock and bring it to the front
        self._debugger_dock.show()
        self._debugger_dock.raise_()

        self._debug_tab = tab
        self._debug_session = DebugSession(self)
        self._debug_session.paused.connect(
            lambda origin, line, frames, stk, pbodies, perr: self._on_debug_paused(
                origin, line, frames, stk, pbodies, perr)
        )
        self._debug_session.error_break.connect(
            lambda origin, line, msg, frames, stk, pbodies, perr: self._on_debug_error_break(
                origin, line, msg, frames, stk, pbodies, perr)
        )
        self._debug_session.finished.connect(
            lambda bodies, id2node: self._on_debug_finished(bodies, id2node)
        )
        self._debug_session.errored.connect(self._on_debug_error)
        self._debug_session.logged.connect(self._on_debug_print)
        self._debug_session.logged_value.connect(self._on_debug_print_value)

        self._debugger_pane.set_running()
        self._viewport.load_geometry([])
        self._set_debug_busy(True)
        self._debug_session.start(nodes, root_scope, breakpoints,
                                self._viewport_params(),
                                current_file=current_file)

    def _find_or_open_tab(self, file_path: str):
        """Return the tab for *file_path*, opening it in a new tab if needed."""
        resolved = str(Path(file_path).resolve())
        for i in range(self._tabs.count()):
            t = self._tabs.widget(i)
            if t and t.file_path and str(Path(t.file_path).resolve()) == resolved:
                return t, i
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None, -1
        new_tab = self._create_and_add_tab(file_path, text)
        idx = self._tabs.indexOf(new_tab)
        return new_tab, idx

    def _clear_all_execution_lines(self):
        for i in range(self._tabs.count()):
            t = self._tabs.widget(i)
            if t:
                t.editor.clear_execution_line()

    def _show_debug_line(self, origin: str, line: int):
        """Switch to the correct editor tab for *origin* and highlight *line*."""
        self._clear_all_execution_lines()
        tab = self._debug_tab
        if tab is None:
            return
        current_file = tab.file_path
        if not origin or not current_file or str(Path(origin).resolve()) == str(Path(current_file).resolve()):
            tab.editor.set_execution_line(line)
            self._tabs.setCurrentWidget(tab)
        else:
            target_tab, idx = self._find_or_open_tab(origin)
            if target_tab is not None:
                target_tab.editor.set_execution_line(line)
                self._tabs.setCurrentIndex(idx)

    def _on_debug_paused(self, origin: str, line: int, all_frame_locals: list, call_stack: list,
                        partial_bodies=None, partial_error: str | None = None):
        if not self._debug_tab:
            return
        self._set_debug_busy(False)
        if partial_bodies is not None:
            self._viewport.load_geometry(partial_bodies)
        self._debugger_pane.set_paused(line, all_frame_locals, call_stack, origin=origin,
                                       partial_error=partial_error)
        innermost = all_frame_locals[0] if all_frame_locals else {}
        locals_dict = {**innermost.get("outer_scope", {}), **innermost.get("local_scope", {})}
        self._apply_vp_params({k: locals_dict[k] for k in ("$vpt", "$vpr", "$vpd", "$vpf") if k in locals_dict})
        self._show_debug_line(origin, line)
        self._set_debug_locals_on_visible(locals_dict)

    def _on_debug_error_break(self, origin: str, line: int, msg: str, all_frame_locals: list, call_stack: list,
                              partial_bodies=None, partial_error: str | None = None):
        if not self._debug_tab:
            return
        self._set_debug_busy(False)
        if partial_bodies is not None:
            self._viewport.load_geometry(partial_bodies)
        self._debugger_pane.set_error_break(line, msg, all_frame_locals, call_stack, origin=origin,
                                            partial_error=partial_error)
        innermost = all_frame_locals[0] if all_frame_locals else {}
        locals_dict = {**innermost.get("outer_scope", {}), **innermost.get("local_scope", {})}
        self._show_debug_line(origin, line)
        self._set_debug_locals_on_visible(locals_dict)

    def _clear_all_debug_locals(self):
        for i in range(self._tabs.count()):
            t = self._tabs.widget(i)
            if t:
                t.editor.set_debug_locals(None)

    def _set_debug_locals_on_visible(self, locals_dict: dict):
        """Clear debug locals from all editors, set them on the currently visible tab."""
        self._clear_all_debug_locals()
        visible = self._current_tab()
        if visible:
            visible.editor.set_debug_locals(locals_dict)
        self._viewport.set_debug_paused(True)

    def _on_debug_finished(self, bodies, id_to_node):
        from belfryscad.engine.evaluator import to_renderable_bodies

        tab = self._debug_tab
        if not tab:
            return
        self._set_debug_busy(False)
        self.id_to_node = id_to_node
        self._rendered_tab = tab
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_idle()
        self._debug_session = None
        self._debug_tab = None
        self._tabs.setCurrentWidget(tab)
        if not bodies:
            self.log("Debug: no geometry produced.")
            return

        bodies = to_renderable_bodies(bodies)
        try:
            self._viewport.load_geometry(bodies)
        except Exception as e:
            import traceback
            self.log(f"GPU upload error: {e}\n{traceback.format_exc()}")
            return
        self._bodies = bodies
        try:
            import manifold3d as m3d
            import numpy as np
            all_bodies = [b.body for b in bodies if not b.body.is_empty()]
            if all_bodies:
                composed = m3d.Manifold.compose(all_bodies)
                bb = composed.bounding_box()
                bb_min = np.array([bb[0], bb[1], bb[2]], dtype=np.float32)
                bb_max = np.array([bb[3], bb[4], bb[5]], dtype=np.float32)
                self._viewport.frame_scene(bb_min, bb_max)
                self.log("Debug: completed.")
        except Exception:
            pass

    def _on_debug_error(self, msg: str):
        error_tab = self._debug_tab
        self._set_debug_busy(False)
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_idle()
        self._debug_session = None
        self._debug_tab = None
        if error_tab is not None:
            self._tabs.setCurrentWidget(error_tab)
        self.log(f"Debug error:\n{msg}")

    def _set_debug_busy(self, busy: bool):
        self._viewport.set_debug_busy(busy)

    def _on_debug_continue(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_running()
        self._set_debug_busy(True)
        self._debug_session.resume("continue", mods)

    def _on_debug_pause(self):
        if not self._debug_session:
            return
        self._debug_session.pause()

    def _on_debug_step_into(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_running()
        self._set_debug_busy(True)
        self._debug_session.resume("step_into", mods)

    def _on_debug_step_over(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_running()
        self._set_debug_busy(True)
        self._debug_session.resume("step_over", mods)

    def _on_debug_step_out(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_running()
        self._set_debug_busy(True)
        self._debug_session.resume("step_out", mods)

    def _on_debug_restart(self):
        restart_tab = self._debug_tab
        self._set_debug_busy(False)
        self._clear_all_debug_locals()
        if self._debug_session:
            self._debug_session.paused.disconnect()
            self._debug_session.error_break.disconnect()
            self._debug_session.finished.disconnect()
            self._debug_session.errored.disconnect()
            self._debug_session.logged.disconnect()
            self._debug_session.logged_value.disconnect()
            self._debug_session.stop()
            self._debug_session = None
        self._clear_all_execution_lines()
        self._debug_tab = None
        if restart_tab is not None:
            self._tabs.setCurrentWidget(restart_tab)
        self._start_debug()

    def _on_debug_stop(self):
        if not self._debug_session:
            return
        stop_tab = self._debug_tab
        self._set_debug_busy(False)
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debug_session.paused.disconnect()
        self._debug_session.error_break.disconnect()
        self._debug_session.finished.disconnect()
        self._debug_session.errored.disconnect()
        self._debug_session.logged.disconnect()
        self._debug_session.logged_value.disconnect()
        self._debug_session.stop()
        self._debug_session = None
        self._debug_tab = None
        self._debugger_pane.set_idle()
        if stop_tab is not None:
            self._tabs.setCurrentWidget(stop_tab)

    def _on_debug_print(self, text: str):
        self._console.append_output(text)

    def _on_debug_print_value(self, name: str, value: object):
        self._console.append_value(name, value, _pretty_assignment(name, value))

    def _on_debug_frame_selected(self, file_path: str, line: int):
        if not file_path or not line:
            return
        self._clear_all_execution_lines()
        target_tab, idx = self._find_or_open_tab(file_path)
        if target_tab is not None:
            target_tab.editor.set_execution_line(line)
            self._tabs.setCurrentIndex(idx)

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
        viewer_ipd = load_preference("viewport/viewerIPD", float)
        viewer_screen_dist = load_preference("viewport/viewerScreenDist", float)
        stereo_depth_scale = load_preference("viewport/stereoDepthScale", float)
        font = QFont(family, size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab:
                self._apply_preferences_to_tab(tab, font, indent, show_guide, guide_col)
        # Data-viewer dialogs (VNF/Path/Grid) each own a real Viewport/camera
        # too, so their stereo settings should track preference changes the
        # same way the main window's does, not just at dialog-open time.
        from PySide6.QtWidgets import QApplication
        viewports = [self._viewport] + [
            w._vp for w in QApplication.topLevelWidgets() if hasattr(w, '_vp')
        ]
        for vp in viewports:
            cam = vp._renderer.camera
            cam.viewer_ipd = viewer_ipd
            cam.viewer_screen_dist = viewer_screen_dist
            cam.stereo_depth_scale = stereo_depth_scale
            cam.screen_dpi = vp.screen().physicalDotsPerInch()
            if cam.stereo:
                vp.update()

    @staticmethod
    def _apply_preferences_to_tab(tab, font: QFont, indent: int, show_guide: bool, guide_col: int):
        tab.editor.setFont(font)
        tab.editor.set_indent_size(indent)
        tab.editor._column_guide.set_column(guide_col)
        tab.editor._column_guide.setVisible(show_guide)

    def _restore_settings(self):
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        geometry = s.value("windowGeometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = s.value("windowState")
        layout_version = s.value("layoutVersion", 0, type=int)
        if state is not None and layout_version == self._LAYOUT_VERSION:
            self._first_show = False
            self.restoreState(state)
        perspective = s.value("perspective", True, type=bool)
        self._act_perspective.blockSignals(True)
        self._act_perspective.setChecked(perspective)
        self._act_perspective.blockSignals(False)
        self._toggle_perspective(perspective)
        stereo = s.value("stereo", False, type=bool)
        self._act_stereo.blockSignals(True)
        self._act_stereo.setChecked(stereo)
        self._act_stereo.blockSignals(False)
        self._toggle_stereo(stereo)
        word_wrap = s.value("wordWrap", False, type=bool)
        self._act_word_wrap.blockSignals(True)
        self._act_word_wrap.setChecked(word_wrap)
        self._act_word_wrap.blockSignals(False)
        self._toggle_word_wrap(word_wrap)
        self._apply_preferences()

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            QTimer.singleShot(0, self._set_default_layout)

    def _set_default_layout(self):
        w = self.width()
        h = self.height()
        bottom_h = max(180, h // 4)
        right_w = max(250, w // 4)
        # editor dock: left ~40% of window width
        self.resizeDocks([self._editor_dock], [max(300, w * 2 // 5)], Qt.Orientation.Horizontal)
        # right dock: ~25% of window width
        self.resizeDocks([self._debugger_dock], [right_w], Qt.Orientation.Horizontal)
        # bottom dock area: ~25% of window height
        self.resizeDocks([self._console_dock], [bottom_h], Qt.Orientation.Vertical)
        # right dock: debugger top ~60%, customizer/animate bottom ~40%
        self.resizeDocks([self._debugger_dock, self._customizer_dock],
                         [max(200, (h - bottom_h) * 3 // 5),
                          max(150, (h - bottom_h) * 2 // 5)],
                         Qt.Orientation.Vertical)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._render_cancel is not None:
            self._render_cancel.set()
            self._set_render_busy(False)
            self.log("Render cancelled.")
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # Prompt to save any modified tabs before quitting
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab and tab.is_modified:
                reply = QMessageBox.question(
                    self, "Unsaved Changes",
                    f"Save changes to {tab.display_name()}?",
                    QMessageBox.StandardButton.Save |
                    QMessageBox.StandardButton.Discard |
                    QMessageBox.StandardButton.Cancel,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    event.ignore()
                    return
                if reply == QMessageBox.StandardButton.Save:
                    self._tabs.setCurrentIndex(i)
                    if not self._save_file():
                        event.ignore()
                        return
        # Stop animation playback (no more renders get queued) and let any
        # in-flight render thread finish — Qt aborts if a QThread is
        # destroyed while still running.
        self._animate_pane.pause()
        if self._render_cancel is not None:
            self._render_cancel.set()
        deadline = time.monotonic() + 5.0
        while any(t.isRunning() for _, _, t in self._render_jobs) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)

        s = QSettings("BelfrySCAD", "BelfrySCAD")
        s.setValue("windowGeometry", self.saveGeometry())
        s.setValue("windowState", self.saveState())
        s.setValue("layoutVersion", self._LAYOUT_VERSION)
        s.setValue("perspective", self._act_perspective.isChecked())
        s.setValue("stereo", self._act_stereo.isChecked())
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
        self._bodies = []
        self._viewport.load_geometry([])
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().endswith('.scad'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.endswith('.scad'):
                self.open_file_by_path(path)
        event.acceptProposedAction()

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
        self._debugger_pane.set_splitter_orientation(orientation)

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

    @staticmethod
    def _active_viewer_viewport():
        """If a data-viewer dialog is the active window, return its viewport."""
        from PySide6.QtWidgets import QApplication
        active = QApplication.activeWindow()
        if active is not None and hasattr(active, '_vp'):
            return active._vp
        return None

    def _target_viewport(self):
        """Whichever viewport a View-menu toggle/shortcut should affect: the
        active data-viewer dialog's, if one is focused, else the main
        window's. Every viewport (main window and data viewers alike) is a
        `Viewport` wrapping a `SceneRenderer`, so callers can always reach
        camera/display state via `vp._renderer....` regardless of which one
        this returns."""
        return self._active_viewer_viewport() or self._viewport

    def _toggle_spin(self, enabled: bool):
        self._target_viewport().set_spinning(enabled)

    def _toggle_perspective(self, perspective: bool):
        vp = self._target_viewport()
        vp._renderer.camera.orthographic = not perspective
        vp.update()

    def _toggle_stereo(self, enabled: bool):
        vp = self._target_viewport()
        vp._renderer.camera.stereo = enabled
        vp.update()

    def _toggle_axes(self, visible):
        vp = self._target_viewport()
        vp._renderer.show_axes = visible
        vp.update()

    def _toggle_edges(self, visible):
        vp = self._target_viewport()
        vp._renderer.show_edges = visible
        vp.update()

    def _toggle_scale_markers(self, visible):
        vp = self._target_viewport()
        vp._renderer.show_scale_markers = visible
        vp.update()

    def _toggle_crosshairs(self, visible):
        vp = self._target_viewport()
        vp._renderer.show_crosshairs = visible
        vp.update()

    def _set_view(self, preset):
        self._target_viewport().set_view_preset(preset)

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
        self._viewport.zoom(direction)

    # ------------------------------------------------------------------
    # Window
    # ------------------------------------------------------------------

    def _bring_all_to_front(self):
        self.raise_()

    def _new_window(self):
        win = MainWindow()
        win.show()

    def _open_in_new_window(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open in New Window", "", "OpenSCAD Files (*.scad);;All Files (*)"
        )
        if path:
            win = MainWindow()
            win.show()
            win.open_file_by_path(path)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_selection_changed(self, orig_id: int):
        rendered = self._rendered_tab
        if rendered is None:
            return
        if orig_id < 0:
            rendered.editor.clear_selection()
            return
        node = self.id_to_node.get(orig_id)
        if node is None:
            rendered.editor.clear_selection()
            return
        rendered.editor.set_selection(node.position.start_offset, node.position.end_offset)

    # ------------------------------------------------------------------
    # Translate gizmo commit
    # ------------------------------------------------------------------

    def _on_translate_committed(self, dx: float, dy: float, dz: float):
        if not self._rendered_tab:
            return
        orig_id = self._viewport._renderer.selected_id
        if orig_id is None:
            return
        node = self.id_to_node.get(orig_id)
        if node is None:
            return

        # Switch to rendered tab if it's not the current editor
        if self._current_tab() is not self._rendered_tab:
            idx = self._tabs.indexOf(self._rendered_tab)
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)

        source = self._rendered_tab.editor.toPlainText()
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
            self._rendered_tab, self._rendered_tab.editor, source, new_source, self._render,
            new_node_start, self._restore_selection_after_translate,
            merge_id=1001, label="Translate", viewport=self._viewport,
        )
        self._undo_stack.push(cmd)

    def _restore_selection_after_translate(self, new_node_start: int):
        for orig_id, node in self.id_to_node.items():
            if node.position.start_offset == new_node_start:
                self._viewport._renderer.selected_id = orig_id
                if self._rendered_tab:
                    self._rendered_tab.editor.set_selection(node.position.start_offset, node.position.end_offset)
                self._viewport.update()
                return
        self._viewport._renderer.selected_id = None
        if self._rendered_tab:
            self._rendered_tab.editor.clear_selection()
        self._viewport.update()

    # ------------------------------------------------------------------
    # Rotate gizmo commit
    # ------------------------------------------------------------------

    def _on_rotate_committed(self, axis: int, angle_deg: float):
        if not self._rendered_tab:
            return
        orig_id = self._viewport._renderer.selected_id
        if orig_id is None:
            return
        node = self.id_to_node.get(orig_id)
        if node is None:
            return

        if self._current_tab() is not self._rendered_tab:
            idx = self._tabs.indexOf(self._rendered_tab)
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)

        source = self._rendered_tab.editor.toPlainText()
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
            self._rendered_tab, self._rendered_tab.editor, source, new_source, self._render,
            new_node_start, self._restore_selection_after_translate,
            merge_id=1002, label="Rotate", viewport=self._viewport,
        )
        self._undo_stack.push(cmd)

    # ------------------------------------------------------------------
    # Scale gizmo commit
    # ------------------------------------------------------------------

    def _on_scale_committed(self, axis: int, factor: float, uniform: bool):
        if not self._rendered_tab:
            return
        orig_id = self._viewport._renderer.selected_id
        if orig_id is None:
            return
        node = self.id_to_node.get(orig_id)
        if node is None:
            return

        if self._current_tab() is not self._rendered_tab:
            idx = self._tabs.indexOf(self._rendered_tab)
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)

        source = self._rendered_tab.editor.toPlainText()
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
            self._rendered_tab, self._rendered_tab.editor, source, new_source, self._render,
            new_node_start, self._restore_selection_after_translate,
            merge_id=1003, label="Scale", viewport=self._viewport,
        )
        self._undo_stack.push(cmd)

    # ------------------------------------------------------------------
    # Coordinate display
    # ------------------------------------------------------------------

    def show_clicked_coords(self, x, y, z):
        self._coord_label.setText(f"x: {x:.3f}  y: {y:.3f}  z: {z:.3f}")
