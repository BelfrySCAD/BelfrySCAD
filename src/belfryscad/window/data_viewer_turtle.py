"""TurtleViewer: a BOSL2 turtle() command list, as a table of commands beside
the path it draws.

The path is worked out by `belfryscad.turtle`, a port of BOSL2's own
turtle(), so the preview needs no library and no render. The viewport is the
2D path editor's, read-only: the path is derived from the commands, so it is
the commands that are edited, and selecting a row highlights the points that
command drew. The background image to trace over comes with it.
"""
from __future__ import annotations

import ast
import copy
import math

import numpy as np

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QSplitter, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from belfryscad import turtle
from belfryscad.engine.renderer import _axis_extent, _drawn_spacings
from belfryscad.window.background_image import restore_session_background
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin,
                                                  _lit_marker_triangles,
                                                  _marker_radius_for_point,
                                                  _style_table_headers,
                                                  _sync_viewport_to_main_window)
from belfryscad.window.data_viewer_path import _PathViewport


def format_turtle(value) -> str:
    """OpenSCAD source for a turtle list: `["move", 10, "arcleft", 5, 90]`."""
    if isinstance(value, list):
        return "[" + ", ".join(format_turtle(x) for x in value) + "]"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.10g}" if isinstance(value, float) else str(value)


def format_turtle_lines(commands: list, indent: str = "") -> str:
    """A turtle list as the editor writes it back: one command and its
    arguments per line, indented a step past `indent` (the line the list
    starts on), the closing bracket back at `indent`."""
    rows = turtle.split(commands)
    if not rows:
        return "[]"
    lines = [", ".join(format_turtle(x) for x in [cmd, *args]) for cmd, args in rows]
    inner = indent + "    "
    return "[\n" + ",\n".join(inner + line for line in lines) + "\n" + indent + "]"


#: Start positions moved in an editor opened on a list outside any turtle()
#: call, which has no state= to keep them: per editor title, for the session,
#: as the background image is.
_session_starts: dict = {}


def _parse_arg(text: str):
    """A cell's argument: None when blank, else a number, vector or list."""
    text = text.strip()
    if not text:
        return None
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = None
    if not isinstance(value, (int, float, list)) or isinstance(value, bool):
        raise turtle.TurtleError(f"An argument is a number or a list, not {text}")
    return value


