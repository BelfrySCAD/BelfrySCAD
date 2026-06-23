"""
Debugger session (runs evaluator in a worker thread) and the debugger pane widget.
"""
import os
import threading
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QTableWidget, QTableWidgetItem, QPushButton,
    QLabel, QHeaderView, QAbstractItemView, QComboBox, QCheckBox,
    QMenu,
)
from PySide6.QtGui import QFont, QIcon, QPalette
from PySide6.QtCore import Qt, QObject, Signal

_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"


def _debug_icon(name: str) -> QIcon:
    path = _ICONS_DIR / f"debug-{name}.svg"
    return QIcon(str(path)) if path.exists() else QIcon()


def _fmt(v) -> str:
    if v is None:
        return "undef"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, str):
        return f'"{v}"'
    from neuscad.engine.evaluator import OscObject
    if isinstance(v, OscObject):
        inner = ", ".join(f"{k} = {_fmt(val)}" for k, val in v.items())
        return f"object({inner})"
    return str(v)


def _is_hidden(name: str) -> bool:
    return name.startswith('_') or name.startswith('$_')


def _var_category(name: str, local_names: set) -> str:
    """Return 'specials', 'constants', 'locals', or 'globals'."""
    if name.startswith('$'):
        return 'specials'
    if name.isupper() and any(c.isalpha() for c in name):
        return 'constants'
    if name in local_names:
        return 'locals'
    return 'globals'


def _filtered_vars(frame_data: dict, category: str, show_hidden: bool) -> dict:
    local_scope = frame_data.get("local_scope", {})
    outer_scope = frame_data.get("outer_scope", {})
    local_names = set(local_scope.keys())
    all_vars = {**outer_scope, **local_scope}   # local overrides outer on name collision
    result = {}
    for name, value in all_vars.items():
        if _is_hidden(name) and not show_hidden:
            continue
        if _var_category(name, local_names) == category:
            result[name] = value
    return result


def _pretty_fmt_value(value, indent: int = 0) -> str | None:
    """Format OscObject values with multi-line layout. Returns None for other types."""
    from neuscad.engine.evaluator import OscObject
    if isinstance(value, OscObject):
        if not value.data:
            return "object()"
        pad = " " * (indent + 4)
        lines = [f"{pad}{k} = {_pretty_fmt_value(v, indent + 4) or _fmt(v)}" for k, v in value.items()]
        return "object(\n" + ",\n".join(lines) + "\n" + " " * indent + ")"
    return None


def _pretty_assignment(name: str, value) -> str:
    """Format ``name = value;`` using the parser's pretty-printer for complex values."""
    obj_fmt = _pretty_fmt_value(value)
    if obj_fmt is not None:
        return f"{name} = {obj_fmt};"

    from openscad_lalr_parser.nodes import (
        Assignment, ListComprehension, NumberLiteral, BooleanLiteral,
        StringLiteral, UndefinedLiteral, Position, Identifier,
    )
    from openscad_lalr_parser import to_openscad

    p = Position(0, 0, 0, 0, '')

    def _to_ast(v):
        if v is None:
            return UndefinedLiteral(p)
        if isinstance(v, bool):
            return BooleanLiteral(p, v)
        if isinstance(v, (int, float)):
            return NumberLiteral(p, float(v))
        if isinstance(v, str):
            return StringLiteral(p, v)
        if isinstance(v, list):
            return ListComprehension(p, [_to_ast(x) for x in v])
        return StringLiteral(p, str(v))

    node = Assignment(p, Identifier(p, name), _to_ast(value))
    return to_openscad([node]).rstrip('\n')


def _parse_val(s: str):
    """Lossily parse a user-edited value string back to a Python value. Returns None on failure."""
    s = s.strip()
    if s == "undef":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        return float(s)
    except ValueError:
        pass
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return None


