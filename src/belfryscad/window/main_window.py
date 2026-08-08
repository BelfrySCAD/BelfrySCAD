from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QTabBar, QStackedWidget, QPlainTextEdit, QToolBar, QStatusBar,
    QLabel, QMessageBox, QFileDialog, QToolButton, QButtonGroup,
    QDockWidget, QApplication, QMenu, QDialog,
)
from PySide6.QtGui import QAction, QKeySequence, QFont, QIcon, QShortcut, QUndoCommand, QTextCursor
from PySide6.QtCore import Qt, QSize, QSettings, QThread, QObject, QTimer, Signal, Slot
import threading
import time

from belfryscad import exporters
from belfryscad.window.editor import CodeEditor
from belfryscad.window.console import ConsoleWidget
from belfryscad.window.viewport import Viewport
from belfryscad.window.debugger import DebuggerPane, DebugSession, _pretty_assignment
from belfryscad.window.animate import AnimatePane
from belfryscad.window.customizer import CustomizerPane
from belfryscad.window.ai_chat import AIChatPane
from belfryscad.window.preferences import PreferencesDialog, load_preference
from belfryscad.window.color_themes import COLOR_THEMES, DEFAULT_COLOR_THEME, all_themes
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

    # Stable per-tab id handed to the AI chat pane's tools. A real counter,
    # not id(self): CPython reuses object ids after a tab is closed, so a
    # stale tool-call reference could otherwise silently hit a different
    # tab. In-memory only -- chat sessions don't outlive the app.
    _next_chat_id = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = CodeEditor()
        layout.addWidget(self.editor)
        self.chat_id = FileTab._next_chat_id
        FileTab._next_chat_id += 1
        self.file_path = None
        # A name for a tab that isn't on disk yet -- the AI's
        # propose_new_script supplies one, and without this the tab it
        # created read as "Untitled" and its filename argument did nothing.
        self.suggested_name = None
        self.is_modified = False
        self.root_scope = None
        # The temp file most recently parsed for this tab's root_scope --
        # _RenderWorker always parses a temp copy of the live buffer (see
        # its own doc comment), so AST node .position.origin values for
        # this tab's own top-level code point here, not at file_path. Kept
        # around so _go_to_definition can recognize "this tab" either way.
        self._last_parse_path = None
        self._last_text = ""
        self._last_cursor = 0
        self._suppress_text_undo = False

    def display_name(self):
        if self.file_path:
            import os
            name = os.path.basename(self.file_path)
        else:
            name = self.suggested_name or "Untitled"
        name += "*" if self.is_modified else ""
        name += " (ro)" if self.editor.isReadOnly() else ""
        return name


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
    def on_tmp_path(self, path: str):
        if self._render_id == self._mw._render_id:
            self._mw._path_labels[path] = self._file_tab.display_name()

    @Slot(str)
    def on_parse_errored(self, captured: str):
        if self._render_id == self._mw._render_id:
            self._mw._console.append_output(captured.rstrip())
            self._mw._parse_error_to_editor(self._file_tab, captured)

    @Slot(object, object, str)
    def on_ast_ready(self, nodes, root_scope, parse_path: str):
        if self._render_id == self._mw._render_id:
            self._file_tab.root_scope = root_scope
            self._file_tab._last_parse_path = parse_path
            self._file_tab.editor.update_user_names(root_scope)

    @Slot(object, object, float, object, object, object)
    def on_finished(self, bodies, id_to_node, elapsed_ms: float, final_vp: dict, csg_tree: list, profile_result):
        self._mw._on_render_done(self._file_tab, bodies, id_to_node, elapsed_ms, self._render_id, final_vp, csg_tree,
                                 profile_result=profile_result)

    @Slot()
    def on_done(self):
        self._mw._set_render_busy(False)
        if self._render_id == self._mw._render_id:
            self._mw._on_render_thread_done(self._file_tab)