class _CommandDelegate(QStyledItemDelegate):
    """The Command column edits with a menu of turtle commands."""

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.addItems(turtle.COMMANDS)
        box.activated.connect(lambda _i, b=box: self.commitData.emit(b))
        return box

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data())
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class _TurtleViewport(_PathViewport):
    """The path viewport, plus an arrowhead at the end of the path showing
    which way the turtle ends up facing (`heading`, set before each load),
    and a click on the path naming the segment hit: `segment_clicked(i)` for
    the segment from point i to i+1, -1 for none."""

    segment_clicked = Signal(int)
    #: The start handle: pressed, dragged to (x, y) -- "move" from the dot,
    #: "turn" from the arrow's tip -- and released.
    start_drag_started = Signal()
    start_dragged = Signal(float, float, str)
    start_drag_finished = Signal()
    #: A joint (the end of a drawing command): pressed, dragged to (x, y),
    #: released -- with whether it moved, since a press without a drag is a
    #: click that selects that command.
    joint_drag_started = Signal(int)
    joint_dragged = Signal(int, float, float)
    joint_drag_finished = Signal(int, bool)
    #: A plain click off the path: (x, y) in the model's plane.
    draw_clicked = Signal(float, float)
    #: The pointer moved, or a modifier changed: redraw the drawing preview.
    hover_changed = Signal()
    heading = None
    #: Where the turtle starts, and the way it faces there (degrees), set
    #: before each load; `start_movable` offers the handle for dragging.
    start = None
    start_angle = 0.0
    start_movable = False
    #: Called with (menu, model point or None) to add the start's actions.
    start_menu = None
    _start_drag = None
    #: The joints' points, set before each load; [] when the list has a
    #: command a drag could not refit around (`turtle.joints`).
    joints: list = []
    _joint_drag = None
    _joint_moved = False
    hover: QPoint | None = None
    _preview_lb = None

    _PREVIEW_COLOR = (0.15, 0.4, 0.95)

    def segment_at(self, pos) -> int:
        """The path segment under widget point `pos`, or -1."""
        n = len(self._path_pts)
        if n < 2:
            return -1
        hit = self._renderer.pick_nearest_segment(
            self._path_pts, [(i, i + 1) for i in range(n - 1)],
            pos.x(), pos.y(), self.width(), self.height())
        return hit[0] if hit is not None else -1

    _START_COLOR = [0.1, 0.6, 0.2]

    def _start_arrow(self):
        """(base, tip, side) of the start handle's arrow, or None."""
        if self.start is None:
            return None
        base = np.array([self.start[0], self.start[1], 0.0], dtype=np.float32)
        a = math.radians(self.start_angle)
        d = np.array([math.cos(a), math.sin(a), 0.0], dtype=np.float32)
        size = 4.0 * _marker_radius_for_point(self, base)
        return base, base + d * size, np.array([-d[1], d[0], 0.0], dtype=np.float32) * size * 0.3

    def start_handle_at(self, pos) -> str | None:
        """"move" over the start dot, "turn" over its arrow's tip, else None."""
        arrow = self._start_arrow() if self.start_movable else None
        if arrow is None:
            return None
        hit = self._renderer.pick_nearest_point(np.array(arrow[:2]), pos.x(), pos.y(),
                                                self.width(), self.height())
        return ("move", "turn")[hit] if hit >= 0 else None

    _JOINT_COLOR = np.array([0.1, 0.3, 0.85], dtype=np.float32)

    def joint_at(self, pos) -> int:
        """The joint under widget point `pos`, or -1."""
        if not self.joints:
            return -1
        return self._renderer.pick_nearest_point(
            np.array([[x, y, 0.0] for x, y in self.joints], dtype=np.float32),
            pos.x(), pos.y(), self.width(), self.height())

    def minor_step(self) -> float:
        """The spacing of the smallest scale ticks now shown: Shift's snap."""
        return _drawn_spacings(_axis_extent(self._renderer.camera))[2]

    def set_preview(self, points):
        """Show `points` (a polyline, [x, y] each) as a dashed line, or
        clear it with None. Dashes are a fixed size on screen."""
        if self._ctx is None:
            return
        self.makeCurrent()
        lb, self._preview_lb = self._preview_lb, None
        lines = self._renderer._line_buffers
        if lb is not None and lb in lines:   # a reload has already freed it otherwise
            lines.remove(lb)
            lb.vao.release()
            lb.wide_vao.release()
            lb.vbo.release()
        if points is not None and len(points) >= 2:
            pts = [np.array([p[0], p[1], 0.0]) for p in points]
            dash = 2.0 * _marker_radius_for_point(self, pts[0])
            rows, on, left = [], True, dash
            for a, b in zip(pts, pts[1:]):
                seg = float(np.linalg.norm(b - a))
                if seg == 0:
                    continue
                u, pos, rest = (b - a) / seg, a, seg
                while rest > 1e-12:
                    step = min(left, rest)
                    q = pos + u * step
                    if on:
                        rows += [[*pos, *self._PREVIEW_COLOR], [*q, *self._PREVIEW_COLOR]]
                    pos, rest, left = q, rest - step, left - step
                    if left <= 1e-12:
                        on, left = not on, dash
            if rows:
                self._preview_lb = self._renderer.upload_lines(np.array(rows, dtype=np.float32))
        self.doneCurrent()
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()
        left = event.button() == Qt.MouseButton.LeftButton
        kind = self.start_handle_at(pos) if left else None
        if kind is not None:
            self._start_drag = kind
            self.start_drag_started.emit()
            return   # a drag of the handle, not of the view
        j = self.joint_at(pos) if left else -1
        if j >= 0:
            self._joint_drag, self._joint_moved = j, False
            self.joint_drag_started.emit(j)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._start_drag is not None:
            pt = self._model_point(event.position().toPoint())
            if pt is not None:
                self.start_dragged.emit(pt[0], pt[1], self._start_drag)
            return
        if self._joint_drag is not None:
            pt = self._model_point(event.position().toPoint())
            if pt is not None:
                self._joint_moved = True
                self.joint_dragged.emit(self._joint_drag, pt[0], pt[1])
            return
        pos = event.position().toPoint()
        handle = self.start_handle_at(pos)
        self.setCursor(Qt.CursorShape.SizeAllCursor if handle == "move" or self.joint_at(pos) >= 0
                       else Qt.CursorShape.CrossCursor if handle == "turn"
                       else Qt.CursorShape.ArrowCursor)
        self.hover = event.position().toPoint() if not event.buttons() else None
        self.hover_changed.emit()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover = None
        self.hover_changed.emit()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        self.hover_changed.emit()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        self.hover_changed.emit()
        super().keyReleaseEvent(event)

    def _build_point_markers(self):
        # Built with the markers, so it is rebuilt with them on every zoom and
        # stays the same size on screen.
        super()._build_point_markers()
        arrow = self._start_arrow() if self._ctx is not None else None
        if arrow is not None:
            base, tip, side = arrow
            up = [0.0, 0.0, 1.0]
            rows = [[*p, *up, *self._START_COLOR] for p in (tip, base + side, base - side)]
            self._renderer.upload_points(np.array(rows, dtype=np.float32))
        if self.joints and self._ctx is not None:
            tris = []
            for x, y in self.joints:
                pt = np.array([x, y, 0.0], dtype=np.float32)
                tris += _lit_marker_triangles(pt, _marker_radius_for_point(self, pt),
                                              self._marker_faces(1.0), self._JOINT_COLOR)
            self._renderer.upload_points(np.array(tris, dtype=np.float32))
        if self.heading is None or len(self._path_pts) == 0 or self._ctx is None:
            return
        hx, hy = self.heading[0], self.heading[1]
        n = math.hypot(hx, hy)
        if n == 0:
            return
        d = np.array([hx / n, hy / n, 0.0], dtype=np.float32)
        side = np.array([-d[1], d[0], 0.0], dtype=np.float32)
        end = self._path_pts[-1]
        size = 3.0 * _marker_radius_for_point(self, end)
        tip, left, right = end + d * size, end + side * size * 0.45, end - side * size * 0.45
        up, color = [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]
        rows = [[*p, *up, *color] for p in (tip, left, right)]
        self._renderer.upload_points(np.array(rows, dtype=np.float32))

    def contextMenuEvent(self, event):
        if self._context_menu_suppressed:
            return
        menu = QMenu(self)
        if self.start_menu is not None:
            self.start_menu(menu, self._model_point(event.pos()))
        self._exec_menu(menu, event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._start_drag is not None:
            self._start_drag = None
            self.start_drag_finished.emit()
            return
        if self._joint_drag is not None:
            j, self._joint_drag = self._joint_drag, None
            self.joint_drag_finished.emit(j, self._joint_moved)
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started and self._press_pos is not None
                and not self._bg_adjusting):
            pos = event.position().toPoint()
            seg = self.segment_at(pos)
            pt = self._model_point(pos)
            # On the path, a click selects; anywhere else it draws.
            if seg >= 0 or pt is None:
                self.segment_clicked.emit(seg)
            else:
                self.draw_clicked.emit(*pt)
        super().mouseReleaseEvent(event)


