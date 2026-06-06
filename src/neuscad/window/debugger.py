"""
Debugger session (runs evaluator in a worker thread) and the debugger pane widget.
"""
import threading
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QTableWidget, QTableWidgetItem, QPushButton,
    QLabel, QHeaderView, QAbstractItemView,
)
from PySide6.QtGui import QFont, QIcon
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
    return str(v)


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
    paused = Signal(int, object, object)   # line (1-indexed), locals dict, call_stack list
    finished = Signal(object, object)      # bodies, id_to_node
    errored = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pause_event = threading.Event()
        self._resume_command = "continue"
        self._pending_mods: dict = {}
        self._breakpoints: set[int] = set()
        # Step-mode flags (all mutually exclusive, cleared on each pause)
        self._break_on_first: bool = False
        self._step_mode: bool = False          # step_into: pause at very next statement
        self._step_over_depth: int | None = None  # step_over: pause when depth ≤ N
        self._step_out_depth: int | None = None   # step_out:  pause when depth < N
        self._stopped: bool = False
        self._thread: threading.Thread | None = None

    def start(self, nodes, root_scope, breakpoints: set[int], echo_fn):
        self._breakpoints = set(breakpoints)
        self._break_on_first = True   # always pause at the very first statement
        self._step_mode = False
        self._step_over_depth = None
        self._step_out_depth = None
        self._stopped = False
        self._pending_mods = {}
        self._thread = threading.Thread(
            target=self._run, args=(nodes, root_scope, echo_fn), daemon=True
        )
        self._thread.start()

    def _make_hook(self):
        def hook(line: int, locals_dict: dict, call_stack: list) -> tuple[str, dict]:
            if self._stopped:
                return ("stop", {})

            depth = len(call_stack)
            should_pause = (
                self._break_on_first
                or (line in self._breakpoints)
                or self._step_mode
                or (self._step_over_depth is not None and depth <= self._step_over_depth)
                or (self._step_out_depth is not None and depth < self._step_out_depth)
            )

            if not should_pause:
                return ("continue", {})

            # Clear all step state before pausing
            self._break_on_first = False
            self._step_mode = False
            self._step_over_depth = None
            self._step_out_depth = None

            self.paused.emit(line, dict(locals_dict), list(call_stack))
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
                self._step_out_depth = depth
            # "continue" leaves all flags cleared

            return ("continue", mods)   # evaluator only needs to know about "stop"
        return hook

    def _run(self, nodes, root_scope, echo_fn):
        from neuscad.engine.evaluator import Evaluator, EvalError
        ev = Evaluator(echo_fn=echo_fn, debug_hook=self._make_hook())
        try:
            bodies, id_to_node = ev.evaluate(nodes, root_scope)
            if not self._stopped:
                self.finished.emit(bodies, id_to_node)
        except EvalError as e:
            if not self._stopped:
                self.errored.emit(str(e))
        except Exception as e:
            import traceback
            if not self._stopped:
                self.errored.emit(f"{e}\n{traceback.format_exc()}")

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
    step_into_requested = Signal()
    step_over_requested = Signal()
    step_out_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_locals: dict[str, str] = {}
        self._setup_ui()
        self.setMinimumWidth(180)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("Debugger"))
        header.addStretch()

        self._btn_continue = QPushButton()
        self._btn_continue.setIcon(_debug_icon("continue"))
        self._btn_continue.setToolTip("Continue (F5)")
        self._btn_continue.setFixedSize(28, 28)
        self._btn_step_over = QPushButton()
        self._btn_step_over.setIcon(_debug_icon("step-over"))
        self._btn_step_over.setToolTip("Step Over (F10)")
        self._btn_step_over.setFixedSize(28, 24)
        self._btn_step_into = QPushButton()
        self._btn_step_into.setIcon(_debug_icon("step-into"))
        self._btn_step_into.setToolTip("Step Into (F11)")
        self._btn_step_into.setFixedSize(28, 24)
        self._btn_step_out = QPushButton()
        self._btn_step_out.setIcon(_debug_icon("step-out"))
        self._btn_step_out.setToolTip("Step Out (F12)")
        self._btn_step_out.setFixedSize(28, 24)
        self._btn_stop = QPushButton()
        self._btn_stop.setIcon(_debug_icon("stop"))
        self._btn_stop.setToolTip("Stop")
        self._btn_stop.setFixedSize(28, 24)

        for btn in (self._btn_continue, self._btn_step_over, self._btn_step_into,
                    self._btn_step_out, self._btn_stop):
            header.addWidget(btn)
            btn.setEnabled(False)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)

        mono = QFont("Menlo", 10)

        stack_widget = QWidget()
        sv = QVBoxLayout(stack_widget)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(QLabel("Call Stack"))
        self._stack_list = QListWidget()
        self._stack_list.setFont(mono)
        sv.addWidget(self._stack_list)
        splitter.addWidget(stack_widget)

        vars_widget = QWidget()
        vv = QVBoxLayout(vars_widget)
        vv.setContentsMargins(0, 0, 0, 0)
        vv.addWidget(QLabel("Variables"))
        self._vars_table = QTableWidget(0, 2)
        self._vars_table.setFont(mono)
        self._vars_table.setHorizontalHeaderLabels(["Name", "Value"])
        self._vars_table.horizontalHeader().setStretchLastSection(True)
        self._vars_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._vars_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._vars_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        vv.addWidget(self._vars_table)
        splitter.addWidget(vars_widget)

        layout.addWidget(splitter)

        self._status = QLabel("Not debugging")
        layout.addWidget(self._status)

        self._btn_continue.clicked.connect(self.continue_requested)
        self._btn_step_into.clicked.connect(self.step_into_requested)
        self._btn_step_over.clicked.connect(self.step_over_requested)
        self._btn_step_out.clicked.connect(self.step_out_requested)
        self._btn_stop.clicked.connect(self.stop_requested)

    def get_modifications(self) -> dict:
        """Return any variable values the user edited in the table."""
        mods = {}
        for row in range(self._vars_table.rowCount()):
            name_item = self._vars_table.item(row, 0)
            val_item = self._vars_table.item(row, 1)
            if name_item and val_item:
                name = name_item.text()
                new_str = val_item.text()
                if new_str != self._original_locals.get(name, ""):
                    parsed = _parse_val(new_str)
                    if parsed is not None:
                        mods[name] = parsed
        return mods

    def set_paused(self, line: int, locals_dict: dict, call_stack: list):
        self._status.setText(f"Paused at line {line}")
        self._original_locals = {k: _fmt(v) for k, v in locals_dict.items()}

        self._stack_list.clear()
        for fname, pos in reversed(call_stack):
            if pos is not None:
                self._stack_list.addItem(f"{fname}()  line {getattr(pos, 'line', '?')}")
            else:
                self._stack_list.addItem(f"{fname}()")

        self._vars_table.setRowCount(0)
        for name in sorted(locals_dict):
            row = self._vars_table.rowCount()
            self._vars_table.insertRow(row)
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._vars_table.setItem(row, 0, name_item)
            self._vars_table.setItem(row, 1, QTableWidgetItem(_fmt(locals_dict[name])))

        for btn in (self._btn_continue, self._btn_step_into, self._btn_step_over,
                    self._btn_step_out, self._btn_stop):
            btn.setEnabled(True)

    def set_running(self):
        self._status.setText("Running…")
        for btn in (self._btn_continue, self._btn_step_into, self._btn_step_over, self._btn_step_out):
            btn.setEnabled(False)
        self._btn_stop.setEnabled(True)

    def set_idle(self):
        self._status.setText("Not debugging")
        for btn in (self._btn_continue, self._btn_step_into, self._btn_step_over,
                    self._btn_step_out, self._btn_stop):
            btn.setEnabled(False)
        self._stack_list.clear()
        self._vars_table.setRowCount(0)