class DebugSession(QObject):
    """Runs the evaluator in a daemon worker thread, pausing at breakpoints."""

    # All emitted from the worker thread — PySide6 queues these to the main thread.
    paused = Signal(str, int, object, object)       # origin, line, all_frame_locals (list, innermost first), call_stack
    error_break = Signal(str, int, str, object, object)  # origin, line, error header, all_frame_locals, call_stack
    finished = Signal(object, object)          # bodies, id_to_node
    errored = Signal(str)
    logged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pause_event = threading.Event()
        self._resume_command = "continue"
        self._pending_mods: dict = {}
        self._breakpoints: dict[str, set[int]] = {}
        # Step-mode flags (all mutually exclusive, cleared on each pause)
        self._step_mode: bool = False          # step_into: pause at very next statement
        self._step_over_depth: int | None = None  # step_over: pause when depth ≤ N
        self._step_out_depth: int | None = None   # step_out:  pause when call depth < N
        self._step_out_expr_depth: int | None = None  # step_out inside expr: pause when expr_depth ≤ N
        self._current_pause_expr_depth: int = 0
        self._stopped: bool = False
        self._pause_requested: bool = False
        self._thread: threading.Thread | None = None

    def start(self, nodes, root_scope, breakpoints: dict[str, set[int]], viewport_params: dict | None = None, current_file: str | None = None):
        self._current_file = os.path.realpath(current_file) if current_file else None
        self._breakpoints = dict(breakpoints)
        self._step_mode = False
        self._step_over_depth = None
        self._step_out_depth = None
        self._step_out_expr_depth = None
        self._current_pause_expr_depth = 0
        self._stopped = False
        self._pause_requested = False
        self._pending_mods = {}
        self._thread = threading.Thread(
            target=self._run, args=(nodes, root_scope, viewport_params or {}), daemon=True
        )
        self._thread.start()

    def _make_hook(self):
        _resolve_cache: dict[str, str] = {}
        _realpath = os.path.realpath
        _current = self._current_file or ""
        def _resolve(origin):
            if not origin:
                return _current
            cached = _resolve_cache.get(origin)
            if cached is not None:
                return cached
            resolved = _realpath(origin)
            _resolve_cache[origin] = resolved
            return resolved

        def hook(line: int, depth: int, *, forced: bool = False, expr_level: bool = False, expr_depth: int = 0, origin: str | None = None, get_frames=None) -> tuple[str, dict]:
            if self._stopped:
                return ("stop", {})

            resolved_origin = _resolve(origin)
            pause_now = self._pause_requested
            if pause_now:
                self._pause_requested = False
            should_pause = (
                forced
                or pause_now
                or (line in self._breakpoints.get(resolved_origin, set()) and not expr_level)
                or self._step_mode
                or (self._step_over_depth is not None and depth <= self._step_over_depth and not expr_level)
                or (self._step_out_depth is not None and depth < self._step_out_depth and not expr_level)
                or (self._step_out_expr_depth is not None and expr_depth <= self._step_out_expr_depth)
            )

            if not should_pause:
                return ("continue", {})

            # Clear all step state before pausing
            self._step_mode = False
            self._step_over_depth = None
            self._step_out_depth = None
            self._step_out_expr_depth = None
            self._current_pause_expr_depth = expr_depth

            (locals_dict, all_frame_locals), call_stack = get_frames()
            display_stack = [("toplevel", "<toplevel>", None)] + list(call_stack)
            self.paused.emit(origin or "", line, list(all_frame_locals), display_stack)
            self._pause_event.clear()
            self._pause_event.wait()

            if self._stopped:
                return ("stop", {})

            cmd = self._resume_command
            mods = dict(self._pending_mods)
            self._pending_mods.clear()

            if cmd == "step_into":
                self._step_mode = True
            elif cmd == "step_over":
                self._step_over_depth = depth
            elif cmd == "step_out":
                if self._current_pause_expr_depth > 0:
                    self._step_out_expr_depth = self._current_pause_expr_depth - 1
                else:
                    self._step_out_depth = depth

            return ("continue", mods)
        return hook

    def _error_break(self, line: int, msg: str, all_frame_locals: list, call_stack: list, origin: str | None = None):
        """Called by the evaluator on any runtime error in debug mode.
        Pauses so the user can inspect state; returns when the user resumes.
        The EvalError is raised by the evaluator after this returns.
        """
        if self._stopped:
            return
        display_stack = [("toplevel", "<toplevel>", None)] + list(call_stack)
        self.error_break.emit(origin or "", line, msg, list(all_frame_locals), display_stack)
        self._pause_event.clear()
        self._pause_event.wait()

    def _run(self, nodes, root_scope, viewport_params: dict):
        from neuscad.engine.evaluator import Evaluator, EvalError
        ev = Evaluator(echo_fn=self.logged.emit, debug_hook=self._make_hook(), error_break_fn=self._error_break)
        try:
            bodies, id_to_node = ev.evaluate(nodes, root_scope, viewport_params)
            if not self._stopped:
                self.finished.emit(bodies, id_to_node)
        except EvalError as e:
            if not self._stopped:
                self.errored.emit(str(e))
        except Exception as e:
            import traceback
            if not self._stopped:
                self.errored.emit(f"{e}\n{traceback.format_exc()}")

    def pause(self):
        """Request the evaluator to pause at the next debug hook call."""
        self._pause_requested = True

    def resume(self, command: str = "continue", mods: dict | None = None):
        self._resume_command = command
        self._pending_mods = dict(mods) if mods else {}
        if command == "stop":
            self._stopped = True
        self._pause_event.set()

    def stop(self):
        self._stopped = True
        self._pause_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class DebuggerPane(QWidget):
    """Pane showing call stack, local variables, and step controls."""

    continue_requested = Signal()
    pause_requested = Signal()
    step_into_requested = Signal()
    step_over_requested = Signal()
    step_out_requested = Signal()
    restart_requested = Signal()
    stop_requested = Signal()
    print_to_console = Signal(str)
    frame_selected = Signal(str, int)  # file_path, line

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_locals: dict[str, str] = {}
        self._all_frame_locals: list[dict] = []
        self._stack_positions: list[tuple[str, int]] = []  # (file_path, line) per row
        self._innermost_row: int = 0  # row index of the innermost (current) frame
        self._is_running: bool = False
        self._setup_ui()
        self.setMinimumWidth(180)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._btn_continue = QPushButton()
        self._btn_continue.setIcon(_debug_icon("continue"))
        self._btn_continue.setToolTip("Continue (F5)")
        self._btn_continue.setFixedSize(28, 28)
        self._btn_step_over = QPushButton()
        self._btn_step_over.setIcon(_debug_icon("step-over"))
        self._btn_step_over.setToolTip("Step Over (F10)")
        self._btn_step_over.setFixedSize(28, 28)
        self._btn_step_into = QPushButton()
        self._btn_step_into.setIcon(_debug_icon("step-into"))
        self._btn_step_into.setToolTip("Step Into (F11)")
        self._btn_step_into.setFixedSize(28, 28)
        self._btn_step_out = QPushButton()
        self._btn_step_out.setIcon(_debug_icon("step-out"))
        self._btn_step_out.setToolTip("Step Out (F12)")
        self._btn_step_out.setFixedSize(28, 28)
        self._btn_restart = QPushButton()
        self._btn_restart.setIcon(_debug_icon("restart"))
        self._btn_restart.setToolTip("Restart")
        self._btn_restart.setFixedSize(28, 28)
        self._btn_stop = QPushButton()
        self._btn_stop.setIcon(_debug_icon("stop"))
        self._btn_stop.setToolTip("Stop")
        self._btn_stop.setFixedSize(28, 28)

        for btn in (self._btn_continue, self._btn_step_over, self._btn_step_into,
                    self._btn_step_out, self._btn_stop, self._btn_restart):
            btn.setFlat(True)
            btn.setEnabled(btn is self._btn_restart)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter = self._splitter

        mono = QFont("Menlo", 10)

        stack_widget = QWidget()
        sv = QVBoxLayout(stack_widget)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(2)
        stack_header = QHBoxLayout()
        stack_header.addWidget(QLabel("Call Stack"))
        stack_header.addStretch()
        for btn in (self._btn_continue, self._btn_step_over, self._btn_step_into,
                    self._btn_step_out, self._btn_stop, self._btn_restart):
            stack_header.addWidget(btn)
        sv.addLayout(stack_header)
        self._stack_list = QListWidget()
        self._stack_list.setFont(mono)
        # Keep active highlight color even when the list loses keyboard focus.
        pal = self._stack_list.palette()
        pal.setColor(QPalette.ColorGroup.Inactive,
                     QPalette.ColorRole.Highlight,
                     pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight))
        pal.setColor(QPalette.ColorGroup.Inactive,
                     QPalette.ColorRole.HighlightedText,
                     pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText))
        self._stack_list.setPalette(pal)
        sv.addWidget(self._stack_list)
        splitter.addWidget(stack_widget)

        vars_widget = QWidget()
        vv = QVBoxLayout(vars_widget)
        vv.setContentsMargins(0, 0, 0, 0)
        vv.setSpacing(0)
        vars_header = QHBoxLayout()
        vars_header.setContentsMargins(0, 0, 0, 0)
        self._filter_combo = QComboBox()
        self._filter_combo.addItem("Local Variables",   "locals")
        self._filter_combo.addItem("Global Variables",  "globals")
        self._filter_combo.addItem("$Special Variables","specials")
        self._filter_combo.addItem("CONSTANTS",         "constants")
        self._filter_combo.setCurrentIndex(0)
        vars_header.addWidget(self._filter_combo)
        vars_header.addStretch()
        self._hidden_check = QCheckBox("Hiddens")
        vars_header.addWidget(self._hidden_check)
        vv.addLayout(vars_header)
        self._vars_table = QTableWidget(0, 2)
        self._vars_table.setFont(mono)
        self._vars_table.setHorizontalHeaderLabels(["Name", "Value"])
        from neuscad.window.data_viewers import _style_table_headers
        _style_table_headers(self._vars_table)
        self._vars_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._vars_table.horizontalHeader().setStretchLastSection(True)
        self._vars_table.horizontalHeader().resizeSection(0, 120)
        self._vars_table.verticalHeader().setVisible(False)
        self._vars_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._vars_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._vars_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._vars_table.customContextMenuRequested.connect(self._on_vars_context_menu)
        vv.addWidget(self._vars_table)
        splitter.addWidget(vars_widget)

        layout.addWidget(splitter)

        self._status = QLabel("Not debugging")
        layout.addWidget(self._status)

        self._btn_continue.clicked.connect(self._on_continue_pause_clicked)
        self._btn_step_into.clicked.connect(self.step_into_requested)
        self._btn_step_over.clicked.connect(self.step_over_requested)
        self._btn_step_out.clicked.connect(self.step_out_requested)
        self._btn_restart.clicked.connect(self.restart_requested)
        self._btn_stop.clicked.connect(self.stop_requested)
        self._stack_list.currentRowChanged.connect(self._on_frame_selected)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._hidden_check.toggled.connect(self._on_filter_changed)

        # Seed the initial <toplevel> entry so the list is never empty.
        self._stack_list.addItem("<toplevel>")
        self._stack_list.setCurrentRow(0)

    def set_splitter_orientation(self, orientation: Qt.Orientation):
        self._splitter.setOrientation(orientation)

    def _on_continue_pause_clicked(self):
        if self._is_running:
            self.pause_requested.emit()
        else:
            self.continue_requested.emit()

    def get_modifications(self) -> dict:
        """Return variable values the user edited in the innermost-frame vars table."""
        mods = {}
        for row in range(self._vars_table.rowCount()):
            name_item = self._vars_table.item(row, 0)
            val_item = self._vars_table.item(row, 1)
            if name_item and val_item:
                if not (val_item.flags() & Qt.ItemFlag.ItemIsEditable):
                    continue  # parent-frame rows are read-only; skip
                name = name_item.text()
                new_str = val_item.text()
                if new_str != self._original_locals.get(name, ""):
                    parsed = _parse_val(new_str)
                    if parsed is not None:
                        mods[name] = parsed
        return mods

    def _populate_stack(self, call_stack: list, pause_origin: str = "", pause_line: int = 0):
        self._stack_list.blockSignals(True)
        self._stack_list.clear()
        self._stack_positions.clear()
        # call_stack is [toplevel, outermost, ..., innermost].
        # Navigation: each entry shows where it calls the next;
        # the innermost (last) entry shows the current pause point.
        non_toplevel = [e for e in call_stack if e[0] != "toplevel"]
        for i, entry in enumerate(call_stack):
            if entry[0] == "toplevel":
                self._stack_list.addItem("<toplevel>")
                if non_toplevel:
                    cp = non_toplevel[0][2]  # call_pos of first callee
                    self._stack_positions.append((
                        getattr(cp, 'origin', '') or '',
                        int(getattr(cp, 'line', 0)) if cp else 0))
                else:
                    self._stack_positions.append(("", 0))
            else:
                name = entry[1]
                decl_pos = entry[3] if len(entry) > 3 else None
                decl_line = str(getattr(decl_pos, 'line', '?')) if decl_pos is not None else '?'
                decl_origin = getattr(decl_pos, 'origin', '') if decl_pos is not None else ''
                file_str = os.path.basename(decl_origin) if decl_origin else ''
                if file_str:
                    self._stack_list.addItem(f"{name}()  {file_str}:{decl_line}")
                else:
                    self._stack_list.addItem(f"{name}()  line {decl_line}")
                # Find the next non-toplevel entry after this one
                next_idx = i + 1
                next_entry = call_stack[next_idx] if next_idx < len(call_stack) and call_stack[next_idx][0] != "toplevel" else None
                if next_entry is not None:
                    cp = next_entry[2]  # call_pos of callee
                    self._stack_positions.append((
                        getattr(cp, 'origin', '') or '',
                        int(getattr(cp, 'line', 0)) if cp else 0))
                else:
                    self._stack_positions.append((pause_origin, pause_line))
        if self._stack_list.count() > 0:
            row = min(self._innermost_row, self._stack_list.count() - 1)
            self._stack_list.setCurrentRow(row)
        self._stack_list.blockSignals(False)

    def _populate_vars(self, frame_data: dict, is_innermost: bool = False):
        category = self._filter_combo.currentData()
        show_hidden = self._hidden_check.isChecked()
        data = _filtered_vars(frame_data, category, show_hidden) if isinstance(frame_data, dict) else {}
        dyn_names = frame_data.get("dyn_names", set()) if isinstance(frame_data, dict) else set()
        self._vars_table.setRowCount(0)
        for name in sorted(data):
            row = self._vars_table.rowCount()
            self._vars_table.insertRow(row)
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._vars_table.setItem(row, 0, name_item)
            val_item = QTableWidgetItem(_fmt(data[name]))
            if not (is_innermost and category == "locals" and name in dyn_names):
                val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._vars_table.setItem(row, 1, val_item)

    def _on_frame_selected(self, row: int):
        if row < 0 or row >= len(self._all_frame_locals):
            return
        self._populate_vars(self._all_frame_locals[row], is_innermost=(row == self._innermost_row))
        if row < len(self._stack_positions):
            fp, ln = self._stack_positions[row]
            if fp and ln:
                self.frame_selected.emit(fp, ln)

    def _on_filter_changed(self, _=None):
        row = self._stack_list.currentRow()
        if row < 0:
            row = 0
        if row < len(self._all_frame_locals):
            self._populate_vars(self._all_frame_locals[row], is_innermost=(row == self._innermost_row))

    def _on_vars_context_menu(self, pos):
        item = self._vars_table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        name_item = self._vars_table.item(row, 0)
        if name_item is None:
            return
        name = name_item.text()
        frame_row = self._stack_list.currentRow()
        if frame_row < 0 or frame_row >= len(self._all_frame_locals):
            return
        frame = self._all_frame_locals[frame_row]
        all_vars = {**frame.get("outer_scope", {}), **frame.get("local_scope", {})}
        if name not in all_vars:
            return
        value = all_vars[name]
        menu = QMenu(self)
        menu.addAction("Print to Console",
                       lambda: self.print_to_console.emit(_pretty_assignment(name, value)))
        from neuscad.window.data_viewers import build_viewer_menu
        build_viewer_menu(menu, name, value, self)
        menu.exec(self._vars_table.viewport().mapToGlobal(pos))

    def set_paused(self, line: int, all_frame_locals: list, call_stack: list, origin: str = ""):
        self._set_continue_mode()
        self._status.setText(f"Paused at line {line}")
        # Reorder to match display: [toplevel, outermost, ..., innermost]
        if len(all_frame_locals) > 1:
            self._all_frame_locals = list(reversed(all_frame_locals))
            self._innermost_row = len(self._all_frame_locals) - 1
        else:
            self._all_frame_locals = list(all_frame_locals)
            self._innermost_row = 0
        innermost = all_frame_locals[0] if all_frame_locals else {}
        dyn_names = innermost.get("dyn_names", set())
        self._original_locals = {k: _fmt(v) for k, v in innermost.get("local_scope", {}).items()
                                  if k in dyn_names}
        self._populate_stack(call_stack, pause_origin=origin, pause_line=line)
        self._populate_vars(self._all_frame_locals[self._innermost_row], is_innermost=True)
        for btn in (self._btn_continue, self._btn_step_into, self._btn_step_over,
                    self._btn_step_out, self._btn_restart, self._btn_stop):
            btn.setEnabled(True)

    def set_error_break(self, line: int, msg: str, all_frame_locals: list, call_stack: list, origin: str = ""):
        self._set_continue_mode()
        display = msg.removeprefix("ERROR: ")
        if len(display) > 80:
            display = display[:77] + "…"
        self._status.setText(f"Line {line}: {display}")
        if len(all_frame_locals) > 1:
            self._all_frame_locals = list(reversed(all_frame_locals))
            self._innermost_row = len(self._all_frame_locals) - 1
        else:
            self._all_frame_locals = list(all_frame_locals)
            self._innermost_row = 0
        self._original_locals = {}
        self._populate_stack(call_stack, pause_origin=origin, pause_line=line)
        self._populate_vars(self._all_frame_locals[self._innermost_row])
        self._btn_continue.setEnabled(True)
        self._btn_step_into.setEnabled(False)
        self._btn_step_over.setEnabled(False)
        self._btn_step_out.setEnabled(False)
        self._btn_restart.setEnabled(True)
        self._btn_stop.setEnabled(True)

    def _set_continue_mode(self):
        self._is_running = False
        self._btn_continue.setIcon(_debug_icon("continue"))
        self._btn_continue.setToolTip("Continue (F5)")

    def set_running(self):
        self._is_running = True
        self._status.setText("Running…")
        self._btn_continue.setIcon(_debug_icon("pause"))
        self._btn_continue.setToolTip("Pause")
        self._btn_continue.setEnabled(True)
        for btn in (self._btn_step_into, self._btn_step_over, self._btn_step_out):
            btn.setEnabled(False)
        self._btn_restart.setEnabled(True)
        self._btn_stop.setEnabled(True)

    def set_idle(self):
        self._set_continue_mode()
        self._status.setText("Not debugging")
        for btn in (self._btn_continue, self._btn_step_into, self._btn_step_over,
                    self._btn_step_out, self._btn_stop):
            btn.setEnabled(False)
        self._btn_restart.setEnabled(True)
        self._stack_list.blockSignals(True)
        self._stack_list.clear()
        self._stack_list.addItem("<toplevel>")
        self._stack_list.setCurrentRow(0)
        self._stack_list.blockSignals(False)
        self._vars_table.setRowCount(0)
