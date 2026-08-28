from __future__ import annotations
import math
import time
from pathlib import Path
import numpy as np
from dataclasses import dataclass

from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel, QPushButton
from PySide6.QtCore import (Qt, QPoint, QSize, Signal, QTimer, QVariantAnimation,
                             QEasingCurve)
from PySide6.QtGui import (QMouseEvent, QWheelEvent, QNativeGestureEvent,
                            QPainter, QPixmap, QIcon)

from belfryscad.engine.renderer import SceneRenderer
from belfryscad.window.debugger import _debug_icon

_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"

# Ignore near-zero scroll deltas, which otherwise jitter the camera.
_WHEEL_DEADSPOT = 5

# Trackpad axis conventions vary by platform, and on macOS the pan sign
# also flips with the user's "natural scrolling" preference -- which Qt
# folds into pixelDelta() rather than exposing separately. Both were
# settled on a real trackpad, not derived: Qt's RotateNativeGesture
# value() turned out to be clockwise-positive on macOS, the same sense
# _outer_ring_roll_delta_deg already uses, so it needs no flip on the way
# into `cam.roll -= ...` (an earlier -1.0 here rotated the wrong way).
# These stay named constants because they're the only plausible thing to
# change if a future Qt or platform reverses either convention.
_TRACKPAD_PAN_SIGN = 1.0
_ROTATE_GESTURE_SIGN = 1.0


def _recolored_icon_pixmap(name: str, size: int, color: Qt.GlobalColor, prefix: str = "debug") -> QPixmap:
    """Render a `{prefix}-{name}.svg` icon at `size`x`size`, recolored solid
    `color` (keeping the original alpha/shape) — most of these icons are
    a normal dark-gray for menus/toolbars, but the viewport's dark
    translucent overlays need them in white for contrast."""
    if prefix == "debug":
        icon = _debug_icon(name)
    else:
        path = _ICONS_DIR / f"{prefix}-{name}.svg"
        icon = QIcon(str(path)) if path.exists() else QIcon()
    pixmap = icon.pixmap(size, size)
    recolored = QPixmap(pixmap.size())
    # icon.pixmap(size, size) returns a pixmap sized size*devicePixelRatio
    # in raw pixels on a HiDPI screen, with devicePixelRatio() set so its
    # LOGICAL size is still size x size -- but a freshly-constructed
    # QPixmap always defaults devicePixelRatio to 1.0, so without this,
    # `recolored`'s logical size silently becomes size*dpr x size*dpr
    # instead of size x size. drawPixmap below then only fills the
    # top-left 1/dpr fraction of `recolored`'s canvas (the rest stays
    # transparent), and every consumer that fits this into a fixed
    # logical-pixel icon slot (setIconSize, QLabel.setPixmap) shrinks
    # that already-too-small glyph even further -- reproduced only on a
    # HiDPI/Retina display (dpr > 1), never on a standard external
    # monitor (dpr == 1, where this mismatch is a no-op).
    recolored.setDevicePixelRatio(pixmap.devicePixelRatio())
    recolored.fill(Qt.GlobalColor.transparent)
    painter = QPainter(recolored)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(recolored.rect(), color)
    painter.end()
    return recolored


_RING_PERP1 = [
    np.array([0.0, 1.0, 0.0], dtype=np.float64),  # X ring: perp1 = Y
    np.array([1.0, 0.0, 0.0], dtype=np.float64),  # Y ring: perp1 = X
    np.array([1.0, 0.0, 0.0], dtype=np.float64),  # Z ring: perp1 = X
]
_RING_PERP2 = [
    np.array([0.0, 0.0, 1.0], dtype=np.float64),  # X ring: perp2 = Z
    np.array([0.0, 0.0, 1.0], dtype=np.float64),  # Y ring: perp2 = Z
    np.array([0.0, 1.0, 0.0], dtype=np.float64),  # Z ring: perp2 = Y
]


def _outer_ring_roll_delta_deg(x: float, y: float, dx: float, dy: float,
                                width: float, height: float) -> float | None:
    """Shift+drag "Orbit" mode: if (x, y) -- the drag's new mouse position --
    falls in the outer 20% of the viewport's inscribed circle, returns the
    on-screen (clockwise-positive) angle the mouse just swept around the
    viewport center, for rolling the view like a dial rim; otherwise None
    (drag is close enough to center for the normal trackball tilt instead).
    Screen y grows downward, so an increasing atan2(y, x) angle already
    reads as clockwise motion -- no extra sign flip needed here."""
    cx, cy = width / 2.0, height / 2.0
    radius = min(width, height) / 2.0
    if radius <= 0:
        return None
    nx, ny = (x - cx) / radius, (y - cy) / radius
    if (nx * nx + ny * ny) ** 0.5 < 0.8:
        return None
    old_x, old_y = x - dx, y - dy
    angle_old = math.atan2(old_y - cy, old_x - cx)
    angle_new = math.atan2(y - cy, x - cx)
    delta = (angle_new - angle_old + math.pi) % (2 * math.pi) - math.pi
    return math.degrees(delta)


class _MeasureLabel(QLabel):
    """A measurement's floating readout. Clicking it dismisses that
    measurement -- the label is the only part of a measurement big enough
    to aim at, so it is what the click has to land on."""

    clicked = Signal(object)

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to dismiss this measurement")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)
            event.accept()
            return
        super().mousePressEvent(event)


@dataclass
class Measurement:
    """One finished measurement, in world space.

    Points are kept rather than the derived number so the overlay can be
    redrawn from any camera angle, and so a measurement reads the same
    however the view moves. They do NOT survive a re-render: a point
    snapped to a vertex that no longer exists would still draw, and a
    plausible wrong number is worse than none.
    """
    kind: str                 # "distance" | "angle"
    points: list              # 2 for a distance, 3 for an angle (middle = vertex)
    snaps: list               # how each point was snapped: vertex/edge/face

    def value(self) -> float:
        """Length, or degrees at the middle point."""
        if self.kind == "distance":
            return float(np.linalg.norm(self.points[1] - self.points[0]))
        a = self.points[0] - self.points[1]
        b = self.points[2] - self.points[1]
        na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        if na < 1e-12 or nb < 1e-12:
            return float("nan")     # a leg of no length has no angle
        cos = float(np.dot(a, b)) / (na * nb)
        return float(np.degrees(np.arccos(max(-1.0, min(1.0, cos)))))

    def label(self) -> str:
        v = self.value()
        if self.kind == "distance":
            d = self.points[1] - self.points[0]
            return (f"{v:.3f}  (dx {d[0]:.3f}, dy {d[1]:.3f}, dz {d[2]:.3f})")
        if v != v:      # NaN
            return "angle undefined (a leg has zero length)"
        la = float(np.linalg.norm(self.points[0] - self.points[1]))
        lb = float(np.linalg.norm(self.points[2] - self.points[1]))
        return f"{v:.3f}\u00b0  (legs {la:.3f}, {lb:.3f})"