class _RenderWorker(QObject):
    """Runs parse + evaluate in a background thread. All signals are queued to the main thread."""
    logged = Signal(str)
    parse_errored = Signal(str)          # captured stdout; triggers editor error marking
    tmp_path_ready = Signal(str)         # temp .scad holding the live buffer, for label mapping
    ast_ready = Signal(object, object, str)   # (nodes, root_scope, parse_path) — emitted after successful parse
    finished = Signal(object, object, float, object, object, object)  # (bodies, id_to_node, elapsed_ms, final_vp, csg_tree, profile_result)
    done = Signal()                      # always emitted at end of run(), for thread cleanup

    def __init__(self, source: str, file_path, cancel: threading.Event, viewport_params: dict | None = None,
                 manifold_cache=None, profile: bool = False):
        super().__init__()
        self._source = source
        self._file_path = file_path
        self._cancel = cancel
        self._viewport_params = viewport_params or {}
        self._manifold_cache = manifold_cache
        self._profile = profile
        self._tmp_path = None  # temp .scad for an unsaved buffer; unlinked in run()

    @Slot()
    def run(self):
        try:
            self._do_render()
        finally:
            # The C++ evaluator reads the file at eval time, so the temp
            # file _do_render always writes the live buffer to must outlive
            # it -- clean it up here.
            if self._tmp_path:
                import os as _os
                try:
                    _os.unlink(self._tmp_path)
                except OSError:
                    pass
                self._tmp_path = None
            self.done.emit()

    def _do_render(self):
        import time as _time, tempfile, traceback
        from openscad_cpp_evaluator import Evaluator, EvalError, ParseError, parse as _oce_parse, to_renderable_bodies

        _t0 = _time.perf_counter()

        # Always render *self._source* (the live editor buffer captured by
        # _render() at trigger time), never self._file_path's on-disk
        # content directly -- F6/Render must reflect unsaved edits, not
        # just whatever was last saved. A temp .scad is written either way
        # (the C++ parser/evaluator are both path-based) and unlinked in
        # run()'s finally. When the tab has a real file_path, the temp file
        # goes in *that* file's own directory so relative use/include
        # statements still resolve (same convention as
        # belfryscad.headless's CLI -D handling, _prepare_source).
        tmp_dir = str(Path(self._file_path).parent) if self._file_path else None
        _tmp = tempfile.NamedTemporaryFile(
            suffix=".scad", mode="w", encoding="utf-8", delete=False, dir=tmp_dir
        )
        _tmp.write(self._source)
        _tmp.close()
        parse_path = self._tmp_path = _tmp.name
        # Every position the evaluator reports (errors, warnings, profile
        # call sites) names this temp file rather than the tab, since that
        # is genuinely what it parsed. Announce it so the UI can show the
        # tab's name instead of a tmpXXXX.scad nobody recognises.
        self.tmp_path_ready.emit(parse_path)

        # --- Parse (C++) ---
        try:
            root_scope = _oce_parse(parse_path)
        except ParseError as e:
            self.parse_errored.emit(str(e))
            return
        except Exception as e:
            self.logged.emit(f"Parse error: {e}")
            return

        self.ast_ready.emit(None, root_scope, parse_path)

        if self._cancel.is_set():
            return

        # --- Evaluate ---
        evaluator = Evaluator(echo_fn=self.logged.emit, manifold_cache=self._manifold_cache, profile=self._profile)
        try:
            bodies, id_to_node = evaluator.evaluate(parse_path, self._viewport_params)
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
        for k in ("$vpt", "$vpr", "$vpd", "$vpf"):
            # Only apply values the script itself assigned -- dyn also
            # carries $vp* pre-seeded from the *current* camera (see
            # _viewport_params/viewport_params), which would otherwise
            # get reapplied as a no-op-that-isn't: if the user manually
            # orbits/pans/zooms while this render is in flight, that
            # seeded value is stale by the time it comes back here.
            if k in evaluator.dyn_explicit:
                v = evaluator.dyn[k]
                final_vp[k] = v.tolist() if hasattr(v, "tolist") else v
        self.finished.emit(bodies, id_to_node, elapsed_ms, final_vp, evaluator.csg_tree, evaluator.profile_result)


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
    _LAYOUT_VERSION = 5

    # Debounce interval for Customizer-triggered auto-render -- see
    # _on_customizer_source_changed.
    _CUSTOMIZER_RENDER_DELAY_MS = 2000

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
        self._last_csg_tree: list | None = None  # resolved+generated CSGNode tree from the last successful render, for "Dump CSG Tree to Console"
        self._last_profile_result = None  # ProfileResult from the last "Render with Profiling" run, for "Show Profile Report…"
        # Maps a path the evaluator reports back to a friendlier label --
        # currently the temp .scad each render writes the live buffer to,
        # shown as its tab's name. Keyed by path, so stale entries from
        # earlier renders are harmless (those files no longer exist).
        self._path_labels: dict[str, str] = {}
        from openscad_cpp_evaluator import ManifoldCache
        self._csg_cache = ManifoldCache()  # content-hash cache of generated CSGNode subtrees, shared across renders/debug sessions
        self._rendered_tab: FileTab | None = None  # tab that produced the current viewport geometry
        self._dump_dir: Optional[str] = None
        self._dump_frame: int = 0
        self._first_show = True
        # Testing-only escape hatch: set True to make _confirm_unsaved()
        # treat every tab as if it had no unsaved changes, so a script
        # constructing a MainWindow directly can close tabs/the window
        # without blocking on the "Save changes?" QMessageBox modal (which
        # can't be driven programmatically the way a real user would).
        self.skip_unsaved_prompts = False
        # Testing-only escape hatch, like skip_unsaved_prompts above. A
        # verifier that builds a MainWindow, opens the debugger dock, raises
        # the AI dock and then closes would otherwise save that transient
        # arrangement over the user's real layout -- which is exactly how a
        # stray dock tab ends up in their window. Redirecting QSettings
        # cannot do this job: on macOS the (organization, application)
        # constructor ignores setDefaultFormat and resolves to the native
        # plist regardless.
        self.persist_settings = True
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
        self._viewport.perspective_toggled.connect(self._on_viewport_perspective_toggled)
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
        self._debugger_pane.step_to_child_requested.connect(self._on_debug_step_to_child)
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
            (Qt.Modifier.CTRL | Qt.Key.Key_F11, self._debugger_pane._btn_step_to_child),
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
        # Auto-render _CUSTOMIZER_RENDER_DELAY_MS after the user stops
        # editing Customizer fields -- start()ing an already-running QTimer
        # restarts its countdown, which
        # is exactly the debounce this needs (each new edit pushes the
        # render back out, so it only fires once editing has genuinely
        # stopped). _customizer_render_tab is the tab active when the timer
        # was (re)started, checked again on fire in case the user has since
        # switched tabs -- rendering whatever's merely current then would
        # render the wrong file.
        self._customizer_render_timer = QTimer(self)
        self._customizer_render_timer.setSingleShot(True)
        self._customizer_render_timer.timeout.connect(self._on_customizer_render_timer)
        self._customizer_render_tab = None

        self._customizer_dock = QDockWidget("Customizer", self)
        self._customizer_dock.setObjectName("CustomizerDock")
        self._customizer_dock.setWidget(self._customizer_pane)
        self._customizer_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._customizer_dock)
        self.splitDockWidget(self._debugger_dock, self._customizer_dock, Qt.Orientation.Vertical)
        self._customizer_dock.hide()

        # --- Animate dock (right, bottom — tabbed with Customizer) ---
        self._animate_pane = AnimatePane()
        self._animate_pane.render_busy_check = lambda: bool(self._render_jobs)
        self._animate_pane.frame_changed.connect(self._on_animate_frame)
        self._animate_pane.dump_started.connect(self._on_dump_started, Qt.ConnectionType.QueuedConnection)
        self._animate_pane.dump_finished.connect(self._on_dump_finished)

        self._animate_dock = QDockWidget("Animate", self)
        self._animate_dock.setObjectName("AnimateDock")
        self._animate_dock.setWidget(self._animate_pane)
        self._animate_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.tabifyDockWidget(self._customizer_dock, self._animate_dock)
        self._animate_dock.hide()

        # --- AI Chat dock (right, bottom — tabbed with Customizer/Animate) ---
        self._ai_chat_pane = AIChatPane()
        self._ai_chat_pane.send_requested.connect(self._on_ai_send)
        self._ai_chat_pane.proposal_accepted.connect(self._on_ai_proposal_accepted)

        self._ai_chat_dock = QDockWidget("AI Chat", self)
        self._ai_chat_dock.setObjectName("AIChatDock")
        self._ai_chat_dock.setWidget(self._ai_chat_pane)
        self._ai_chat_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.tabifyDockWidget(self._customizer_dock, self._ai_chat_dock)
        self._ai_chat_dock.hide()

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
            view_sub = QMenu(f"View '{name}' as...", self._console)
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
        act_replace = self._add_action(edit_menu, "Find & Replace…", self._find_replace)
        # StandardKey.Replace alone leaves this unbound on macOS -- Qt has no
        # default there, so the menu item had no shortcut at all. Ctrl+Shift+F
        # renders as ⇧⌘F on macOS and rides alongside the standard binding
        # (Ctrl+H) elsewhere rather than replacing it. Empty sequences are
        # filtered out so a platform without a default gets one entry, not a
        # blank one that would display as a stray separator in the menu.
        #
        # NOT Ctrl+Alt+F (⌥⌘F, which is the macOS convention and what this
        # first shipped as): Option rewrites the character it modifies -- ⌥F
        # produces ƒ -- and the native menu bar's key equivalent then never
        # matches, so the shortcut is displayed but silently dead. Confirmed
        # on a real keyboard; the action itself was enabled and correctly
        # bound, which is why no amount of inspecting it found the problem.
        act_replace.setShortcuts([s for s in (QKeySequence(QKeySequence.StandardKey.Replace),
                                               QKeySequence("Ctrl+Shift+F")) if not s.isEmpty()])
        edit_menu.addSeparator()
        self._act_word_wrap = QAction("Word Wrap", self, checkable=True)
        self._act_word_wrap.triggered.connect(self._toggle_word_wrap)
        edit_menu.addAction(self._act_word_wrap)
        edit_menu.addSeparator()
        self._act_read_only = QAction("Read Only", self, checkable=True)
        self._act_read_only.triggered.connect(self._toggle_read_only)
        edit_menu.addAction(self._act_read_only)
        edit_menu.addSeparator()
        prefs_act = self._add_action(edit_menu, "Preferences…", self._open_preferences, QKeySequence("Ctrl+,"))
        prefs_act.setMenuRole(QAction.MenuRole.PreferencesRole)

        # Design
        design_menu = mb.addMenu("Design")
        self._act_render_menu = self._add_action(design_menu, "Render", self._render, QKeySequence("F6"))
        self._add_action(design_menu, "Render with Profiling", lambda: self._render(profile=True))
        self._add_action(design_menu, "Show Profile Report…", self._show_profile_report)
        design_menu.addSeparator()
        self._add_action(design_menu, "Dump CSG Tree to Console", self._dump_csg_tree)
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

        self._act_show_ai_chat = self._ai_chat_dock.toggleViewAction()
        self._act_show_ai_chat.setText("Show AI Chat")
        view_menu.addAction(self._act_show_ai_chat)

        self._act_show_status = self._add_checkable(view_menu, "Show Status Bar", True, self._status_bar.setVisible)

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
            act = self._add_action(view_menu, label,
                             lambda p=preset: self._set_view(p),
                             QKeySequence(key))
            # ApplicationShortcut, not the default WindowShortcut: these must
            # fire while a data-viewer dialog (incl. a modal "Edit as..."
            # editor) is the active window, not just the main window — Qt's
            # shortcut dispatch otherwise suppresses WindowShortcut-context
            # actions owned by a non-active top-level widget whenever any
            # modal widget is active, regardless of ApplicationModal vs.
            # WindowModal.
            act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        view_menu.addSeparator()
        self._add_action(view_menu, "Reset Panel Layout",
                         self._reset_panel_layout)
        view_menu.addSeparator()
        self._act_zoom_in = self._add_action(view_menu, "Zoom In", lambda: self._zoom_viewport(1), QKeySequence("Ctrl+]"))
        self._act_zoom_in.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._act_zoom_out = self._add_action(view_menu, "Zoom Out", lambda: self._zoom_viewport(-1), QKeySequence("Ctrl+["))
        self._act_zoom_out.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        view_menu.addSeparator()
        self._act_spin = self._add_checkable(view_menu, "Spin", False, self._toggle_spin)
        self._act_spin.setShortcut(QKeySequence("Ctrl+Meta+1"))
        self._act_spin.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._act_perspective = self._add_checkable(view_menu, "Perspective", True, self._toggle_perspective)
        self._act_perspective.setShortcut(QKeySequence("Ctrl+Meta+2"))
        self._act_perspective.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._act_stereo = self._add_checkable(view_menu, "Stereo (Cross-eye)", False, self._toggle_stereo)
        self._act_stereo.setShortcut(QKeySequence("Ctrl+Meta+3"))
        self._act_stereo.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        view_menu.addSeparator()
        self._act_show_edges = self._add_checkable(view_menu, "Show Edges", False, self._toggle_edges)
        self._act_show_edges.setShortcut(QKeySequence("Ctrl+1"))
        self._act_show_edges.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._act_show_axes = self._add_checkable(view_menu, "Show Axes", True, self._toggle_axes)
        self._act_show_axes.setShortcut(QKeySequence("Ctrl+2"))
        self._act_show_axes.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._act_show_scale = self._add_checkable(view_menu, "Show Scale Markers", True, self._toggle_scale_markers)
        self._act_show_cross = self._add_checkable(view_menu, "Show Crosshairs", False, self._toggle_crosshairs)
        self._act_show_cross.setShortcut(QKeySequence("Ctrl+3"))
        self._act_show_cross.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
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
        shortcut("Shift+F6", self._start_debug)

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
        tab.editor.edit_parameter_requested.connect(
            lambda word, t=tab: self._edit_customizer_parameter(t, word)
        )
        tab.editor.print_to_console.connect(self._on_debug_print)
        tab.editor.print_value_to_console.connect(self._on_debug_print_value)
        tab.editor.breakpoints_changed.connect(self._on_breakpoints_changed)
        tab.editor.source_edited_externally.connect(lambda t=tab: self._render(t))
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
        vpr_y = ((float(cam.roll)) % 360.0 + 360.0) % 360.0
        vpr_z = ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0
        vpd = float(cam.distance)
        vpf = float(cam.fov)
        self._vpt_label.setText(f"  Viewport: translate = [{vpt[0]:.2f}, {vpt[1]:.2f}, {vpt[2]:.2f}]")
        self._vpr_label.setText(f"  rotate = [{vpr_x:.1f}, {vpr_y:.1f}, {vpr_z:.1f}]")
        self._vpd_label.setText(f"  dist = {vpd:.1f}")
        self._vpf_label.setText(f"  FoV = {vpf:.1f}")

    def _vp_state_strings(self) -> dict:
        import numpy as np
        cam = self._viewport._renderer.camera
        vpt = np.asarray(cam.target)
        vpr_x = ((90.0 - float(cam.elevation)) % 360.0 + 360.0) % 360.0
        vpr_y = ((float(cam.roll)) % 360.0 + 360.0) % 360.0
        vpr_z = ((float(cam.azimuth) - 270.0) % 360.0 + 360.0) % 360.0
        return {
            "$vpt": f"$vpt = [{vpt[0]:.2f}, {vpt[1]:.2f}, {vpt[2]:.2f}]",
            "$vpr": f"$vpr = [{vpr_x:.1f}, {vpr_y:.1f}, {vpr_z:.1f}]",
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
            self._customizer_pane.set_file_path(tab.file_path)
            self._customizer_pane.set_source(tab.editor.toPlainText())
            self._act_read_only.setChecked(tab.editor.isReadOnly())

    def _toggle_read_only(self, enabled: bool):
        tab = self._current_tab()
        if not tab:
            return
        tab.editor.setReadOnly(enabled)
        idx = self._tabs.indexOf(tab)
        self._tabs.setTabText(idx, tab.display_name())

    def _confirm_unsaved(self, tab) -> bool:
        """True if it's OK to proceed past `tab`'s unsaved changes (there
        were none, they were discarded, or they were saved successfully);
        False if the caller should abort (user cancelled, or save
        failed). Shared by _close_tab and closeEvent, the two places that
        need to prompt before discarding a modified tab."""
        if not tab.is_modified or self.skip_unsaved_prompts:
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            f"Save changes to {tab.display_name()}?",
            QMessageBox.StandardButton.Save |
            QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Save:
            self._tabs.setCurrentIndex(self._tabs.indexOf(tab))
            if not self._save_file():
                return False
        return True

    def _close_tab(self, index):
        tab = self._tabs.widget(index)
        if tab and not self._confirm_unsaved(tab):
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
        # A render already in flight is now stale (it's computing geometry
        # for source this edit just superseded) -- cancel it right away
        # rather than letting it run to completion for nothing; the
        # debounced render below will start a fresh one once editing
        # actually settles. Same cancel idiom as Escape (keyPressEvent).
        if self._render_cancel is not None and any(t.isRunning() for _, _, t in self._render_jobs):
            self._render_cancel.set()
            self._set_render_busy(False)
            self.log("Render cancelled — Customizer field changed.")
        self._customizer_render_tab = tab
        self._customizer_render_timer.start(self._CUSTOMIZER_RENDER_DELAY_MS)

    def _on_customizer_render_timer(self):
        tab = self._customizer_render_tab
        self._customizer_render_tab = None
        if tab is not None and tab is self._current_tab():
            self._render(tab)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def _create_and_add_tab(self, path: str, text: str) -> FileTab:
        """Create a fully-connected FileTab for an existing file and add it to the UI."""
        from belfryscad.window.library_manager import _library_dir

        tab = FileTab()
        tab.file_path = path
        tab.editor.setReadOnly(Path(path).resolve().is_relative_to(_library_dir().resolve()))
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
        tab.editor.edit_parameter_requested.connect(
            lambda word, t=tab: self._edit_customizer_parameter(t, word)
        )
        tab.editor.print_to_console.connect(self._on_debug_print)
        tab.editor.print_value_to_console.connect(self._on_debug_print_value)
        tab.editor.breakpoints_changed.connect(self._on_breakpoints_changed)
        tab.editor.source_edited_externally.connect(lambda t=tab: self._render(t))
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
            self, "Save File", tab.file_path or tab.suggested_name or "",
            "OpenSCAD Files (*.scad);;All Files (*)"
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
        if tab is self._current_tab():
            self._customizer_pane.set_file_path(path)
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
                # 3MF keeps the parts as separate objects, so each is
                # checked on its own -- that is what the file contains.
                for problem in self._check_export_bodies(bodies):
                    self.log(f"WARNING: export: {problem}")
                exporters.write_3mf(path, bodies)
            else:
                open_parts = []
                mesh = exporters.merge_bodies_to_mesh(bodies, open_parts)
                for n in open_parts:
                    self.log(f"WARNING: export: part {n} is not a closed "
                             f"solid; its surface is written as-is, and most "
                             f"slicers will reject it.")
                if mesh is None:
                    QMessageBox.warning(self, "Export", "No geometry to export.")
                    return
                # Zero-area faces are removed before writing. They break no
                # topology, but slicers commonly discard them and are then
                # left with the holes their removal opens -- which is how a
                # sound model becomes a failed print. Removing them here
                # closes those gaps properly instead.
                mesh = self._strip_export_slivers(mesh)

                # Checked AFTER merging and stripping, because that is what
                # gets written. Checking the parts instead passed a Menger
                # sponge whose 160,000 cubes were each fine and whose file
                # was riddled with duplicate faces.
                #
                # Warned rather than refused: a deliberately open surface is
                # a legitimate export, and blocking a save the user asked
                # for would be worse than saying so.
                for problem in self._check_export_mesh(mesh):
                    self.log(f"WARNING: export: {problem}")
                if ext == ".obj":
                    exporters.write_obj(path, mesh)
                else:
                    if not path.endswith(".stl"):
                        path += ".stl"
                    exporters.write_stl(path, mesh)
            self.log(f"Exported to {path}")
        except OSError as e:
            QMessageBox.critical(self, "Export Error", str(e))

    def _strip_export_slivers(self, mesh):
        """Remove zero-area faces from the mesh about to be written.

        A retriangulation, not a repair: the solid is unchanged and no
        vertex moves. A sliver's three corners are collinear, so removing
        one leaves its middle vertex on the neighbour's edge -- the
        neighbour is split there to keep the surface closed.

        Logged rather than silent, because it changes the triangle list of
        a file the user asked for.
        """
        try:
            import numpy as np
            from openscad_cpp_evaluator import strip_slivers
        except ImportError:
            return mesh       # older evaluator: leave the mesh alone
        try:
            v = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
            t = np.asarray(mesh.tri_verts, dtype=np.uint32).ravel()
            # Welded by position first. The slivers only exist once
            # coincident vertices are one vertex, which is what writing the
            # file does -- an STL has no indices at all. On a level-4 Menger
            # sponge the unwelded mesh has none and the welded one has 14,
            # so stripping without welding finds nothing to do.
            keys, inverse = np.unique(np.round(v, 6), axis=0, return_inverse=True)
            nv, nt, rep = strip_slivers(keys.ravel().tolist(),
                                        inverse[t].astype(np.uint32).tolist())
        except Exception:      # noqa: BLE001 -- must never block a save
            return mesh
        if not rep.get("removed"):
            return mesh
        n = rep["removed"]
        note = f"stripped {n} zero-area face{'' if n == 1 else 's'}"
        if rep.get("restitched"):
            k = rep["restitched"]
            note += f", splitting {k} neighbour{'' if k == 1 else 's'} to keep it closed"
        self.log(f"export: {note}")
        from types import SimpleNamespace
        return SimpleNamespace(
            vert_properties=np.asarray(nv, dtype=np.float32).reshape(-1, 3),
            tri_verts=np.asarray(nt, dtype=np.uint32).reshape(-1, 3))

    @staticmethod
    def _check_export_mesh(mesh) -> list:
        """Problems with the single mesh about to be written, if any."""
        try:
            import numpy as np
            from openscad_cpp_evaluator import check_mesh
        except ImportError:
            return []
        try:
            v = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
            t = np.asarray(mesh.tri_verts, dtype=np.uint32).ravel()
            # Welded by position first, because that is what reading the
            # file does. An STL carries no indices at all, and OBJ's are
            # discarded by most importers, so two coincident-but-separately
            # indexed copies of a face -- which look like two sound solids
            # to an index-based check -- become the doubled faces a slicer
            # actually chokes on.
            keys, inverse = np.unique(np.round(v, 6), axis=0, return_inverse=True)
            d = check_mesh(keys.ravel().tolist(), inverse[t].tolist())
        except Exception:      # noqa: BLE001 -- a check must never block a save
            return []
        if not d.get("ok", True):
            return [f"the exported mesh is not a closed manifold solid -- {d['summary']}"]
        if d.get("degenerate_faces"):
            # Said separately, and not as "not manifold", because it still
            # is one: CSG emits zero-area triangles routinely and they break
            # no topology. They are worth mentioning only because some
            # slicers discard them and are then left with real holes.
            n = d["degenerate_faces"]
            return [f"the exported mesh is a closed manifold solid, but contains {n} "
                    f"zero-area triangle{'' if n == 1 else 's'}; some slicers drop "
                    f"these and report holes as a result"]
        return []

    @staticmethod
    def _check_export_bodies(bodies) -> list:
        """One message per body that is not a closed manifold solid.

        Uses the evaluator's own check rather than a second implementation
        here: the GUI writes files through belfryscad.exporters instead of
        the C++ writers, so without this the CLI and the GUI would disagree
        about what counts as sound.
        """
        try:
            import numpy as np
            from openscad_cpp_evaluator import check_mesh
        except ImportError:
            return []      # older evaluator: nothing to say rather than a crash
        out = []
        for n, b in enumerate(bodies, start=1):
            body = getattr(b, "body", None)
            if body is None or body.is_empty():
                continue
            try:
                m = body.to_mesh()
                verts = np.asarray(m.vert_properties[:, :3], dtype=np.float32).ravel().tolist()
                tris = np.asarray(m.tri_verts, dtype=np.uint32).ravel().tolist()
                d = check_mesh(verts, tris)
            except Exception:      # noqa: BLE001 -- a check must never block a save
                continue
            if not d.get("ok", True):
                out.append(f"part {n} is not a closed manifold solid -- {d['summary']}")
        return out

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
                new_roll = float(v[1]) % 360.0
                new_az   = (float(v[2]) + 270.0) % 360.0
                if (not math.isclose(cam.elevation, new_elev) or not math.isclose(cam.roll, new_roll)
                        or not math.isclose(cam.azimuth, new_az)):
                    cam.elevation = new_elev
                    cam.roll      = new_roll
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

    def _render(self, tab=None, profile: bool = False):
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
        # Deliberately NOT self._viewport.load_geometry([]) here -- the
        # previous render's geometry stays visible (with the busy overlay on
        # top) until this one's new geometry is ready to replace it, in
        # _on_render_done. Matches CLAUDE.md's own "display last valid
        # geometry while code is invalid" requirement, which clearing here
        # violated for the whole render duration, not just error cases --
        # most visible during animation playback on a slow model, where the
        # viewport was empty for seconds at a time between frames.
        self.log("Rendering…")

        cancel = threading.Event()
        self._render_cancel = cancel
        self._set_render_busy(True)

        worker = _RenderWorker(source, tab.file_path, cancel, self._viewport_params(), manifold_cache=self._csg_cache,
                               profile=profile)
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
            if not self._render_jobs:
                # Only now is it safe to start another render (see
                # AnimatePane.render_busy_check/resume_deferred_advance's own
                # doc comments) -- resume a step deferred by animation
                # playback while this one was in flight, if any.
                self._animate_pane.resume_deferred_advance()

        # callback lives in the main thread; Qt auto-uses QueuedConnection for all
        # of these cross-thread connections, so all slots run on the main thread.
        thread.started.connect(worker.run)
        worker.logged.connect(callback.on_logged)
        worker.parse_errored.connect(callback.on_parse_errored)
        worker.tmp_path_ready.connect(callback.on_tmp_path)
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

    def _on_render_done(self, file_tab, bodies, id_to_node, elapsed_ms: float, render_id: int,
                        final_vp: dict | None = None, csg_tree: list | None = None, profile_result=None):
        if render_id != self._render_id:
            return  # superseded by a later render; discard

        self._rendered_tab = file_tab
        self.id_to_node = id_to_node
        self._last_csg_tree = csg_tree
        self._last_profile_result = profile_result
        if profile_result is not None:
            if getattr(self, "_suppress_profile_report", False):
                self._suppress_profile_report = False
                self.log("Profiling data collected — Design > Show Profile "
                         "Report… to see it in full.")
            else:
                self._show_profile_report()
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
            import numpy as np
            mins, maxs = [], []
            for b in bodies:
                if b.body.is_empty():
                    continue
                v = np.asarray(b.body.to_mesh().vert_properties[:, :3])
                mins.append(v.min(axis=0))
                maxs.append(v.max(axis=0))
            if mins:
                bb_min = np.min(mins, axis=0).astype(np.float32)
                bb_max = np.max(maxs, axis=0).astype(np.float32)
                # Skip auto-fit if the script explicitly positioned the camera,
                # or if animation playback is active.
                if not script_moved_camera and not self._animate_pane.is_playing():
                    self._viewport.frame_scene(bb_min, bb_max)
                # Timestamped so a reader can tell one render's output from
                # the last one's. The AI tools rely on this: the console is
                # otherwise identical whether a render just landed or the
                # text is left over from before the change.
                self.log(f"Rendered successfully at {time.strftime('%H:%M:%S')} "
                         f"in {elapsed_ms / 1000:.3f} seconds.")
                self.log(
                    "Bounds:\n"
                    f"      [{bb_min[0]:.2f}, {bb_min[1]:.2f}, {bb_min[2]:.2f}]\n"
                    f"      [{bb_max[0]:.2f}, {bb_max[1]:.2f}, {bb_max[2]:.2f}]"
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
        # Release any AI follow-up that was waiting on a render: by now the
        # geometry is loaded, so the viewport grab and the measurements it
        # takes will reflect this render.
        self._ai_chat_pane.on_render_finished()

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

    def _dump_csg_tree(self):
        """Print the resolved+generated CSG tree from the last successful
        render to the console — a debugging aid for inspecting the tree
        structure (kind/params/body counts) without a debug session."""
        if not self._last_csg_tree:
            self.log("No CSG tree available — render first.")
            return
        from openscad_cpp_evaluator import format_csg_tree
        self.log(format_csg_tree(self._last_csg_tree))

    def _show_profile_report(self):
        """Open a sortable per-call-site profiling report from the last
        "Render with Profiling" run."""
        if not self._last_profile_result:
            self.log("No profile available — use Render with Profiling first.")
            return
        from belfryscad.window.data_viewers import ProfileViewer
        from belfryscad.window.library_manager import _library_dir
        viewer = ProfileViewer(self._last_profile_result, parent=self,
                                path_labels=self._path_labels,
                                trim_prefix=str(_library_dir()))
        viewer.navigate_requested.connect(self._on_debug_frame_selected)
        viewer.show()

    def _flush_caches(self):
        """Discard each tab's pre-calculated AST scope/node table and the
        incremental Manifold rebuild cache. (The old parser's separate AST
        cache doesn't exist under the C++ backend -- it parses fresh from
        the file on every call, nothing to flush there anymore.)"""
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab:
                tab.root_scope = None
        self.id_to_node = {}
        self._csg_cache.clear()
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
            # _RenderWorker always parses a temp copy of the live buffer
            # (see its own doc comment), so a definition in *this* tab's
            # own top-level code reports origin as tab._last_parse_path,
            # not tab.file_path itself -- accept either as "this tab".
            tab_parse_resolved = (
                str(Path(tab._last_parse_path).resolve())
                if getattr(tab, '_last_parse_path', None) else None
            )
            if def_resolved == tab_resolved or def_resolved == tab_parse_resolved:
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

    # ------------------------------------------------------------------
    # AI chat pane
    # ------------------------------------------------------------------

    def _on_ai_send(self, text: str):
        """Build the tool context on this (the GUI) thread, then hand it to
        the pane to run the turn in a worker. The snapshot is taken fresh
        per turn precisely so tool handlers never touch a live editor from
        the worker thread -- same reason _RenderWorker gets plain source
        text rather than the widget."""
        from belfryscad.window.library_manager import _library_dir
        from belfryscad.window.ai_tools import AIToolContext, TabSnapshot

        tabs = self._tab_snapshots()
        png, note = self._capture_viewport_png()
        ctx = AIToolContext(library_dir=_library_dir(), open_tabs=tabs,
                            viewport_png=png, viewport_note=note,
                            geometry_summary=self._geometry_summary(),
                            console_text=self._console_tail(),
                            capture_view=self._capture_view_threadsafe,
                            ask_user=self._ask_user_threadsafe,
                            live_state=self._live_state_threadsafe,
                            request_render=self._render_threadsafe,
                            check_geometry=self._check_geometry_threadsafe,
                            live_tabs=self._live_tabs_threadsafe,
                            project_dirs=self._project_dirs_threadsafe,
                            profile_report=self._profile_report_threadsafe,
                            debug_control=self._debug_threadsafe)
        self._ai_chat_pane.start_turn(text, ctx)

    _AI_CONSOLE_LINES = 200
    _AI_CONSOLE_CHARS = 20_000

    def _console_tail(self) -> str:
        """The end of the console for the read_console tool. Read here on
        the GUI thread, and capped: a long session's console can run to
        megabytes, and only the last render's output is of any use."""
        try:
            text = self._console.toPlainText()
        except Exception:      # noqa: BLE001 -- the turn proceeds without it
            return ""
        lines = text.splitlines()
        clipped = len(lines) > self._AI_CONSOLE_LINES
        tail = "\n".join(lines[-self._AI_CONSOLE_LINES:])
        if len(tail) > self._AI_CONSOLE_CHARS:
            tail = tail[-self._AI_CONSOLE_CHARS:]
            clipped = True
        return ("(earlier output omitted)\n" + tail) if clipped else tail

    def _geometry_summary(self) -> str:
        """Measurements of the last render, as plain text for the
        describe_geometry tool. Computed here on the GUI thread: Manifold
        objects are never handed to the worker."""
        import numpy as np
        bodies = getattr(self, "_bodies", None)
        if not bodies:
            return ""
        lines, total_v, total_a = [], 0.0, 0.0
        open_count = 0
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        failed = None
        for i, cb in enumerate(bodies):
            try:
                if cb.body.is_empty():
                    continue
                # Rebuilt from the mesh: the body itself is a shim with no
                # measurement API, so reading volume() off it silently threw
                # and left this returning "" -- which the AI's
                # describe_geometry reported as "nothing has been rendered".
                m = exporters.body_to_manifold(cb)
                if m.is_empty():
                    # Manifold rejects anything that isn't a closed solid, so
                    # an open shell converts to nothing. Measured from its
                    # triangles instead -- reporting the empty Manifold gave
                    # "0 triangles, genus 1" and an inverted-infinity box for
                    # a surface plainly visible in the viewport.
                    mv, mt = exporters.body_mesh_arrays(cb)
                    if not len(mt):
                        continue
                    bb = [*mv.min(axis=0), *mv.max(axis=0)]
                    for k in range(3):
                        lo[k] = min(lo[k], bb[k])
                        hi[k] = max(hi[k], bb[k + 3])
                    open_count += 1
                    tri = mv[mt.astype(int)]
                    a = float(np.linalg.norm(
                        np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]),
                        axis=1).sum() / 2)
                    total_a += a
                    lines.append(
                        f"  body {i + 1}: an open surface, NOT a closed solid "
                        f"(so it has no volume and cannot take part in CSG); "
                        f"surface area {a:.3f}, size {bb[3] - bb[0]:.3f} x "
                        f"{bb[4] - bb[1]:.3f} x {bb[5] - bb[2]:.3f}, "
                        f"{len(mt)} triangles")
                    continue
                v, a = m.volume(), m.surface_area()
                bb = m.bounding_box()
                total_v += v
                total_a += a
                for k in range(3):
                    lo[k] = min(lo[k], bb[k])
                    hi[k] = max(hi[k], bb[k + 3])
                lines.append(
                    f"  body {i + 1}: volume {v:.3f}, surface area {a:.3f}, "
                    f"size {bb[3] - bb[0]:.3f} x {bb[4] - bb[1]:.3f} x "
                    f"{bb[5] - bb[2]:.3f}, {m.num_tri()} triangles, "
                    f"genus {m.genus()}")
            except Exception as e:      # noqa: BLE001 -- report what we can
                failed = failed or e
                continue
        if not lines:
            # Distinguishable from "nothing rendered": bodies that all fail to
            # measure used to look identical to no bodies at all.
            return (f"Error: the rendered model could not be measured ({failed})."
                    if failed and bodies else "")
        solids = len(lines) - open_count
        headline = f"Rendered model: {solids} solid part(s)"
        if open_count:
            headline += (f" and {open_count} open surface(s) -- an open "
                         f"surface has no volume and cannot be used in CSG "
                         f"or printed")
        return "\n".join([
            headline + ".",
            f"Total volume {total_v:.3f}, total surface area {total_a:.3f}.",
            f"Overall bounding box: [{lo[0]:.3f}, {lo[1]:.3f}, {lo[2]:.3f}] to "
            f"[{hi[0]:.3f}, {hi[1]:.3f}, {hi[2]:.3f}] "
            f"(size {hi[0] - lo[0]:.3f} x {hi[1] - lo[1]:.3f} x {hi[2] - lo[2]:.3f}).",
            "Genus counts through-holes: 0 means none.",
            "Per part:",
            *lines,
        ])

    # Named camera angles for the AI's view_viewport tool, as
    # (azimuth, elevation). Matches the View menu's own presets.
    _AI_VIEW_ANGLES = {
        "front": (270, 0), "back": (90, 0), "left": (180, 0), "right": (0, 0),
        "top": (270, 89), "bottom": (270, -89), "iso": (315, 35),
    }

    def _ask_user_threadsafe(self, questions: list) -> bool:
        """Called from the AI worker thread. Hands the questions to the GUI
        thread and returns at once.

        Nothing waits. Blocking here would hold a tool call -- and on the CLI
        transports an MCP request -- open for however long the user takes to
        think, which outlasts client read timeouts and pins a worker thread
        on something that is not work. The answer comes back as an ordinary
        user message instead, and dismissing the question cancels the turn
        that asked it.
        """
        from PySide6.QtCore import QMetaObject
        if getattr(self, "_ai_ask_dialog", None) is not None:
            return False
        self._ai_ask_pending = questions
        QMetaObject.invokeMethod(self, "_service_ai_ask_request",
                                 Qt.ConnectionType.QueuedConnection)
        return True

    @Slot()
    def _service_ai_ask_request(self):
        """GUI thread: put the questions on screen, modeless."""
        questions = getattr(self, "_ai_ask_pending", None)
        if not questions:
            return
        self._ai_ask_pending = None
        try:
            from belfryscad.window.ai_question_dialog import AIQuestionDialog
            dlg = AIQuestionDialog(questions, parent=self)
        except Exception as e:      # noqa: BLE001
            self.log(f"AI question dialog failed: {e}")
            return
        self._ai_ask_dialog = dlg
        dlg.finished.connect(lambda code, d=dlg: self._on_ai_ask_finished(code, d))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_ai_ask_finished(self, code, dlg):
        self._ai_ask_dialog = None
        pane = getattr(self, "_ai_chat_pane", None)
        if pane is None:
            return
        if code != QDialog.DialogCode.Accepted:
            # Dismissed. Stop the work that asked -- carrying on would mean
            # proceeding with exactly the guess the question existed to avoid.
            pane.cancel_turn()
            return
        text = self._format_ai_answers(dlg.questions, dlg.answers)
        if text:
            pane.submit_user_text(text)
        else:
            # Accepted with nothing chosen and nothing typed. Treated as a
            # dismissal rather than sent as an empty answer, which would read
            # to the model as "none of the above".
            pane.cancel_turn()

    @staticmethod
    def _format_ai_answers(questions: list, answers: list) -> str:
        """The answers as the user would have typed them."""
        lines = []
        for spec, ans in zip(questions, answers):
            picked = ans.get("selected") or []
            note = (ans.get("note") or "").strip()
            if not picked and not note:
                continue
            body = ", ".join(picked) if picked else ""
            if note:
                body = f"{body} ({note})" if body else note
            lines.append(f"{spec.get('question', '').strip()} {body}".strip())
        return "\n".join(lines)

    def _render_busy(self) -> bool:
        """Whether a render is still running. A render is asynchronous, so
        the AI's tools have to distinguish "nothing rendered" from "not
        rendered yet"."""
        return any(t.isRunning() for _, _, t in self._render_jobs)

    def _tab_snapshots(self) -> list:
        """Every open .scad tab, as plain data. Read on the GUI thread and
        handed over as text so tool handlers never touch a live editor from
        the worker."""
        from belfryscad.window.ai_tools import TabSnapshot
        tabs = []
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab is None:
                continue
            if tab.file_path and not tab.file_path.lower().endswith(".scad"):
                continue
            tabs.append(TabSnapshot(
                id=tab.chat_id,
                name=tab.display_name(),
                path=tab.file_path,
                modified=tab.is_modified,
                text=tab.editor.toPlainText(),
            ))
        return tabs

    def _project_dirs(self) -> list:
        """Directories holding the user's open scripts. These bound what the
        project file tools may read: opening a file is what makes its
        folder legible, so nothing outside one is reachable."""
        from pathlib import Path as _P
        seen, out = set(), []
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            path = getattr(tab, "file_path", None) if tab else None
            if not path:
                continue
            d = _P(path).resolve().parent
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    def _live_state_threadsafe(self, want_check: bool = False,
                               want_tabs: bool = False,
                               want_profile: bool = False,
                               timeout: float = 10) -> dict:
        """Called from the AI worker thread: the geometry summary and
        console as they are now, rather than as they were when the turn
        started. Accepting a proposal re-renders, so a model that looks
        after making a change was otherwise shown the state from before
        it."""
        import threading
        from PySide6.QtCore import QMetaObject
        self._ai_state_request = {"done": threading.Event(), "result": None,
                                  "check": want_check, "tabs": want_tabs,
                                  "profile": want_profile}
        QMetaObject.invokeMethod(self, "_service_ai_state_request",
                                 Qt.ConnectionType.QueuedConnection)
        if not self._ai_state_request["done"].wait(timeout):
            return {}
        return self._ai_state_request["result"] or {}

    @Slot()
    def _service_ai_state_request(self):
        req = getattr(self, "_ai_state_request", None)
        if not req:
            return
        try:
            res = {"geometry": self._geometry_summary(),
                   "console": self._console_tail(),
                   "rendering": self._render_busy()}
            if req.get("check"):
                res["check"] = self._geometry_check()
            if req.get("tabs"):
                res["tabs"] = self._tab_snapshots()
                res["project_dirs"] = self._project_dirs()
            if req.get("profile"):
                res["profile"] = self._profile_report()
            req["result"] = res
        finally:
            # Set even if reading threw, or the worker blocks for the full
            # timeout to learn nothing.
            req["done"].set()

    def _live_tabs_threadsafe(self) -> list:
        """The open scripts as they are now. The per-turn snapshot goes
        stale the moment the user types, and a model reasoning about text
        that no longer exists wastes a turn at best."""
        return self._live_state_threadsafe(want_tabs=True).get("tabs") or []

    def _project_dirs_threadsafe(self) -> list:
        return self._live_state_threadsafe(want_tabs=True).get(
            "project_dirs") or []

    _AI_PROFILE_SITES = 25

    def _profile_report(self) -> str:
        """The last profiled render as text, for the AI's read_profile.

        The slowest call sites rather than the whole tree: the tree is what
        the ProfileViewer is for, and a BOSL2 render has thousands of nodes.
        Self time is what ranks them -- cumulative time would put the
        top-level call first every time and say nothing.
        """
        result = getattr(self, "_last_profile_result", None)
        if not result:
            return ""
        try:
            # Percentages are of resolve_time, not of the whole render:
            # self times sum to resolve_time + unattributed_time, so this is
            # the only denominator they honestly add up against.
            resolve = result.resolve_time or 0.0
            generate = getattr(result, "generate_time", 0.0) or 0.0
            total = getattr(result, "total_time", 0.0) or (resolve + generate)
            unattributed = getattr(result, "unattributed_time", 0.0) or 0.0
            sites = sorted(result.call_sites,
                           key=lambda s: s.self_time, reverse=True)
            pct = (lambda t: 100 * t / resolve) if resolve > 0 else (lambda t: 0.0)
            lines = [
                f"Profiled render: {total * 1000:.1f} ms total -- "
                f"{resolve * 1000:.1f} ms running the script, "
                f"{generate * 1000:.1f} ms building geometry.",
            ]
            if total > 0 and generate > resolve:
                # Worth saying plainly: rewriting the script cannot help much
                # when most of the time is Manifold doing boolean work.
                lines.append(
                    f"Most of the time ({100 * generate / total:.0f}%) went on "
                    f"geometry, not on script evaluation -- that is Manifold "
                    f"doing CSG, and is reduced by making the model simpler "
                    f"(fewer/cheaper booleans, lower $fn), not by rewriting "
                    f"the script's logic.")
            lines.append(
                f"{len(result.call_sites)} call site(s). Percentages below are "
                f"of the {resolve * 1000:.1f} ms script time"
                + (f"; {100 * unattributed / resolve:.0f}% of that is "
                   f"top-level code outside any call." if resolve > 0
                      and unattributed else "."))
            lines.append(
                f"The {min(len(sites), self._AI_PROFILE_SITES)} slowest by "
                f"self time (time in the call itself, not its children):")
            for s in sites[:self._AI_PROFILE_SITES]:
                where = self._display_profile_origin(s.call_origin)
                lines.append(
                    f"  {s.name} ({s.kind}) called {s.call_count}x from "
                    f"{s.caller_name} at {where}:{s.call_line}:"
                    f"{s.call_column} -- self {s.self_time * 1000:.1f} ms "
                    f"({pct(s.self_time):.1f}%), total "
                    f"{s.cumulative_time * 1000:.1f} ms "
                    f"({pct(s.cumulative_time):.1f}%)")
            return "\n".join(lines)
        except Exception as e:      # noqa: BLE001
            return f"Error: the profile could not be summarised ({e})."

    def _display_profile_origin(self, origin) -> str:
        """A profile call site names the temp file the evaluator parsed;
        show the tab it came from, as the ProfileViewer does."""
        if not origin:
            return "?"
        label = self._path_labels.get(origin)
        return label or Path(origin).name

    def _profile_report_threadsafe(self) -> str:
        return self._live_state_threadsafe(want_profile=True).get("profile", "")

    # ------------------------------------------------------------------
    # Debugger, driven by the AI
    #
    # The session is stateful and the tool interface is not, so each tool
    # call issues one command and then blocks until the session next comes
    # to rest -- a pause, an error, or the end of the run. That is what
    # makes "step and tell me where I am" a single tool call.
    # ------------------------------------------------------------------
    _AI_DEBUG_TIMEOUT = 120
    _AI_MAX_LOCALS = 40
    _AI_MAX_VALUE = 200

    def _ai_debug_notify(self, state: dict):
        """Record where the session has come to rest and wake the waiting
        tool. Called from the same handlers that update the pane, so the
        model and the user are told the same thing."""
        self._ai_debug_state = state
        ev = getattr(self, "_ai_debug_event", None)
        if ev is not None:
            ev.set()

    def _ai_debug_label(self, origin: str) -> str:
        """A file name the model can act on.

        The debugged tab's own code is parsed from an ephemeral temp file,
        so its origin is a random tmp name that means nothing to anyone and
        will not exist once the session ends. Same remap the debugger pane
        does for its own File column.
        """
        import os
        tab = getattr(self, "_debug_tab", None)
        parse_path = getattr(tab, "_last_parse_path", None) if tab else None
        if origin and parse_path:
            try:
                if os.path.realpath(origin) == os.path.realpath(parse_path):
                    return (Path(tab.file_path).name if tab.file_path
                            else tab.display_name())
            except OSError:
                pass
        return Path(origin).name if origin else ""

    def _ai_frame_state(self, origin: str, line: int, all_frame_locals: list,
                        call_stack: list, status: str = "paused",
                        message: str = "") -> dict:
        """One pause, as plain data."""
        def clip(v):
            s = str(v)
            return s if len(s) <= self._AI_MAX_VALUE else s[:self._AI_MAX_VALUE] + "..."

        def frame_line(entry):
            # (kind, name, call_pos, decl_pos) -- raw tuples carry
            # _Position objects that str() renders as memory addresses.
            kind = entry[0] if entry else ""
            if kind == "toplevel":
                return "<toplevel>"
            name = entry[1] if len(entry) > 1 else "?"
            call_pos = entry[2] if len(entry) > 2 else None
            where = ""
            if call_pos is not None:
                cl = getattr(call_pos, "line", None)
                co = self._ai_debug_label(getattr(call_pos, "origin", "") or "")
                if cl:
                    where = f", called from {co}:{cl}" if co else f", called from line {cl}"
            return f"{name}() [{kind}]{where}"

        frames = []
        for fr in (all_frame_locals or [])[:12]:
            merged = {**(fr.get("outer_scope") or {}),
                      **(fr.get("local_scope") or {})}
            # The script's own names first: a frame carries nine or so $
            # specials ($fa, $vpt, $parent_modules...) that would otherwise
            # crowd out what was actually asked about, and the list is
            # truncated.
            names = sorted(merged, key=lambda n: (n.startswith("$"), n))
            names = names[:self._AI_MAX_LOCALS]
            frames.append({
                "variables": {n: clip(merged[n]) for n in names},
                "truncated": max(0, len(merged) - len(names)),
            })
        return {
            "status": status,
            "file": self._ai_debug_label(origin),
            "line": line,
            "message": message,
            "stack": [frame_line(e) for e in reversed((call_stack or [])[-20:])],
            "frames": frames,
        }

    def _debug_threadsafe(self, action: str, arg=None) -> dict:
        """Called from the AI worker thread. Issues one debugger command on
        the GUI thread, then waits for the session to come to rest."""
        import threading
        from PySide6.QtCore import QMetaObject
        self._ai_debug_event = threading.Event()
        self._ai_debug_state = None
        self._ai_debug_cmd = (action, arg)
        QMetaObject.invokeMethod(self, "_service_ai_debug_cmd",
                                 Qt.ConnectionType.QueuedConnection)
        if action == "state":
            # Nothing to wait for -- the slot answers immediately.
            if not self._ai_debug_event.wait(10):
                return {"status": "error", "message": "the GUI did not respond."}
            return self._ai_debug_state or {"status": "idle"}
        if not self._ai_debug_event.wait(self._AI_DEBUG_TIMEOUT):
            return {"status": "running",
                    "message": (f"still running after {self._AI_DEBUG_TIMEOUT}s "
                                f"without reaching a breakpoint.")}
        return self._ai_debug_state or {"status": "idle"}

    @Slot()
    def _service_ai_debug_cmd(self):
        action, arg = getattr(self, "_ai_debug_cmd", (None, None))
        try:
            if action == "start":
                self._ai_debug_do_start(arg or {})
            elif action == "resume":
                self._ai_debug_do_resume(arg)
            elif action == "stop":
                if self._debug_session and self._debug_session.is_running():
                    self._on_debug_stop()
                self._ai_debug_notify({"status": "stopped"})
            elif action == "state":
                running = bool(self._debug_session
                               and self._debug_session.is_running())
                if not running:
                    self._ai_debug_notify({"status": "idle"})
                else:
                    last = getattr(self, "_ai_debug_last_pause", None)
                    self._ai_debug_notify(last or {"status": "running"})
        except Exception as e:      # noqa: BLE001 -- reported, never raised
            self._ai_debug_notify({"status": "error", "message": str(e)})

    def _ai_debug_do_start(self, spec: dict):
        tab = self._tab_by_chat_id(spec.get("id"))
        if tab is None:
            self._ai_debug_notify({"status": "error",
                                   "message": "that script is not open."})
            return
        if not tab.editor.toPlainText().strip():
            self._ai_debug_notify({"status": "error",
                                   "message": "that script is empty."})
            return
        if self._debug_session and self._debug_session.is_running():
            self._ai_debug_notify({
                "status": "error",
                "message": ("a debug session is already running -- stop it "
                            "before starting another.")})
            return

        # Set as real gutter breakpoints rather than through a private
        # channel, so the user can see where the model chose to stop.
        # Added to theirs, not replacing them: removing a breakpoint someone
        # placed by hand would be a surprising thing for a tool to do.
        added = []
        for ln in spec.get("breakpoints") or []:
            try:
                block = int(ln) - 1
            except (TypeError, ValueError):
                continue
            if block < 0 or block >= tab.editor.document().blockCount():
                continue
            if block not in tab.editor._breakpoints:
                tab.editor.toggle_breakpoint(block)
                added.append(block + 1)

        idx = self._tabs.indexOf(tab)
        if idx >= 0:
            self._tabs.setCurrentIndex(idx)
        self._ai_debug_started_bps = (tab, added)
        self._ai_debug_last_pause = None
        if added:
            self.log(f"AI: set breakpoint(s) at line {', '.join(map(str, added))}.")
        # _start_debug does the rest -- live-buffer temp file, the parse,
        # and duplicating breakpoint keys under the temp path so they still
        # match. None of that is worth a second implementation.
        self._start_debug()
        if not (self._debug_session and self._debug_session.is_running()):
            self._ai_debug_notify({
                "status": "error",
                "message": ("the session did not start -- the script most "
                            "likely failed to parse; read_console has the "
                            "error.")})

    _AI_DEBUG_COMMANDS = {"continue": "_on_debug_continue",
                          "into": "_on_debug_step_into",
                          "over": "_on_debug_step_over",
                          "out": "_on_debug_step_out",
                          "to_child": "_on_debug_step_to_child"}

    def _ai_debug_do_resume(self, command: str):
        if not (self._debug_session and self._debug_session.is_running()):
            self._ai_debug_notify({
                "status": "idle",
                "message": "no debug session is running."})
            return
        handler = self._AI_DEBUG_COMMANDS.get(command)
        if handler is None:
            self._ai_debug_notify({
                "status": "error",
                "message": (f"unknown command {command!r}; expected one of "
                            f"{', '.join(sorted(self._AI_DEBUG_COMMANDS))}.")})
            return
        getattr(self, handler)()

    def _check_geometry_threadsafe(self) -> str:
        """The AI's check_geometry tool. A generous timeout: this is a full
        topology pass plus the export union, and a Menger sponge is 400,000
        triangles -- far longer than an ordinary state read."""
        return self._live_state_threadsafe(want_check=True, timeout=120).get(
            "check", "")

    def _geometry_check(self) -> str:
        """Mesh soundness of the last render, per part and for the merged
        mesh an export would write.

        Both, because they answer different questions and can disagree: a
        Menger sponge is thousands of individually perfect cubes whose
        concatenation is riddled with duplicate faces, and checking only the
        parts passed it.
        """
        bodies = getattr(self, "_bodies", None)
        if not bodies:
            return ""
        import numpy as np
        try:
            from openscad_cpp_evaluator import check_mesh
        except ImportError:
            return "Error: this evaluator has no mesh check."

        lines, bad = [], 0
        for n, b in enumerate(bodies, start=1):
            body = getattr(b, "body", None)
            if body is None or body.is_empty():
                continue
            try:
                v, t = exporters.body_mesh_arrays(b)
                d = check_mesh(v.ravel().tolist(), t.ravel().tolist())
            except Exception as e:      # noqa: BLE001
                lines.append(f"  part {n}: could not be checked ({e})")
                continue
            if d.get("ok"):
                lines.append(f"  part {n}: closed manifold solid"
                             + (f" -- but {d['summary']}" if d.get("summary") else ""))
            else:
                bad += 1
                lines.append(f"  part {n}: NOT a closed manifold solid -- "
                             f"{d.get('summary', 'no detail')}")
        if not lines:
            return "The render produced no geometry to check."

        out = [f"{len(lines)} part(s) checked, {bad} unsound.", *lines]

        # What a file would contain, which is not the same question: the
        # parts are unioned on the way out.
        try:
            open_parts = []
            mesh = exporters.merge_bodies_to_mesh(bodies, open_parts)
            if mesh is None:
                out.append("\nMerged for export: nothing would be written.")
            else:
                v = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
                t = np.asarray(mesh.tri_verts, dtype=np.uint32)
                d = check_mesh(v.ravel().tolist(), t.ravel().tolist())
                state = ("a closed manifold solid" if d.get("ok")
                         else "NOT a closed manifold solid")
                out.append(f"\nMerged for export: {len(t)} triangles, {state}"
                           + (f" -- {d['summary']}" if d.get("summary") else "."))
                if d.get("degenerate_faces"):
                    out.append("Zero-area faces are stripped automatically on "
                               "export, so they are not a defect in the file.")
                for p in open_parts:
                    out.append(f"Part {p} is not a closed solid; its surface "
                               f"is written as-is and most slicers reject it.")
        except Exception as e:      # noqa: BLE001
            out.append(f"\nMerged for export: could not be checked ({e}).")
        return "\n".join(out)

    def _render_threadsafe(self, chat_id=None, profile: bool = False) -> bool:
        """The AI's render tool. Queues the render on the GUI thread and
        returns whether there was anything to render -- not whether it
        succeeded, which isn't known until it finishes."""
        from PySide6.QtCore import QMetaObject
        tab = self._tab_by_chat_id(chat_id) if chat_id is not None \
            else self._current_tab()
        if tab is None or not tab.editor.toPlainText().strip():
            return False
        self._ai_render_tab = tab
        self._ai_render_profile = bool(profile)
        QMetaObject.invokeMethod(self, "_render_for_ai",
                                 Qt.ConnectionType.QueuedConnection)
        return True

    @Slot()
    def _render_for_ai(self):
        """invokeMethod resolves by name through the meta-object, so the
        target has to be a registered slot; _render is a plain method with
        arguments."""
        tab = getattr(self, "_ai_render_tab", None)
        if tab is not None:
            # Made active first: the geometry, console and viewport the AI
            # reads afterwards all describe the last render, whichever tab
            # it came from, so leaving a different tab selected would show
            # the user one script and the model another.
            idx = self._tabs.indexOf(tab)
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)
        profile = bool(getattr(self, "_ai_render_profile", False))
        # The report window is the user's own way of reading a profile; when
        # the AI asked for one it reads it through read_profile instead, so
        # popping a window at them unbidden mid-conversation would be noise.
        # Menu-driven profiling still opens it.
        self._suppress_profile_report = profile
        self._render(tab, profile=profile)

    def _capture_view_threadsafe(self, view: str, overrides: dict | None = None):
        """Called from the AI worker thread. Hands the request to the GUI
        thread (only it may touch GL) and waits for the image. Safe from
        deadlock because the GUI thread never waits on the worker."""
        import threading
        from PySide6.QtCore import QMetaObject
        self._ai_view_request = {"view": view, "overrides": overrides or {},
                                 "done": threading.Event(), "result": None}
        QMetaObject.invokeMethod(self, "_service_ai_view_request",
                                 Qt.ConnectionType.QueuedConnection)
        if not self._ai_view_request["done"].wait(20):
            return None
        return self._ai_view_request["result"]

    @Slot()
    def _service_ai_view_request(self):
        """GUI thread: point the camera at the requested angle, grab, and put
        it back. grabFramebuffer() renders offscreen and we restore before
        returning to the event loop, so the visible viewport never flickers."""
        req = getattr(self, "_ai_view_request", None)
        if not req:
            return
        renderer = self._viewport._renderer
        cam = renderer.camera
        # Everything touched here is restored in the finally block: these
        # overrides colour one AI-requested image only, never the user's
        # own display settings.
        saved = (cam.azimuth, cam.elevation, cam.roll, cam.orthographic,
                 renderer.show_axes, renderer.show_edges)
        ov = req.get("overrides") or {}
        try:
            az, el = self._AI_VIEW_ANGLES.get(
                req["view"], (cam.azimuth, cam.elevation))
            if ov.get("azimuth") is not None or ov.get("elevation") is not None:
                # An explicit angle wins over the named view.
                az = ov.get("azimuth", cam.azimuth)
                el = ov.get("elevation", cam.elevation)
                if az is None:
                    az = cam.azimuth
                if el is None:
                    el = cam.elevation
            cam.azimuth, cam.elevation, cam.roll = az, el, 0.0
            if ov.get("projection") is not None:
                cam.orthographic = ov["projection"] == "orthographic"
            if ov.get("axes") is not None:
                renderer.show_axes = bool(ov["axes"])
            if ov.get("edges") is not None:
                renderer.show_edges = bool(ov["edges"])

            png, _note = self._capture_viewport_png()
            if png:
                import base64
                req["result"] = (base64.b64encode(png).decode("ascii"),
                                 self._describe_ai_view(req["view"], ov, az, el))
        except Exception as e:      # noqa: BLE001 -- the tool reports failure
            self.log(f"AI: could not render the {req['view']} view: {e}")
        finally:
            (cam.azimuth, cam.elevation, cam.roll, cam.orthographic,
             renderer.show_axes, renderer.show_edges) = saved
            req["done"].set()

    @staticmethod
    def _describe_ai_view(view: str, ov: dict, az: float, el: float) -> str:
        """Caption telling the model exactly what it's looking at -- it
        can't tell an orthographic view from a perspective one by eye."""
        if ov.get("azimuth") is not None or ov.get("elevation") is not None:
            head = f"View at azimuth {az:.0f}°, elevation {el:.0f}°"
        else:
            head = f"{view.capitalize()} view"
        extra = []
        if ov.get("projection"):
            extra.append(ov["projection"])
        if ov.get("axes") is not None:
            extra.append("axes on" if ov["axes"] else "axes off")
        if ov.get("edges") is not None:
            extra.append("edges on" if ov["edges"] else "edges off")
        return f"{head} ({', '.join(extra)})" if extra else head

    _AI_VIEWPORT_MAX_PX = 1024

    def _capture_viewport_png(self) -> tuple[bytes | None, str]:
        """Grab the viewport for the view_viewport tool. Must happen here,
        on the GUI thread -- grabFramebuffer() touches the GL context. Taken
        once per turn alongside the text snapshot, and scaled down: a retina
        viewport is far larger than any model needs and costs prompt tokens
        for nothing."""
        from belfryscad.window.ai_tools import image_to_png
        try:
            png = image_to_png(self._viewport.grabFramebuffer(),
                               self._AI_VIEWPORT_MAX_PX)
            if png is None:
                return None, ""
            cam = self._viewport._renderer.camera
            note = (f"Rendered viewport, azimuth {cam.azimuth:.0f}°, "
                    f"elevation {cam.elevation:.0f}°")
            return png, note
        except Exception as e:      # noqa: BLE001 -- the turn proceeds without it
            self.log(f"AI: could not capture the viewport: {e}")
            return None, ""

    def _tab_by_chat_id(self, chat_id: int):
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if tab is not None and tab.chat_id == chat_id:
                return tab
        return None

    def _on_ai_proposal_accepted(self, proposal):
        """Apply a change the user accepted in the chat pane's review bar.
        Goes through replace_span + source_edited_externally, the same path
        "Edit as..."/"Reformat Selection" use, so it lands as one clean undo
        step and triggers a re-render."""
        if proposal.kind == "edit":
            tab = self._tab_by_chat_id(proposal.tab_id)
            if tab is None:
                self.log("AI: that script is no longer open; change not applied.")
                return
            if tab.editor.isReadOnly():
                self.log("AI: that script is read-only; change not applied.")
                return
            if proposal.param_changes:
                # Re-applied to the live buffer, not taken as finished text:
                # the Customizer is the pane the user is most likely to be
                # moving while the turn runs, and rewriting the file from
                # the turn-start snapshot would put their values back.
                from belfryscad.window.customizer import (
                    describe_parameters, write_back_value)
                live = tab.editor.toPlainText()
                have = {p["name"] for p in describe_parameters(live)}
                missing = [n for n in proposal.param_changes if n not in have]
                if missing:
                    self.log(f"AI: {', '.join(missing)} no longer "
                             f"{'is' if len(missing) == 1 else 'are'} a "
                             f"parameter of this script; nothing was applied.")
                    return
                new_text = live
                for pname, pvalue in proposal.param_changes.items():
                    new_text = write_back_value(new_text, pname, pvalue)
                if new_text == live:
                    self.log("AI: those parameters already have those "
                             "values; nothing was applied.")
                    return
                tab.editor.replace_span(0, len(live), new_text)
            elif proposal.anchor is not None:
                # Re-found in the live buffer rather than trusting the
                # whole-file content built from the turn-start snapshot: the
                # user may have typed since, and rewriting only this span
                # keeps whatever else they changed. A buffer that moved so
                # far the anchor is gone refuses instead of guessing.
                live = tab.editor.toPlainText()
                found = live.count(proposal.anchor)
                if found != 1:
                    self.log(
                        "AI: the text this change was anchored to "
                        + ("is no longer in the script" if found == 0
                           else f"now appears {found} times")
                        + "; the script changed since it was proposed, so "
                          "nothing was applied. Ask for it again.")
                    return
                start = live.index(proposal.anchor)
                tab.editor.replace_span(start, start + len(proposal.anchor),
                                        proposal.replacement or "")
            else:
                tab.editor.replace_span(0, len(tab.editor.toPlainText()),
                                        proposal.new_content)
            tab.editor.source_edited_externally.emit()
            idx = self._tabs.indexOf(tab)
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)
        else:
            # New script: open it as an unsaved tab and let the user choose
            # where it goes -- never write to disk unprompted.
            self._new_document()
            tab = self._current_tab()
            if tab is not None:
                tab.suggested_name = proposal.filename or None
                tab.editor.setPlainText(proposal.new_content)
                idx = self._tabs.indexOf(tab)
                if idx >= 0:
                    self._tabs.setTabText(idx, tab.display_name())
                tab.editor.source_edited_externally.emit()

    def _edit_customizer_parameter(self, tab, word: str):
        """CodeEditor's "Edit Parameter '<word>'..." context-menu action --
        the same ParameterEditorDialog CustomizerPane's own right-click menu
        opens, reachable directly from any use of the name in the code
        editor (not just its declaration line, and not just from the
        Customizer dock)."""
        from belfryscad.window.customizer import scan_parameters, replace_parameter
        from belfryscad.window.customizer_param_dialog import ParameterEditorDialog

        source = tab.editor.toPlainText()
        params = scan_parameters(source)
        param = next((p for p in params if p.name == word), None)
        if param is None:
            return
        tabs = list(dict.fromkeys(p.tab for p in params))
        dlg = ParameterEditorDialog(
            existing_tabs=tabs, existing_names=[p.name for p in params],
            editing=param, parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        result = dlg.result_values()
        if result is None:
            return
        _name, default, description, _tab, constraint = result
        new_source = replace_parameter(source, word, default, description, constraint)
        if tab is self._current_tab():
            self._on_customizer_source_changed(new_source)
        else:
            # _on_customizer_source_changed always targets _current_tab();
            # editing a parameter from a background tab's editor (e.g. one
            # opened via Go to Definition into a use<>d library file) needs
            # its own direct write, with no Customizer-pane/render-timer
            # side effects on a tab that isn't even visible.
            cursor_pos = tab.editor.textCursor().position()
            tab.editor.setPlainText(new_source)
            cursor = tab.editor.textCursor()
            cursor.setPosition(min(cursor_pos, len(new_source)))
            tab.editor.setTextCursor(cursor)

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

        import tempfile
        from openscad_cpp_evaluator import parse as _oce_parse, ParseError

        # Always parse the live buffer, same as _do_render (see its own
        # doc comment) -- the debug session had the identical
        # stale-on-disk-content bug: "if tab.file_path: parse_path =
        # tab.file_path" ignored *source* entirely for any saved tab.
        # Written into file_path's own directory (when set) so relative
        # use/include still resolve. The debug session owns the temp's
        # lifetime -- it unlinks cleanup_path when it ends.
        tmp_dir = str(Path(tab.file_path).parent) if tab.file_path else None
        _tmp = tempfile.NamedTemporaryFile(
            suffix=".scad", mode="w", encoding="utf-8", delete=False, dir=tmp_dir
        )
        _tmp.write(source)
        _tmp.close()
        parse_path = cleanup_path = _tmp.name
        tab._last_parse_path = parse_path

        # checkDebug()/AST positions now always report *parse_path* as
        # their origin for this tab's own top-level code (verified
        # directly against the real debug hook -- see
        # project_render_stale_buffer_bug memory), never file_path, so
        # that's the identity the C++ hook layer's own fallback
        # (_make_hook's _resolve) needs too.
        current_file = parse_path
        try:
            root_scope = _oce_parse(parse_path)  # for go-to-definition during debug
        except ParseError as e:
            self._parse_error_to_editor(tab, str(e))
            if cleanup_path:
                import os as _os
                try:
                    _os.unlink(cleanup_path)
                except OSError:
                    pass
            return
        except Exception as e:
            self.log(f"Parse error: {e}")
            if cleanup_path:
                import os as _os
                try:
                    _os.unlink(cleanup_path)
                except OSError:
                    pass
            return

        tab.editor.clear_errors()
        tab.root_scope = root_scope

        breakpoints = self._collect_breakpoints()
        # _collect_breakpoints keys everything by real file_path, but the
        # C++ hook now reports this tab's own checkpoints under parse_path
        # (the temp file) -- duplicate this tab's own breakpoint set under
        # that key too, or they'd silently never match and stop firing.
        if tab.file_path:
            tab_key = str(Path(tab.file_path).resolve())
            if tab_key in breakpoints:
                breakpoints[str(Path(parse_path).resolve())] = breakpoints[tab_key]

        # Show the debugger dock and bring it to the front
        self._debugger_dock.show()
        self._debugger_dock.raise_()

        self._debug_tab = tab
        self._debug_session = DebugSession(self, manifold_cache=self._csg_cache)
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
        self._debug_session.start(parse_path, breakpoints,
                                self._viewport_params(),
                                current_file=current_file,
                                cleanup_path=cleanup_path)

    def _find_or_open_tab(self, file_path: str):
        """Return the tab for *file_path*, opening it in a new tab if needed."""
        resolved = str(Path(file_path).resolve())
        for i in range(self._tabs.count()):
            t = self._tabs.widget(i)
            if not t:
                continue
            # _last_parse_path as well as file_path: a modified tab renders
            # from a temp copy of its live buffer, so anything reporting a
            # source location -- a profile call site, an error -- names that
            # temp path, not the tab's own. Matching only file_path missed
            # every such row, and the fallback open below cannot rescue it
            # either: the temp file is unlinked once the render ends, so the
            # read fails and the caller silently does nothing.
            for p in (t.file_path, getattr(t, '_last_parse_path', None)):
                if p and str(Path(p).resolve()) == resolved:
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
        # _start_debug always parses this tab's own live buffer from a temp
        # file (tab._last_parse_path), so checkpoints in its own top-level
        # code report *that* as origin, not tab.file_path -- accept either
        # as "this tab" (same identity mismatch _go_to_definition has).
        identities = [p for p in (tab.file_path, getattr(tab, '_last_parse_path', None)) if p]
        is_current_tab = not origin or not identities or any(
            str(Path(origin).resolve()) == str(Path(p).resolve()) for p in identities
        )
        if is_current_tab:
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
            try:
                self._viewport.load_geometry(partial_bodies)
            except Exception as e:
                # Never let a live partial-render display failure abort the
                # rest of the pause UI update (pane state, execution-line
                # highlight, locals) — it's a best-effort preview.
                self.log(f"WARNING: live partial render failed to display: {e}")
        self._debugger_pane.set_paused(line, all_frame_locals, call_stack, origin=origin,
                                       partial_error=partial_error, main_file=self._debug_tab.file_path or "",
                                       parse_path=getattr(self._debug_tab, '_last_parse_path', None) or "")
        innermost = all_frame_locals[0] if all_frame_locals else {}
        locals_dict = {**innermost.get("outer_scope", {}), **innermost.get("local_scope", {})}
        # Only apply $vp* values the script itself assigned (dyn_explicit) --
        # local_scope also carries $vp* pre-seeded from the camera position
        # at render start (see _viewport_params), which would otherwise
        # "snap back" the camera to that stale position on every single
        # pause, undoing anything the user manually orbited/panned/zoomed
        # to while paused between steps.
        explicit = innermost.get("dyn_explicit", set())
        self._apply_vp_params({k: locals_dict[k] for k in ("$vpt", "$vpr", "$vpd", "$vpf")
                                if k in explicit and k in locals_dict})
        self._show_debug_line(origin, line)
        self._set_debug_locals_on_visible(locals_dict)
        self._ai_debug_last_pause = self._ai_frame_state(
            origin, line, all_frame_locals, call_stack)
        self._ai_debug_notify(self._ai_debug_last_pause)

    def _on_debug_error_break(self, origin: str, line: int, msg: str, all_frame_locals: list, call_stack: list,
                              partial_bodies=None, partial_error: str | None = None):
        if not self._debug_tab:
            return
        self._set_debug_busy(False)
        if partial_bodies is not None:
            try:
                self._viewport.load_geometry(partial_bodies)
            except Exception as e:
                self.log(f"WARNING: live partial render failed to display: {e}")
        self._debugger_pane.set_error_break(line, msg, all_frame_locals, call_stack, origin=origin,
                                            partial_error=partial_error, main_file=self._debug_tab.file_path or "",
                                            parse_path=getattr(self._debug_tab, '_last_parse_path', None) or "")
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
        from openscad_cpp_evaluator import to_renderable_bodies

        tab = self._debug_tab
        if not tab:
            return
        self._set_debug_busy(False)
        self._ai_debug_last_pause = None
        self._ai_debug_notify({"status": "finished",
                               "message": "the script ran to completion."})
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
            import numpy as np
            mins, maxs = [], []
            for b in bodies:
                if b.body.is_empty():
                    continue
                v = np.asarray(b.body.to_mesh().vert_properties[:, :3])
                mins.append(v.min(axis=0))
                maxs.append(v.max(axis=0))
            if mins:
                bb_min = np.min(mins, axis=0).astype(np.float32)
                bb_max = np.max(maxs, axis=0).astype(np.float32)
                self._viewport.frame_scene(bb_min, bb_max)
                self.log("Debug: completed.")
        except Exception:
            pass

    def _on_debug_error(self, msg: str):
        error_tab = self._debug_tab
        self._set_debug_busy(False)
        self._ai_debug_last_pause = None
        self._ai_debug_notify({"status": "error", "message": msg})
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

    def _on_debug_step_to_child(self):
        if not self._debug_session:
            return
        mods = self._debugger_pane.get_modifications()
        self._clear_all_debug_locals()
        self._clear_all_execution_lines()
        self._debugger_pane.set_running()
        self._set_debug_busy(True)
        self._debug_session.resume("step_to_child", mods)

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
        # Non-modal singleton window, same convention as
        # _open_library_manager -- PreferencesDialog used to be opened via
        # dialog.exec(), which (see PreferencesDialog's own docstring)
        # suppressed View-menu shortcuts for any Viewport nested inside it
        # (namely the Color Theme Manager's live preview) and, separately,
        # left the main viewport visibly black after the modal chain
        # closed on macOS -- both symptoms of the same underlying Qt
        # modal-shortcut/repaint suppression, fixed by dropping modality
        # entirely rather than working around either symptom individually.
        if not hasattr(self, '_preferences_dialog') or self._preferences_dialog is None:
            self._preferences_dialog = PreferencesDialog(parent=self, on_change=self._apply_preferences)
            self._preferences_dialog.destroyed.connect(
                lambda: setattr(self, '_preferences_dialog', None)
            )
        self._preferences_dialog.show()
        self._preferences_dialog.raise_()
        self._preferences_dialog.activateWindow()

    def _apply_preferences(self):
        family = load_preference("editor/fontFamily")
        size = load_preference("editor/fontSize", int)
        indent = load_preference("editor/indentSize", int)
        show_guide = load_preference("editor/showColumnGuide", bool)
        guide_col = load_preference("editor/columnGuide", int)
        viewer_ipd = load_preference("viewport/viewerIPD", float)
        viewer_screen_dist = load_preference("viewport/viewerScreenDist", float)
        stereo_depth_scale = load_preference("viewport/stereoDepthScale", float)
        theme = all_themes().get(load_preference("viewport/colorTheme"), COLOR_THEMES[DEFAULT_COLOR_THEME])
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
            vp._renderer.bg_color = theme["background"]
            vp._renderer._default_color = theme["object"]
            vp._renderer.axes_color = theme["axes"]
            vp._renderer.unselected_vertex_color = theme["unselected_vertex"]
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
        bottom_h = max(140, h // 6)
        right_w = max(250, w // 4)
        # editor dock: left ~30% of window width
        self.resizeDocks([self._editor_dock], [max(300, w * 3 // 10)], Qt.Orientation.Horizontal)
        # right dock: ~25% of window width
        self.resizeDocks([self._debugger_dock], [right_w], Qt.Orientation.Horizontal)
        # bottom dock area: ~17% of window height
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
            if tab and not self._confirm_unsaved(tab):
                event.ignore()
                return
        # Stop animation playback and any pending debounced Customizer
        # render (no more renders get queued) and let any in-flight render
        # thread finish — Qt aborts if a QThread is destroyed while still
        # running.
        self._animate_pane.pause()
        self._customizer_render_timer.stop()
        if self._render_cancel is not None:
            self._render_cancel.set()
        deadline = time.monotonic() + 5.0
        while any(t.isRunning() for _, _, t in self._render_jobs) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)

        if self.persist_settings:
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

    def _reset_panel_layout(self):
        """Put the docks back where they started.

        There is a saved layout and a _LAYOUT_VERSION that quietly discards
        it when the version moves on, but nothing the user could reach --
        so a layout that ended up wrong (a dock torn off, a stray tab bar)
        could only be fixed by editing preferences by hand.
        """
        if QMessageBox.question(
                self, "Reset Panel Layout",
                "Put all panels back to their default positions?",
                QMessageBox.StandardButton.Reset
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Reset
        ) != QMessageBox.StandardButton.Reset:
            return
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        s.remove("windowState")
        s.remove("windowGeometry")
        s.remove("layoutVersion")
        s.sync()
        # Applied by restoreState on the next launch: rebuilding the dock
        # arrangement live would mean re-adding every dock in order, and
        # getting that subtly wrong is what this action exists to undo.
        QMessageBox.information(
            self, "Reset Panel Layout",
            "Panel layout will be back to its defaults next time "
            "BelfrySCAD starts.")

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
        vp.refresh_perspective_icon()
        vp.update()

    def _on_viewport_perspective_toggled(self, perspective: bool):
        """The viewport's own upper-left toggle button was clicked --
        mirror the new state into the View menu's "Perspective" checkbox
        without re-triggering _toggle_perspective (which would just
        redundantly reassign camera.orthographic to the value the button
        already set)."""
        self._act_perspective.blockSignals(True)
        self._act_perspective.setChecked(perspective)
        self._act_perspective.blockSignals(False)

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
        self._target_viewport().zoom(direction)

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