class TurtleViewer(QDialog, _UndoableViewerMixin):
    """Command table + live path preview. Read-only by default; pass
    `editable=True` for Save/Cancel editing, as the other literal editors."""

    committed = Signal(str)

    def __init__(self, title: str, commands: list, parent=None, editable: bool = False,
                 start=None, start_movable: bool = True, start_note: str = "",
                 indent: str = ""):
        super().__init__(parent)
        label = "Turtle Editor" if editable else "Turtle Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 520)
        self._editable = editable
        self._commands = commands
        self._start = [float(v) for v in (start or (0.0, 0.0))]
        self._indent = indent

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _TurtleViewport([], True, self, editable=False)
        self._vp.segment_clicked.connect(self._on_segment_clicked)
        if editable:
            self._vp.draw_clicked.connect(self._on_draw_clicked)
            self._vp.hover_changed.connect(self._update_preview)
            self._vp.start_movable = start_movable
            self._vp.start_menu = self._add_start_actions
            self._vp.start_drag_started.connect(self._begin_live_edit)
            self._vp.start_dragged.connect(self._on_start_dragged)
            self._vp.start_drag_finished.connect(lambda: self._end_live_edit("Move Start"))
            self._vp.joint_drag_started.connect(self._on_joint_drag_started)
            self._vp.joint_dragged.connect(self._on_joint_dragged)
            self._vp.joint_drag_finished.connect(self._on_joint_drag_finished)
        _sync_viewport_to_main_window(self._vp)
        # Every arc is many points; the markers would bury the path. The
        # selected command's points still show.
        self._vp._show_unselected = False
        splitter.addWidget(self._vp)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self._count_label = QLabel()
        side_layout.addWidget(self._count_label)
        self._table = QTableWidget(0, 3)
        self._table.setFont(QFont("Menlo", 11))
        self._table.setHorizontalHeaderLabels(["Command", "Arg 1", "Arg 2"])
        _style_table_headers(self._table)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        fm = self._table.fontMetrics()
        self._table.setColumnWidth(0, fm.horizontalAdvance("arcrightto") + 40)
        self._table.setColumnWidth(1, fm.horizontalAdvance("-0000.00") + 16)
        self._table.setColumnWidth(2, fm.horizontalAdvance("-0000.00") + 16)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._highlight_selection)
        if editable:
            self._table.setItemDelegateForColumn(0, _CommandDelegate(self._table))
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                        | QAbstractItemView.EditTrigger.EditKeyPressed)
            self._table.itemChanged.connect(self._on_item_changed)
            self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._table.customContextMenuRequested.connect(self._show_table_menu)
        else:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        side_layout.addWidget(self._table, 1)
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("QLabel { color: #d33; }")
        side_layout.addWidget(self._error_label)
        if start_note:
            note = QLabel(start_note)
            note.setWordWrap(True)
            note.setStyleSheet("QLabel { color: gray; }")
            side_layout.addWidget(note)
        splitter.addWidget(side)
        splitter.setSizes([560, 340])
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        restore_session_background(self.windowTitle(), self._vp)
        if editable:
            self._setup_undo(self._get_value())
            add = QPushButton("Add Command")
            add.clicked.connect(lambda: self._insert(len(self._rows())))
            btn_row.addWidget(add)
            btn_row.addLayout(self._make_reset_button_row())
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            self._save = QPushButton("Save")
            self._save.clicked.connect(self._on_save)
            btn_row.addWidget(self._save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._spans: list[tuple[int, int]] = []
        self._populate_table()
        self._vp.schedule_load(lambda: self._rebuild(reframe=True))

    # -- value ------------------------------------------------------------

    def _rows(self):
        return turtle.split(self._commands)

    def _get_value(self):
        return {"commands": self._commands, "start": self._start}

    def _apply_value(self, value):
        self._commands, self._start = value["commands"], value["start"]
        self._populate_table()
        self._rebuild(reframe=False)

    def _populate_table(self):
        rows = self._rows()
        self._table.blockSignals(True)
        self._table.setRowCount(len(rows))
        self._table.setVerticalHeaderLabels([str(i) for i in range(len(rows))])
        for i, (cmd, args) in enumerate(rows):
            texts = [cmd] + [format_turtle(a) for a in args] + [""] * (2 - len(args))
            for j, text in enumerate(texts[:3]):
                item = QTableWidgetItem(text)
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(i, j, item)
        self._table.blockSignals(False)
        self._count_label.setText(f"Commands ({len(rows)})")

    def start_point(self) -> list:
        return list(self._start)

    def _run(self, commands=None):
        return turtle.run(self._commands if commands is None else commands, start=self._start)

    def _commit(self, commands=None, start=None, label="Edit"):
        self._commit_value({"commands": self._commands if commands is None else commands,
                            "start": self._start if start is None else list(start)}, label)

    def _rebuild(self, reframe: bool = False):
        self._vp.start = self._start
        self._vp.start_angle = turtle.start_heading(self._commands)
        try:
            path, self._spans, self._vp.heading = self._run()
            self._error_label.clear()
            ok = True
        except turtle.TurtleError as e:
            path, self._spans, self._vp.heading = [list(self._start)], [], None
            self._error_label.setText(str(e))
            ok = False
        if self._editable:
            self._save.setEnabled(ok)
            draws = turtle.joints(self._commands) if ok else None
            self._vp.joints = [path[self._spans[d][1]] for d in draws] if draws else []
        if self._vp._ctx is not None:
            self._vp.load_path(path, False, reframe=reframe)
            self._highlight_selection()
            if self._editable:
                self._update_preview()

    def _highlight_selection(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._spans):
            self._vp.set_selected([])
            return
        first, last = self._spans[rows[0].row()]
        self._vp.set_selected(list(range(first, last + 1)))

    def _on_segment_clicked(self, seg: int):
        """Select the command that drew segment `seg` (points seg..seg+1)."""
        for k, (first, last) in enumerate(self._spans):
            if first <= seg < last:
                self._table.selectRow(k)
                return
        self._table.clearSelection()

    # -- drawing ----------------------------------------------------------

    def _draw_commands(self, x: float, y: float) -> list:
        """What a click at (x, y) appends, by the modifiers held now:
        Option, a tangent arc; none, a turn and a move; Cmd (Ctrl off
        macOS), a turn and an exact jump. Shift snaps angles to 30 degrees
        and distances (a move's length, an arc's radius) to the smallest
        scale ticks shown -- except with Cmd, where it snaps the jump's
        point to the grid of those ticks."""
        try:
            path, _, heading = self._run()
        except turtle.TurtleError:
            return []
        mods = QGuiApplication.queryKeyboardModifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        jump = bool(mods & Qt.KeyboardModifier.ControlModifier)
        step = self._vp.minor_step()
        if shift and jump and not mods & Qt.KeyboardModifier.AltModifier:
            # A jump goes to coordinates, so its snap is to the grid.
            return turtle.line_to(path[-1], heading,
                                  (round(x / step) * step, round(y / step) * step), jump=True)
        snap = {"snap_angle": 30, "snap_len": step} if shift else {}
        if mods & Qt.KeyboardModifier.AltModifier:
            return turtle.tangent_arc(path[-1], heading, (x, y), **snap)
        return turtle.line_to(path[-1], heading, (x, y), jump=jump, **snap)

    def _update_preview(self):
        vp = self._vp
        pos = vp.hover
        pt = vp._model_point(pos) if pos is not None else None
        extra = []
        if pt is not None and not vp._bg_adjusting and vp.segment_at(pos) < 0 \
                and vp.start_handle_at(pos) is None and vp.joint_at(pos) < 0:
            extra = self._draw_commands(*pt)
        if not extra:
            vp.set_preview(None)
            return
        n = len(self._rows())
        path, spans, _ = self._run(self._commands + extra)
        vp.set_preview(path[spans[n][0]:])

    def _on_draw_clicked(self, x: float, y: float):
        extra = self._draw_commands(x, y)
        if extra:
            self._commit(self._commands + extra, label="Draw")

    def keyPressEvent(self, event):
        self._update_preview()
        self._delete_on_key(event) or super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        self._update_preview()
        super().keyReleaseEvent(event)

    # -- editing ----------------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem):
        rows = copy.deepcopy(self._rows())
        i, j = item.row(), item.column()
        cmd, args = rows[i]
        try:
            if j == 0:
                cmd = item.text()
            else:
                parsed = [_parse_arg(self._table.item(i, k).text()) for k in (1, 2)]
                # A blank first argument with a second makes no sense: keep
                # the arguments as given, up to the first blank.
                args = parsed[:parsed.index(None)] if None in parsed else parsed
            rows[i] = (cmd, args)
            new = turtle.join(rows)
            # A string typed as an argument would be read as a new command.
            if len(turtle.split(new)) != len(rows):
                raise turtle.TurtleError("A string argument would start a new command")
        except turtle.TurtleError as e:
            self._error_label.setText(str(e))
            self._populate_table()
            return
        self._commit(new, label="Edit Command")

    def _insert(self, at: int, row=("move", [10])):
        rows = self._rows()
        rows.insert(at, (row[0], list(row[1])))
        self._commit(turtle.join(rows), label="Add Command")
        self._table.selectRow(at)

    def _delete(self, at: int):
        rows = self._rows()
        del rows[at]
        self._commit(turtle.join(rows), label="Delete Command")

    def _move(self, at: int, by: int):
        rows = self._rows()
        rows[at], rows[at + by] = rows[at + by], rows[at]
        self._commit(turtle.join(rows), label="Move Command")
        self._table.selectRow(at + by)

    def _show_table_menu(self, pos):
        at = self._table.rowAt(pos.y())
        n = len(self._rows())
        menu = QMenu(self._table)
        if at < 0:
            menu.addAction("Add Command", lambda: self._insert(n))
        else:
            menu.addAction("Insert Command Above", lambda: self._insert(at))
            menu.addAction("Insert Command Below", lambda: self._insert(at + 1))
            menu.addSeparator()
            up = menu.addAction("Move Up", lambda: self._move(at, -1))
            up.setEnabled(at > 0)
            down = menu.addAction("Move Down", lambda: self._move(at, 1))
            down.setEnabled(at < n - 1)
            menu.addSeparator()
            menu.addAction("Delete Command", lambda: self._delete(at))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _delete_on_key(self, event) -> bool:
        if self._editable and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) \
                and self._table.state() != QAbstractItemView.State.EditingState:
            rows = self._table.selectionModel().selectedRows()
            if rows:
                self._delete(rows[0].row())
                event.accept()
                return True
        return False

    # -- the start --------------------------------------------------------

    def _on_start_dragged(self, x: float, y: float, kind: str):
        """Live, between _begin_live_edit and _end_live_edit: one undo step
        for the whole drag. Shift snaps the position to the smallest grid
        ticks shown, and the direction to 30 degrees, as drawing does."""
        shift = bool(QGuiApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        if kind == "move":
            step = self._vp.minor_step() if shift else 0.01
            self._start = [round(round(x / step) * step, 2), round(round(y / step) * step, 2)]
        else:
            dx, dy = x - self._start[0], y - self._start[1]
            if math.hypot(dx, dy) < 1e-9:
                return
            angle = math.degrees(math.atan2(dy, dx))
            if shift:
                angle = round(angle / 30) * 30
            self._commands = turtle.with_start_heading(self._commands, angle)
            self._populate_table()
        self._rebuild(reframe=False)

    # -- joints -----------------------------------------------------------

    def _on_joint_drag_started(self, j: int):
        # Every frame refits from the list as it was when the drag began,
        # so rounding never accumulates over a long drag.
        self._joint_base = self._commands
        self._begin_live_edit()

    def _on_joint_dragged(self, j: int, x: float, y: float):
        """Shift snaps the joint to the smallest grid ticks shown. A point
        no refit reaches leaves the path as it last was."""
        if QGuiApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            step = self._vp.minor_step()
            x, y = round(x / step) * step, round(y / step) * step
        new = turtle.drag_joint(self._joint_base, self._start, j, (x, y))
        if new is not None:
            self._commands = new
            self._populate_table()
            self._rebuild(reframe=False)

    def _on_joint_drag_finished(self, j: int, moved: bool):
        self._end_live_edit("Move Joint")
        draws = turtle.joints(self._commands)
        if not moved and draws and j < len(draws):
            self._table.selectRow(draws[j])   # a click: select what drew it

    def _add_start_actions(self, menu: QMenu, pt):
        if not self._vp.start_movable:
            return
        if pt is not None:
            menu.addAction("Start Here", lambda: self._commit(
                start=[round(pt[0], 2), round(pt[1], 2)], label="Start Here"))
        menu.addAction("Start From…", self._start_from_dialog)

    def _start_from_dialog(self):
        from PySide6.QtWidgets import QDialogButtonBox, QDoubleSpinBox, QFormLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("Start From")
        form = QFormLayout(dlg)
        boxes = []
        for label, value, lo, hi, dec in (("X:", self._start[0], -1e6, 1e6, 2),
                                         ("Y:", self._start[1], -1e6, 1e6, 2),
                                         ("Direction (°):", turtle.start_heading(self._commands),
                                          -360, 360, 1)):
            box = QDoubleSpinBox(dlg)
            box.setRange(lo, hi)
            box.setDecimals(dec)
            box.setValue(value)
            form.addRow(label, box)
            boxes.append(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel, parent=dlg)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec():
            x, y, angle = (b.value() for b in boxes)
            self._commit(turtle.with_start_heading(self._commands, angle), [x, y], "Start From")

    def _on_save(self):
        self.committed.emit(format_turtle_lines(self._commands, self._indent))
        self.accept()