class Viewport(QOpenGLWidget):
    selection_changed   = Signal(int)                    # originalID or -1
    translate_committed = Signal(float, float, float)    # world-space delta
    rotate_committed    = Signal(int, float)             # axis (0/1/2), degrees
    scale_committed     = Signal(int, float, bool)       # axis (0/1/2), factor, uniform
    camera_changed      = Signal()                       # emitted on any camera movement
    size_changed        = Signal(int, int)               # emitted on viewport resize (w, h)
    perspective_toggled = Signal(bool)                    # emitted on click, new perspective state
    measurement_taken   = Signal(object)                  # a finished Measurement
    measurement_dismissed = Signal(int)                   # index into the list
    measure_progress    = Signal(str)                     # prompt for the next click

    def __init__(self, parent=None, selectable: bool = True, pan_speed: float = 1.0,
                 orientation_cube: bool = False):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._ctx = None
        self._renderer = SceneRenderer()
        self._last_mouse: QPoint | None = None
        self._mouse_button: Qt.MouseButton | None = None
        self._orbit_enabled: bool = True   # subclasses disable for locked 2D top-down views
        self._pan_speed = pan_speed   # data-viewer dialogs use 2x the main viewport's right-drag pan speed
        self.setMouseTracking(True)
        self._frame_count: int = 0
        self._pending_load = None
        self._last_bb_min: np.ndarray | None = None
        self._last_bb_max: np.ndarray | None = None

        # Ctrl+click AST-id selection (main window only — data-viewer
        # subclasses pass selectable=False so Ctrl+drag orbits like any
        # other drag instead of attempting to select/ray-cast).
        self._selectable = selectable

        # Tool state
        self._active_tool: int = -1   # -1=none, 0=translate, 1=rotate, 2=scale

        # Measurement. `_measure_mode` is None, "distance" or "angle";
        # `_measure_pending` collects the snapped points of the measurement
        # being taken. Finished ones live in MainWindow, not here -- the
        # viewport draws them but does not own the list.
        self._measure_mode = None
        self._measure_pending: list = []
        self._measure_hover = None
        self._measurements: list = []

        # Delta overlay label
        self._delta_label = QLabel("", self)
        self._delta_label.setStyleSheet(
            "QLabel { background: rgba(0,0,0,160); color: white;"
            " padding: 4px 10px; border-radius: 4px;"
            " font-family: Menlo; font-size: 13px; }"
        )
        self._delta_label.hide()

        # Measurement readout, one label per finished measurement plus the
        # one being taken. Same treatment as the delta overlay.
        self._measure_labels: list = []

        # Orientation cube (main window only -- the data-viewer subclasses
        # embed small preview viewports where it would just be in the way).
        # A plain child QWidget, so it never touches the GL context.
        self._orientation_cube = None
        self._view_anim = None      # in-flight orientation-cube view swing
        if orientation_cube:
            from .orientation_cube import OrientationCube
            self._orientation_cube = OrientationCube(self)
            self._orientation_cube.view_requested.connect(self._on_orientation_cube_view)
            self._orientation_cube.orbit_requested.connect(self._on_orientation_cube_orbit)
            self.camera_changed.connect(self._sync_orientation_cube)
            self._sync_orientation_cube()

        # Gizmo drag state
        self._gizmo_drag_axis: int = -1
        self._drag_axis_world: np.ndarray = np.zeros(3, dtype=np.float32)
        self._drag_gizmo_center: np.ndarray = np.zeros(3, dtype=np.float32)
        self._drag_start_1d: float = 0.0

        # Busy overlay (render or debug)
        self._render_busy: bool = False
        self._debug_busy: bool = False
        self._debug_paused: bool = False
        self._busy_start: float = 0.0
        self._spinner_frames = ["   ", ".  ", ".. ", "..."]
        self._busy_label = QLabel("", self)
        self._busy_label.setStyleSheet(
            "QLabel { background: rgba(0,0,0,160); color: white;"
            " padding: 8px 18px; border-radius: 8px;"
            " font-family: Menlo; font-size: 18px; }"
        )
        self._busy_label.hide()
        self._busy_timer = QTimer(self)
        self._busy_timer.timeout.connect(self._update_busy_overlay)

        # Spin: 6 RPM = 36°/s at 30 FPS (33 ms) = 1.2°/tick
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(33)
        self._spin_timer.timeout.connect(self._spin_tick)

        # Perspective/orthographic toggle (upper-left corner)
        self._persp_btn = QPushButton(self)
        self._persp_btn.setFlat(True)
        self._persp_btn.setFixedSize(40, 40)
        self._persp_btn.setIconSize(QSize(24, 24))
        self._persp_btn.setStyleSheet(
            "QPushButton { background: rgba(0,0,0,160); border: none; border-radius: 8px; }"
            "QPushButton:hover { background: rgba(0,0,0,200); }"
        )
        self._persp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._persp_btn.move(12, 12)
        self._persp_btn.clicked.connect(self._on_perspective_button_clicked)
        self.refresh_perspective_icon()

        # Transform tools, stacked under the perspective toggle. Hidden
        # until something is selected: a gizmo needs a shape to act on, and
        # a button that does nothing is worse than one that is not there.
        self._tool_btns: dict = {}
        for row, (tool_id, icon_name, tip) in enumerate((
            (0, "translate", "Translate"),
            (1, "rotate", "Rotate"),
            (2, "scale", "Scale"),
        )):
            btn = QPushButton(self)
            btn.setFlat(True)
            btn.setCheckable(True)
            btn.setFixedSize(40, 40)
            btn.setIconSize(QSize(24, 24))
            btn.setStyleSheet(
                "QPushButton { background: rgba(0,0,0,160); border: none;"
                " border-radius: 8px; }"
                "QPushButton:hover { background: rgba(0,0,0,200); }"
                "QPushButton:checked { background: rgba(80,140,220,220); }"
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setIcon(QIcon(_recolored_icon_pixmap(
                icon_name, 24, Qt.GlobalColor.white, prefix="tool")))
            btn.move(12, 12 + (row + 1) * 46)
            btn.clicked.connect(lambda _checked=False, t=tool_id: self._on_tool_button(t))
            btn.hide()
            self._tool_btns[tool_id] = btn

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self):
        import moderngl
        self._ctx = moderngl.create_context(require=330)
        self._renderer.initialize(self._ctx)
        if self._pending_load is not None:
            fn = self._pending_load
            self._pending_load = None
            fn()

    def schedule_load(self, fn):
        """Schedule a geometry-load function to run once GL is initialized
        (immediately if it already is). Lets callers (e.g. a dialog
        constructing this widget) load geometry before `initializeGL` has
        necessarily run yet."""
        if self._ctx is not None:
            fn()
        else:
            self._pending_load = fn

    def resizeGL(self, w, h):
        if self._ctx:
            self._ctx.viewport = (0, 0, w, h)
            self._renderer.set_viewport(w, h)
        if self._orientation_cube is not None:
            self._orientation_cube.place_in(self.width())
        if self._debug_paused or self._render_busy or self._debug_busy:
            self._position_busy_label()
        self.size_changed.emit(w, h)

    def paintGL(self):
        try:
            fbo_id = self.defaultFramebufferObject()
            self._renderer.paint(qt_fbo_id=fbo_id, extra_paint=self._paint_extra)
            self._frame_count += 1
        except Exception as e:
            import traceback
            print("paintGL error:", traceback.format_exc())

    def _paint_extra(self, mvp: np.ndarray):
        """Hook for subclasses (data-viewer dialogs) to draw their own
        overlay geometry — e.g. blinking selection markers — in the same
        eye's `mvp` as the main scene. No-op by default."""
        pass

    # ------------------------------------------------------------------
    # Measurement overlay
    # ------------------------------------------------------------------

    _MEASURE_RGB = (1.0, 0.85, 0.25)
    _PENDING_RGB = (0.55, 0.85, 1.0)

    def _measure_marker_size(self) -> float:
        """Cross size, scaled to the camera so it stays legible at any zoom
        without redrawing on every orbit."""
        return max(1e-6, float(self._renderer.camera.distance) * 0.012)

    def _measure_segments(self):
        """[(a, b, rgb)] for everything the measurement overlay draws."""
        segs = []

        def cross(p, rgb):
            r = self._measure_marker_size()
            for axis in range(3):
                d = np.zeros(3)
                d[axis] = r
                segs.append((p - d, p + d, rgb))

        for m in self._measurements:
            rgb = self._MEASURE_RGB
            if m.kind == "distance":
                segs.append((m.points[0], m.points[1], rgb))
            else:
                segs.append((m.points[1], m.points[0], rgb))
                segs.append((m.points[1], m.points[2], rgb))
            for p in m.points:
                cross(p, rgb)

        # The one being taken: placed points, plus a rubber band to
        # whatever the cursor is currently over.
        placed = [p for p, _ in self._measure_pending]
        for p in placed:
            cross(p, self._PENDING_RGB)
        if self._measure_mode is not None and self._measure_hover is not None:
            hp = self._measure_hover[0]
            cross(hp, self._PENDING_RGB)
            if placed:
                anchor_pt = placed[1] if len(placed) >= 2 else placed[0]
                segs.append((anchor_pt, hp, self._PENDING_RGB))
                if len(placed) >= 2:
                    segs.append((placed[1], placed[0], self._PENDING_RGB))
        return segs

    def _rebuild_measure_overlay(self):
        """Push the overlay into the renderer's line buffers.

        Rebuilt rather than drawn per frame: the segment count is tiny, and
        a buffer means orbiting costs nothing.
        """
        if self._ctx is None:
            return
        self.makeCurrent()
        try:
            self._renderer.clear_lines()
            segs = self._measure_segments()
            if segs:
                rows = []
                for a, b, rgb in segs:
                    rows.append([a[0], a[1], a[2], *rgb])
                    rows.append([b[0], b[1], b[2], *rgb])
                self._renderer.upload_lines(np.array(rows, dtype=np.float32))
        finally:
            self.doneCurrent()
        self._refresh_measure_labels()

    def _on_measure_label_clicked(self, label):
        """Dismiss whichever measurement this label is currently showing.
        Resolved now rather than captured when the label was made: labels
        are reused as the list changes, so a stored index would go stale."""
        try:
            index = self._measure_labels.index(label)
        except ValueError:
            return
        if index < len(self._measurements):
            self.measurement_dismissed.emit(index)

    def _refresh_measure_labels(self):
        """One floating label per measurement, at the midpoint of what it
        measures."""
        while len(self._measure_labels) < len(self._measurements):
            lab = _MeasureLabel(self)
            lab.setStyleSheet(
                "QLabel { background: rgba(0,0,0,170); color: #ffd94a;"
                " padding: 2px 7px; border-radius: 4px;"
                " font-family: Menlo; font-size: 12px; }")
            lab.clicked.connect(self._on_measure_label_clicked)
            self._measure_labels.append(lab)
        for lab in self._measure_labels[len(self._measurements):]:
            lab.hide()

        w, h = self.width(), self.height()
        aspect = w / h if h > 0 else 1.0
        mvp = (self._renderer.camera.projection_matrix(aspect)
                @ self._renderer.camera.view_matrix())
        for m, lab in zip(self._measurements, self._measure_labels):
            anchor_pt = (m.points[0] + m.points[1]) / 2.0 if m.kind == "distance" \
                else m.points[1]
            clip = mvp.astype(np.float64) @ np.array(
                [anchor_pt[0], anchor_pt[1], anchor_pt[2], 1.0])
            if clip[3] <= 1e-9:
                lab.hide()          # behind the camera
                continue
            ndc = clip[:2] / clip[3]
            x = (ndc[0] * 0.5 + 0.5) * w
            y = (1.0 - (ndc[1] * 0.5 + 0.5)) * h
            lab.setText(m.label())
            lab.adjustSize()
            lab.move(int(x - lab.width() / 2), int(y - lab.height() - 6))
            lab.show()
            lab.raise_()

    def paintEvent(self, event):
        super().paintEvent(event)   # triggers paintGL

    def closeEvent(self, event):
        self.makeCurrent()
        self._renderer.release()
        self._ctx = None
        self.doneCurrent()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_geometry(self, bodies):
        self.makeCurrent()
        self._renderer.load_geometry(bodies)
        self.doneCurrent()
        self.update()

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        # Cache the bounds so "View All" can reframe from them directly (see
        # _frame_all) instead of only being able to derive bounds by scanning
        # live buffers — needed for data viewers whose geometry lives in
        # upload_lines/upload_points buffers, which carry no per-vertex CPU
        # arrays the way CSG MeshBuffers do.
        self._last_bb_min = bb_min.copy() if hasattr(bb_min, "copy") else bb_min
        self._last_bb_max = bb_max.copy() if hasattr(bb_max, "copy") else bb_max
        # reframe=False skips the actual camera re-fit (still updates the
        # cache above, so "View All" stays correct) -- used for a live
        # vertex drag/nudge rebuild, where re-fitting on every move would
        # zoom/recenter the view instead of just keeping the edited vertex
        # on-screen (see Camera.pan_to_keep_visible, called separately by
        # the vertex-move handlers in data_viewers.py).
        if reframe:
            self._renderer.camera.frame_bounds(bb_min, bb_max)
        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    # ------------------------------------------------------------------
    # Measurement
    # ------------------------------------------------------------------

    MEASURE_DISTANCE = "distance"
    MEASURE_ANGLE = "angle"

    def set_measure_mode(self, mode):
        """Enter, switch or leave measurement. Anything half-picked is
        dropped: a leftover point from the other mode would silently become
        part of the next measurement."""
        self._measure_mode = mode
        self._measure_pending = []
        self._measure_hover = None
        self.setCursor(Qt.CursorShape.CrossCursor if mode
                        else Qt.CursorShape.ArrowCursor)
        self.measure_progress.emit(self._measure_prompt())
        self._rebuild_measure_overlay()
        self.update()

    def measure_mode(self):
        return self._measure_mode

    def _measure_prompt(self) -> str:
        if self._measure_mode is None:
            return ""
        n = len(self._measure_pending)
        if self._measure_mode == self.MEASURE_DISTANCE:
            return ("Measure: click the first point." if n == 0
                    else "Measure: click the second point.  (Esc cancels)")
        return [
            "Measure angle: click the first point.",
            "Measure angle: click the vertex -- the corner the angle is at.",
            "Measure angle: click the third point.  (Esc cancels)",
        ][min(n, 2)]

    def cancel_measurement(self):
        """Drop a half-taken measurement, staying in the mode."""
        if not self._measure_pending:
            return False
        self._measure_pending = []
        self.measure_progress.emit(self._measure_prompt())
        self._rebuild_measure_overlay()
        self.update()
        return True

    def set_measurements(self, measurements: list):
        """The finished measurements to draw. Owned by MainWindow."""
        self._measurements = list(measurements)
        self._rebuild_measure_overlay()
        self.update()

    def _do_measure_click(self, pos) -> bool:
        snap = self._renderer.snap_at(pos.x(), pos.y(), self.width(), self.height())
        if snap is None:
            # A click on empty space is not a measurement point. Silently
            # ignoring it beats snapping to a far-away vertex that merely
            # happens to be under the cursor.
            self.measure_progress.emit("Measure: that click missed the model.")
            return True
        point, kind = snap
        self._measure_pending.append((np.asarray(point, dtype=np.float64), kind))
        want = 2 if self._measure_mode == self.MEASURE_DISTANCE else 3
        if len(self._measure_pending) >= want:
            points = [p for p, _ in self._measure_pending]
            kinds = [k for _, k in self._measure_pending]
            self._measure_pending = []
            self.measurement_taken.emit(Measurement(self._measure_mode, points, kinds))
        self.measure_progress.emit(self._measure_prompt())
        self._rebuild_measure_overlay()
        self.update()
        return True

    def _on_tool_button(self, tool_id: int):
        """Only one tool runs at a time, and clicking the running one turns
        it off -- otherwise there would be no way to put the gizmo away
        without selecting something else."""
        already = self._active_tool == tool_id
        self.set_active_tool(-1 if already else tool_id)

    def _sync_tool_buttons(self):
        """Show the tools only with a shape selected, and light whichever
        is running."""
        visible = self._renderer.selected_id is not None
        for tool_id, btn in getattr(self, "_tool_btns", {}).items():
            btn.setVisible(visible)
            btn.setChecked(visible and self._active_tool == tool_id)

    def set_selection(self, orig_id):
        """Set (or clear, with None) the selected shape, keeping the tool
        buttons in step. A tool left running over a cleared selection would
        draw no gizmo and answer no clicks."""
        self._renderer.selected_id = orig_id
        if orig_id is None and self._active_tool in (0, 1, 2):
            self.set_active_tool(-1)
        self._sync_tool_buttons()
        self.update()

    def set_active_tool(self, tool_id: int):
        self._active_tool = tool_id
        self._renderer.show_gizmo = tool_id in (0, 1, 2)
        self._renderer.gizmo_type = tool_id
        self._gizmo_drag_axis = -1
        self._renderer.active_gizmo_axis = -1
        self._sync_tool_buttons()
        self.update()

    def camera_info(self) -> dict:
        cam = self._renderer.camera
        return {
            "azimuth": cam.azimuth,
            "elevation": cam.elevation,
            "distance": cam.distance,
            "target": cam.target.tolist(),
            "fov": cam.fov,
        }

    def set_render_busy(self, busy: bool):
        self._render_busy = busy
        if busy:
            self._debug_busy = False
            self._busy_start = time.monotonic()
            self._update_busy_overlay()
            self._busy_label.show()
            self._busy_timer.start(100)
        else:
            self._busy_timer.stop()
            self._busy_label.hide()

    def set_debug_busy(self, busy: bool):
        self._debug_busy = busy
        if busy:
            self._render_busy = False
            self._busy_start = time.monotonic()
            self._update_busy_overlay()
            self._busy_label.show()
            self._busy_timer.start(100)
        else:
            self._busy_timer.stop()
            self._busy_label.hide()

    def set_debug_paused(self, paused: bool):
        self._debug_busy = False
        self._render_busy = False
        self._debug_paused = paused
        self._busy_timer.stop()
        if paused:
            self._busy_label.setPixmap(_recolored_icon_pixmap("pause", 24, Qt.GlobalColor.white))
            self._busy_label.adjustSize()
            self._position_busy_label()
            self._busy_label.show()
        else:
            self._busy_label.hide()

    def _position_busy_label(self):
        """Top edge, horizontally centred -- shared by the debug-paused
        indicator and the render/debug busy countdown overlay
        (_update_busy_overlay).

        It sat in the upper-RIGHT corner, which is where the orientation
        cube now lives, so both overlays were drawn partly behind it. Top
        centre is the nearest free strip: still clear of the model (the
        placement that was genuinely in the way was the old dead-centre
        one, not this), and clear of all three corner widgets -- the
        perspective button upper-left, the cube upper-right.

        Also re-called from resizeGL so the indicator stays inside the
        viewport -- its position is computed from self.width(), which goes
        stale the moment the viewport is resized while the label is
        showing; nothing else re-triggers a reposition mid-render/mid-pause."""
        margin = 12
        x = max(margin, (self.width() - self._busy_label.width()) // 2)
        y = margin
        self._busy_label.move(x, y)

    def _on_perspective_button_clicked(self):
        cam = self._renderer.camera
        cam.orthographic = not cam.orthographic
        self.refresh_perspective_icon()
        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.perspective_toggled.emit(not cam.orthographic)
        self.update()

    def refresh_perspective_icon(self):
        """Sync the upper-left toggle button's icon/tooltip to the camera's
        current projection mode. Public so MainWindow can call it after
        changing camera.orthographic from elsewhere (the View menu's
        "Perspective" checkbox, or restoring a saved preference) -- the
        button's own click handler already keeps itself in sync."""
        orthographic = self._renderer.camera.orthographic
        name = "orthographic" if orthographic else "perspective"
        self._persp_btn.setIcon(QIcon(_recolored_icon_pixmap(name, 24, Qt.GlobalColor.white, prefix="view")))
        self._persp_btn.setToolTip("Orthographic (click for Perspective)" if orthographic
                                    else "Perspective (click for Orthographic)")

    def _update_busy_overlay(self):
        elapsed = time.monotonic() - self._busy_start
        frame = int(elapsed * 4) % len(self._spinner_frames)
        if self._debug_busy:
            self._busy_label.setText(f" Debugging {self._spinner_frames[frame]}")
        else:
            self._busy_label.setText(f" {int(elapsed)}s {self._spinner_frames[frame]}")
        self._busy_label.adjustSize()
        self._position_busy_label()

    # ------------------------------------------------------------------
    # Spin
    # ------------------------------------------------------------------

    def set_spinning(self, enabled: bool):
        if enabled:
            self._spin_timer.start()
        else:
            self._spin_timer.stop()

    def _spin_tick(self):
        cam = self._renderer.camera
        cam.azimuth = (cam.azimuth + 36.0 * 33 / 1000.0) % 360.0
        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    # ------------------------------------------------------------------
    # Camera view presets
    # ------------------------------------------------------------------

    # Where each named view puts the camera. Top and bottom are not exactly
    # +-90: _look_at's world-up ([0,0,1]) becomes parallel to the forward
    # vector at precisely elevation=+-90 (gimbal lock), so it falls back to a
    # hardcoded +X "right" vector -- which doesn't match the
    # azimuth-dependent basis the drag-orbit math continuously converges to
    # as elevation moves away from the pole. Starting a drag from exactly
    # elevation=90 therefore snapped the view to whatever direction that
    # arbitrary +X fallback happened to imply. Landing just shy of the pole
    # keeps the view visually identical (sin/cos differ by ~1e-6) while
    # keeping the basis on the continuous (non-fallback) branch.
    #
    # The orientation cube derives the same numbers from its face normals
    # (orientation_cube.azimuth_elevation_for), including this same dodge, so
    # clicking TOP and pressing Ctrl+4 land on identical values.
    VIEW_PRESETS = {
        "top": (270.0, 89.9999),
        "bottom": (0.0, -89.9999),
        "front": (270.0, 0.0),
        "back": (90.0, 0.0),
        "left": (180.0, 0.0),
        "right": (0.0, 0.0),
        "iso": (295.0, 35.0),
    }

    def set_view_preset(self, preset: str):
        cam = self._renderer.camera
        if preset == "all":
            # Framing, not an orientation change -- nothing to swing through.
            self.stop_view_animation()
            self._frame_all(cam)
            if self._measurements:
                self._refresh_measure_labels()
            self.camera_changed.emit()
            self.update()
            return
        target = self.VIEW_PRESETS.get(preset)
        if target is None:
            return
        # Swung, not cut, so the model's new orientation is readable on the
        # way in -- same animation the orientation cube uses, so the two
        # routes to the same view behave identically.
        self.animate_camera_to(*target)

    def _sync_orientation_cube(self):
        """Mirror the camera's orientation into the cube. Cheap enough to run
        on every camera_changed (it is a 3x3 compare, then a repaint only if
        the rotation actually moved)."""
        if self._orientation_cube is not None:
            self._orientation_cube.set_orientation(self._renderer.camera.view_matrix())

    def _on_orientation_cube_view(self, azimuth: float, elevation: float):
        self.animate_camera_to(azimuth, elevation)

    def _on_orientation_cube_orbit(self, dx: float, dy: float):
        """Dragging the cube orbits the scene, using the same rule and the
        same sensitivity as dragging in the viewport itself -- the cube is a
        second handle on one camera, not a second camera."""
        self.stop_view_animation()
        self._orbit_turntable(dx, dy)
        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    def stop_view_animation(self):
        """Drop any in-flight view animation. Any direct camera input (a
        drag, a zoom, another click) must win immediately rather than fight
        an animation still writing azimuth/elevation underneath it."""
        anim = getattr(self, "_view_anim", None)
        if anim is not None:
            anim.stop()
            self._view_anim = None

    def animate_camera_to(self, azimuth: float, elevation: float,
                           duration_ms: int = 350):
        """Swing the camera round to an orientation instead of cutting to it.

        Interpolates azimuth/elevation/roll together, so a rolled camera
        also levels out on the way (the named views are always level). The
        azimuth leg takes the short way round -- lerping 350 -> 10 the naive
        way would spin the long way through 180.
        """
        cam = self._renderer.camera
        self.stop_view_animation()
        cam.fov = cam.DEFAULT_FOV

        start = (cam.azimuth, cam.elevation, cam.roll)
        # Shortest signed arc, so the cube and the model turn the same way
        # the user expects rather than taking the long way round.
        d_az = ((azimuth - cam.azimuth + 180.0) % 360.0) - 180.0
        delta = (d_az, elevation - cam.elevation, -cam.roll)
        if not any(abs(d) > 1e-9 for d in delta):
            return

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration_ms)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def step(t: float):
            cam.azimuth = start[0] + delta[0] * t
            cam.elevation = start[1] + delta[1] * t
            cam.roll = start[2] + delta[2] * t
            if self._measurements:
                self._refresh_measure_labels()
            self.camera_changed.emit()
            self.update()

        def done():
            # Land exactly on the requested values rather than on whatever
            # the last eased sample happened to be.
            cam.azimuth, cam.elevation, cam.roll = azimuth, elevation, 0.0
            self._view_anim = None
            if self._measurements:
                self._refresh_measure_labels()
            self.camera_changed.emit()
            self.update()

        anim.valueChanged.connect(step)
        anim.finished.connect(done)
        self._view_anim = anim
        anim.start()

    def _frame_all(self, cam):
        # Prefer the bounds cached by the last frame_scene() call (always
        # available for data viewers, whose line/point-only geometry has no
        # per-vertex CPU arrays to scan); fall back to deriving bounds live
        # from mesh buffers (today's main-window behavior, still needed for
        # the very first load before frame_scene has ever been called).
        if self._last_bb_min is not None and self._last_bb_max is not None:
            cam.frame_bounds(self._last_bb_min, self._last_bb_max)
            return
        buffers = self._renderer._buffers
        if not buffers:
            return
        all_verts = np.concatenate([
            np.concatenate([b.cpu_v0, b.cpu_v1, b.cpu_v2], axis=0)
            for b in buffers
        ], axis=0)
        bb_min = all_verts.min(axis=0)
        bb_max = all_verts.max(axis=0)
        cam.frame_bounds(bb_min, bb_max)

    def zoom(self, direction: int):
        cam = self._renderer.camera
        factor = 1.03 if direction < 0 else 0.97
        cam.distance = max(0.1, cam.distance * factor)
        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    def scroll_to_visible(self, pt: np.ndarray):
        """Pan the camera target the minimum amount to keep `pt` within the
        visible area (used by data-viewer dialogs to keep a newly-selected
        vertex/face on screen)."""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        cam = self._renderer.camera
        aspect = w / h
        mvp = cam.projection_matrix(aspect) @ cam.view_matrix()
        clip = mvp @ np.array([pt[0], pt[1], pt[2], 1.0], dtype=np.float32)
        if abs(clip[3]) < 1e-9:
            return
        ndc_x = clip[0] / clip[3]
        ndc_y = clip[1] / clip[3]
        threshold = 0.85
        dx_ndc = 0.0
        dy_ndc = 0.0
        if ndc_x > threshold:
            dx_ndc = ndc_x - threshold
        elif ndc_x < -threshold:
            dx_ndc = ndc_x + threshold
        if ndc_y > threshold:
            dy_ndc = ndc_y - threshold
        elif ndc_y < -threshold:
            dy_ndc = ndc_y + threshold
        if dx_ndc == 0.0 and dy_ndc == 0.0:
            return
        view = cam.view_matrix()
        right = view[0, :3].astype(np.float32)
        up = view[1, :3].astype(np.float32)
        half_h = cam.distance * math.tan(math.radians(cam.fov / 2))
        cam.target = (cam.target
                      + right * dx_ndc * half_h * aspect
                      + up * dy_ndc * half_h).astype(np.float32)
        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    # ------------------------------------------------------------------
    # Mouse input
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()
        self.stop_view_animation()   # a click always beats an in-flight swing

        # Measurement owns the plain click while its mode is on, ahead of
        # selection and orbit -- otherwise every measuring click would also
        # spin the camera.
        if (self._measure_mode is not None
                and event.button() == Qt.MouseButton.LeftButton
                and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            self._do_measure_click(pos)
            return

        # Cmd+click → selection (takes priority over everything). Data-viewer
        # subclasses set selectable=False so Ctrl+drag orbits instead — they
        # have their own plain-click pick logic and no AST/original-id concept.
        if self._selectable and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._do_selection(pos)
            return

        # Gizmo drag start (T/R tool active, gizmo visible, axis hit)
        if (self._active_tool in (0, 1, 2)
                and self._renderer.show_gizmo
                and self._renderer.selected_id is not None):
            axis = self._renderer.pick_gizmo_axis(pos.x(), pos.y(),
                                                   self.width(), self.height())
            if axis >= 0:
                self._start_gizmo_drag(pos, axis)
                return

        self._last_mouse = pos
        self._mouse_button = event.button()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._gizmo_drag_axis >= 0:
            self._commit_gizmo_drag()
            return
        self._last_mouse = None
        self._mouse_button = None

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()

        if self._measure_mode is not None and self._last_mouse is None:
            snap = self._renderer.snap_at(pos.x(), pos.y(), self.width(), self.height())
            hover = (np.asarray(snap[0], dtype=np.float64), snap[1]) if snap else None
            changed = (hover is None) != (self._measure_hover is None)
            if not changed and hover is not None:
                changed = not np.allclose(hover[0], self._measure_hover[0])
            self._measure_hover = hover
            if changed:
                self._rebuild_measure_overlay()
                self.update()

        # Gizmo drag update
        if self._gizmo_drag_axis >= 0:
            self._update_gizmo_drag(pos)
            return

        # Highlight which gizmo axis the cursor is over
        if (self._active_tool in (0, 1, 2)
                and self._renderer.show_gizmo
                and self._renderer.selected_id is not None
                and self._last_mouse is None):   # not orbiting
            axis = self._renderer.pick_gizmo_axis(pos.x(), pos.y(),
                                                   self.width(), self.height())
            if axis != self._renderer.active_gizmo_axis:
                self._renderer.active_gizmo_axis = axis
                self.update()

        if self._last_mouse is None:
            return
        dx = pos.x() - self._last_mouse.x()
        dy = pos.y() - self._last_mouse.y()
        self._last_mouse = pos

        cam = self._renderer.camera
        if self._mouse_button == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                self._renderer.light_az_offset += dx * 0.5
                self._renderer.light_el_offset += dy * 0.5
                self.update()
                return
            if not self._orbit_enabled:
                return
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                roll_delta = _outer_ring_roll_delta_deg(
                    pos.x(), pos.y(), dx, dy, self.width(), self.height())
                if roll_delta is not None:
                    cam.roll -= roll_delta
                else:
                    # "Orbit" mode: true trackball rotation around the
                    # camera's own current up/right axes (see
                    # Camera.orbit_free's own doc comment) -- unlike
                    # Turntable mode below, this has no elevation clamp and
                    # composes in the camera's local frame rather than a
                    # fixed world one.
                    cam.orbit_free(-dx * 0.5, -dy * 0.5)
            else:
                self._orbit_turntable(dx, dy)
        elif self._mouse_button == Qt.MouseButton.RightButton:
            self._pan_by(dx, dy)

        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    def _orbit_turntable(self, dx: float, dy: float):
        """The default (non-Shift) drag rule: azimuth follows horizontal
        travel, elevation vertical, clamped short of the poles so the
        horizon stays level. Shared with the orientation cube so dragging
        the cube turns the model exactly as dragging the model does."""
        cam = self._renderer.camera
        cam.azimuth -= dx * 0.5
        cam.elevation = max(-89, min(89, cam.elevation + dy * 0.5))

    def _pan_by(self, dx: float, dy: float):
        """Slide the camera target so on-screen content follows a drag of
        (dx, dy) screen pixels. Shared by right-button drag and trackpad
        two-finger scroll — same gesture, two input devices."""
        cam = self._renderer.camera
        az = np.radians(cam.azimuth)
        el = np.radians(cam.elevation)
        right = np.array([-np.sin(az), np.cos(az), 0], dtype=np.float32)
        up_approx = np.array([
            -np.sin(el) * np.cos(az),
            -np.sin(el) * np.sin(az),
            np.cos(el),
        ], dtype=np.float32)
        scale = cam.distance * 0.001 * self._pan_speed
        cam.target -= right * dx * scale
        cam.target += up_approx * dy * scale

    def wheelEvent(self, event: QWheelEvent):
        """Mouse wheel zooms; trackpad two-finger scroll pans.

        `pixelDelta()` is the only reliable way Qt distinguishes the two:
        a trackpad reports a real per-pixel scroll offset, while a mouse
        wheel fills in `angleDelta()` alone (1/8-degree notch units) and
        leaves pixelDelta null. They want opposite things — a wheel notch
        is a discrete zoom step, whereas two-finger scroll is a continuous
        1:1 drag of the content, which is what every other viewer on the
        platform does with it. Cmd+scroll zooms on a trackpad, matching the
        same convention.
        """
        self.stop_view_animation()
        cam = self._renderer.camera
        pixel = event.pixelDelta()
        mods = event.modifiers()
        is_trackpad = not pixel.isNull()

        if mods & Qt.KeyboardModifier.ShiftModifier:
            delta = pixel.y() if is_trackpad else event.angleDelta().y()
            if abs(delta) <= _WHEEL_DEADSPOT:
                return
            cam.fov = max(1.0, min(120.0, cam.fov * (0.99 if delta > 0 else 1.01)))
            self._on_zoom_changed()
        elif is_trackpad and not (mods & Qt.KeyboardModifier.ControlModifier):
            # Pan only slides the target; apparent scale is unchanged, so
            # no _on_zoom_changed() here -- which matters, since a trackpad
            # pan fires a great many events and rebuilding screen-space
            # markers on each would be pure waste.
            self._pan_by(_TRACKPAD_PAN_SIGN * pixel.x(), _TRACKPAD_PAN_SIGN * pixel.y())
        else:
            delta = pixel.y() if is_trackpad else event.angleDelta().y()
            # Fixed 1% step with a deadspot, deliberately not proportional
            # to the delta: one wheel notch should always be one consistent
            # zoom increment, and near-zero deltas otherwise jitter.
            if abs(delta) <= _WHEEL_DEADSPOT:
                return
            self._zoom_to_cursor(0.99 if delta > 0 else 1.01, event.position().toPoint())
            self._on_zoom_changed()

        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()

    def _on_zoom_changed(self):
        """Called after anything that changes the camera's apparent scale
        (wheel/pinch zoom, FOV). No-op here; data-viewer subclasses that
        size vertex markers in screen space override it to rebuild them.

        A hook rather than each subclass overriding wheelEvent, because
        zoom now arrives by two routes -- the wheel and a pinch gesture,
        which never touches wheelEvent -- and markers must track both."""

    def event(self, event):
        """Route macOS/Windows multi-touch gestures, which arrive as
        QNativeGestureEvent rather than through any typed handler."""
        if isinstance(event, QNativeGestureEvent) and self._handle_native_gesture(event):
            return True
        return super().event(event)

    def _handle_native_gesture(self, event: QNativeGestureEvent) -> bool:
        cam = self._renderer.camera
        gesture = event.gestureType()

        if gesture == Qt.NativeGestureType.ZoomNativeGesture:
            # value() is an incremental magnification fraction (+0.05 means
            # "grow 5%"), but _zoom_to_cursor takes a distance multiplier,
            # where smaller means closer -- hence the reciprocal, not 1-value.
            # Guard the degenerate <= -1 case rather than dividing by zero.
            value = event.value()
            if value <= -0.99:
                return True
            # mapFromGlobal(globalPosition()), NOT position(): on macOS Qt
            # fills a native gesture's local position relative to the
            # WINDOW and never remaps it for the child widget that handles
            # it, so position() is offset by the viewport's own origin
            # inside the window. Measured on a real trackpad: a pointer
            # genuinely at (287, 270) in a 642x609 viewport reported
            # position() = (718.7, 328.4) -- past the right edge, which is
            # exactly the "zooms at centre-right whatever I do" symptom.
            # globalPosition() is correct, so map that one ourselves.
            # QWheelEvent is unaffected; ordinary mouse events do get
            # remapped, which is why wheel zoom always centred correctly.
            self._zoom_to_cursor(1.0 / (1.0 + value),
                                 self.mapFromGlobal(event.globalPosition().toPoint()))
            self._on_zoom_changed()
        elif gesture == Qt.NativeGestureType.RotateNativeGesture:
            if not self._orbit_enabled:
                return True
            # Same convention as Shift+drag's outer-ring roll: the applied
            # angle is clockwise-positive on screen and subtracted, since
            # increasing cam.roll spins the *up vector* clockwise and so
            # makes the scene appear to turn the other way.
            cam.roll -= _ROTATE_GESTURE_SIGN * event.value()
        elif gesture == Qt.NativeGestureType.SmartZoomNativeGesture:
            # Two-finger double tap -- the platform gesture for "fit the
            # content", which is exactly View All. set_view_preset already
            # emits/repaints; the reframe changes distance, so markers need
            # the same rebuild a zoom gets.
            self.set_view_preset("all")
            self._on_zoom_changed()
            return True
        else:
            # Begin/End bracket the sequence above and carry no value of
            # their own; swipe/pan natives are unused. Swallow them so the
            # platform keeps delivering the rest of the sequence.
            return True

        if self._measurements:
            self._refresh_measure_labels()
        self.camera_changed.emit()
        self.update()
        return True

    def _zoom_to_cursor(self, factor: float, pos: QPoint):
        """Dolly by `factor` centered on the cursor rather than cam.target
        -- see Camera.zoom_to_point for the actual math (pure, unit-tested
        in test_renderer.py; this is just the Qt-side (w, h, camera_ray)
        plumbing)."""
        cam = self._renderer.camera
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            cam.distance = max(0.1, cam.distance * factor)
            return
        origin, ray_dir = self._renderer.camera_ray(pos.x(), pos.y(), w, h)
        cam.zoom_to_point(origin, ray_dir, factor)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _do_selection(self, pos: QPoint):
        w, h = self.width(), self.height()
        ray_origin, ray_dir = self._renderer.camera_ray(pos.x(), pos.y(), w, h)
        orig_id = self._renderer.ray_cast(ray_origin, ray_dir)
        self.set_selection(orig_id)
        self.selection_changed.emit(orig_id if orig_id is not None else -1)

    # ------------------------------------------------------------------
    # Gizmo drag
    # ------------------------------------------------------------------

    _AXIS_DIRS = [
        np.array([1.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0], dtype=np.float32),
        np.array([0.0, 0.0, 1.0], dtype=np.float32),
    ]

    def _start_gizmo_drag(self, pos: QPoint, axis: int):
        bbox = self._renderer._selected_buffer_bbox()
        if bbox is None:
            return
        center, _ = bbox
        self._drag_gizmo_center = center.copy()
        self._drag_axis_world = self._AXIS_DIRS[axis].copy()
        self._gizmo_drag_axis = axis
        self._renderer.active_gizmo_axis = axis

        if self._active_tool == 1:
            t = self._axis_ring_hit(pos.x(), pos.y())
        else:
            t = self._axis_plane_hit(pos.x(), pos.y())
        if t is None:
            self._gizmo_drag_axis = -1
            return
        self._drag_start_1d = t

        if self._active_tool == 1:
            self._renderer.drag_rotation_axis = axis
        elif self._active_tool == 2:
            self._renderer.drag_scale_axis = axis

    def _update_gizmo_drag(self, pos: QPoint):
        if self._active_tool == 0:
            t = self._axis_plane_hit(pos.x(), pos.y())
            if t is None:
                return
            delta = round(t - self._drag_start_1d, 1)
            self._renderer.drag_offset = self._drag_axis_world * delta
            self._show_delta(f"{'XYZ'[self._gizmo_drag_axis]}  {delta:+.1f}")
        elif self._active_tool == 1:
            t = self._axis_ring_hit(pos.x(), pos.y())
            if t is None:
                return
            raw = t - self._drag_start_1d
            while raw >  180: raw -= 360
            while raw < -180: raw += 360
            delta_deg = round(raw)
            self._renderer.drag_rotation_angle = float(delta_deg)
            self._show_delta(f"{'XYZ'[self._gizmo_drag_axis]}  {delta_deg:+.0f}°")
        else:
            t = self._axis_plane_hit(pos.x(), pos.y())
            if t is None:
                return
            gizmo_len = self._renderer.camera.distance * 0.14
            raw_factor = 1.0 + (t - self._drag_start_1d) / max(gizmo_len, 1e-6)
            factor = max(0.1, round(raw_factor, 1))
            from PySide6.QtWidgets import QApplication
            uniform = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._renderer.drag_scale_factor = factor
            self._renderer.drag_scale_uniform = uniform
            axis_name = "XYZ" if uniform else "XYZ"[self._gizmo_drag_axis]
            self._show_delta(f"{axis_name}  ×{factor:.1f}")
        self.update()

    def _show_delta(self, text: str):
        self._delta_label.setText(text)
        self._delta_label.adjustSize()
        x = (self.width() - self._delta_label.width()) // 2
        y = self.height() - self._delta_label.height() - 24
        self._delta_label.move(x, y)
        self._delta_label.show()

    def _commit_gizmo_drag(self):
        self._delta_label.hide()
        self._renderer.active_gizmo_axis = -1

        if self._active_tool == 0:
            offset = self._renderer.drag_offset.copy()
            self._renderer.drag_offset = np.zeros(3, dtype=np.float32)
            self._gizmo_drag_axis = -1
            self.update()
            dx = round(float(offset[0]), 1)
            dy = round(float(offset[1]), 1)
            dz = round(float(offset[2]), 1)
            if abs(dx) + abs(dy) + abs(dz) > 1e-4:
                self.translate_committed.emit(dx, dy, dz)
        elif self._active_tool == 1:
            angle = self._renderer.drag_rotation_angle
            axis  = self._gizmo_drag_axis
            self._renderer.drag_rotation_angle = 0.0
            self._renderer.drag_rotation_axis  = -1
            self._gizmo_drag_axis = -1
            self.update()
            if angle != 0:
                self.rotate_committed.emit(axis, float(angle))
        else:
            factor  = self._renderer.drag_scale_factor
            axis    = self._gizmo_drag_axis
            uniform = self._renderer.drag_scale_uniform
            self._renderer.drag_scale_factor  = 1.0
            self._renderer.drag_scale_axis    = -1
            self._renderer.drag_scale_uniform = False
            self._gizmo_drag_axis = -1
            self.update()
            if abs(factor - 1.0) > 0.05:
                self.scale_committed.emit(axis, factor, uniform)

    def _axis_ring_hit(self, px: float, py: float) -> float | None:
        """
        Intersect camera ray with the ring's plane (normal = drag axis through center).
        Returns the angle in degrees of the hit point relative to the ring's reference frame.
        """
        w, h = self.width(), self.height()
        ray_o, ray_d = self._renderer.camera_ray(px, py, w, h)

        axis   = self._drag_axis_world.astype(np.float64)
        center = self._drag_gizmo_center.astype(np.float64)
        ray_o  = ray_o.astype(np.float64)
        ray_d  = ray_d.astype(np.float64)

        denom = float(np.dot(ray_d, axis))
        if abs(denom) < 1e-8:
            return None
        t = float(np.dot(center - ray_o, axis)) / denom
        hit = ray_o + t * ray_d

        radial = hit - center
        ai = self._gizmo_drag_axis
        p1 = _RING_PERP1[ai]
        p2 = _RING_PERP2[ai]
        return float(np.degrees(np.arctan2(np.dot(radial, p2), np.dot(radial, p1))))

    def _axis_plane_hit(self, px: float, py: float) -> float | None:
        """
        Intersect camera ray with the plane that contains the drag axis
        and faces the camera. Returns the 1-D position along the axis.
        """
        w, h = self.width(), self.height()
        ray_o, ray_d = self._renderer.camera_ray(px, py, w, h)

        axis = self._drag_axis_world
        cam_dir = (self._renderer.camera.eye_position()
                   - self._renderer.camera.target).astype(np.float64)
        cam_norm = np.linalg.norm(cam_dir)
        if cam_norm < 1e-8:
            return None
        cam_dir /= cam_norm

        # Plane normal: component of camera direction perpendicular to axis
        n = cam_dir - np.dot(cam_dir, axis.astype(np.float64)) * axis.astype(np.float64)
        n_len = np.linalg.norm(n)
        if n_len < 1e-6:
            # Camera looking along axis — pick any perpendicular plane
            ref = np.array([0, 1, 0], dtype=np.float64)
            if abs(np.dot(axis, ref)) > 0.9:
                ref = np.array([1, 0, 0], dtype=np.float64)
            n = np.cross(axis.astype(np.float64), ref)
            n /= np.linalg.norm(n)
        else:
            n /= n_len

        denom = float(np.dot(ray_d.astype(np.float64), n))
        if abs(denom) < 1e-8:
            return None

        center = self._drag_gizmo_center.astype(np.float64)
        t_plane = float(np.dot(center - ray_o.astype(np.float64), n)) / denom
        hit = ray_o.astype(np.float64) + t_plane * ray_d.astype(np.float64)
        return float(np.dot(hit - center, axis.astype(np.float64)))
