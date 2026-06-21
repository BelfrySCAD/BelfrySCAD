from __future__ import annotations
import numpy as np

from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel
from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent

from neuscad.engine.renderer import SceneRenderer


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


class Viewport(QOpenGLWidget):
    selection_changed   = Signal(int)                    # originalID or -1
    translate_committed = Signal(float, float, float)    # world-space delta
    rotate_committed    = Signal(int, float)             # axis (0/1/2), degrees
    scale_committed     = Signal(int, float, bool)       # axis (0/1/2), factor, uniform
    camera_changed      = Signal()                       # emitted on any camera movement

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self._ctx = None
        self._renderer = SceneRenderer()
        self._last_mouse: QPoint | None = None
        self._mouse_button: Qt.MouseButton | None = None
        self.setMouseTracking(True)
        self._frame_count: int = 0

        # Tool state
        self._active_tool: int = -1   # -1=none, 0=translate, 1=rotate, 2=scale

        # Delta overlay label
        self._delta_label = QLabel("", self)
        self._delta_label.setStyleSheet(
            "QLabel { background: rgba(0,0,0,160); color: white;"
            " padding: 4px 10px; border-radius: 4px;"
            " font-family: Menlo; font-size: 13px; }"
        )
        self._delta_label.hide()

        # Gizmo drag state
        self._gizmo_drag_axis: int = -1
        self._drag_axis_world: np.ndarray = np.zeros(3, dtype=np.float32)
        self._drag_gizmo_center: np.ndarray = np.zeros(3, dtype=np.float32)
        self._drag_start_1d: float = 0.0

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------

    def initializeGL(self):
        import moderngl
        self._ctx = moderngl.create_context(require=330)
        self._renderer.initialize(self._ctx)

    def resizeGL(self, w, h):
        if self._ctx:
            self._ctx.viewport = (0, 0, w, h)
            self._renderer.set_viewport(w, h)

    def paintGL(self):
        try:
            fbo_id = self.defaultFramebufferObject()
            self._renderer.paint(qt_fbo_id=fbo_id)
            self._frame_count += 1
        except Exception as e:
            import traceback
            print("paintGL error:", traceback.format_exc())

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

    def frame_scene(self, bb_min, bb_max):
        self._renderer.camera.frame_bounds(bb_min, bb_max)
        self.camera_changed.emit()
        self.update()

    def set_background_color(self, r, g, b):
        self._renderer._bg_color = (r, g, b, 1.0)
        self.update()

    def set_active_tool(self, tool_id: int):
        self._active_tool = tool_id
        self._renderer.show_gizmo = tool_id in (0, 1, 2)
        self._renderer.gizmo_type = tool_id
        self._gizmo_drag_axis = -1
        self._renderer.active_gizmo_axis = -1
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

    # ------------------------------------------------------------------
    # Camera view presets
    # ------------------------------------------------------------------

    def set_view_preset(self, preset: str):
        cam = self._renderer.camera
        if preset == "top":
            cam.azimuth, cam.elevation = 0, 90
        elif preset == "bottom":
            cam.azimuth, cam.elevation = 0, -90
        elif preset == "front":
            cam.azimuth, cam.elevation = 270, 0
        elif preset == "back":
            cam.azimuth, cam.elevation = 90, 0
        elif preset == "left":
            cam.azimuth, cam.elevation = 180, 0
        elif preset == "right":
            cam.azimuth, cam.elevation = 0, 0
        elif preset == "iso":
            cam.azimuth, cam.elevation = 295, 35
        elif preset == "all":
            self._frame_all(cam)
            self.camera_changed.emit()
            self.update()
            return
        self.camera_changed.emit()
        self.update()

    def _frame_all(self, cam):
        import numpy as np
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
        self.camera_changed.emit()
        self.update()

    # ------------------------------------------------------------------
    # Mouse input
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()

        # Cmd+click → selection (takes priority over everything)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
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
            cam.azimuth -= dx * 0.5
            cam.elevation = max(-89, min(89, cam.elevation + dy * 0.5))
        elif self._mouse_button == Qt.MouseButton.RightButton:
            az = np.radians(cam.azimuth)
            el = np.radians(cam.elevation)
            right = np.array([-np.sin(az), np.cos(az), 0], dtype=np.float32)
            up_approx = np.array([
                -np.sin(el) * np.cos(az),
                -np.sin(el) * np.sin(az),
                np.cos(el),
            ], dtype=np.float32)
            scale = cam.distance * 0.001
            cam.target -= right * dx * scale
            cam.target += up_approx * dy * scale

        self.camera_changed.emit()
        self.update()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        cam = self._renderer.camera
        deadspot = 5
        factor = 1.01 if delta < -deadspot else 0.99 if delta > deadspot else 1.0
        cam.distance = max(0.1, cam.distance * factor)
        self.camera_changed.emit()
        self.update()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _do_selection(self, pos: QPoint):
        w, h = self.width(), self.height()
        ray_origin, ray_dir = self._renderer.camera_ray(pos.x(), pos.y(), w, h)
        orig_id = self._renderer.ray_cast(ray_origin, ray_dir)
        self._renderer.selected_id = orig_id
        self.update()
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
