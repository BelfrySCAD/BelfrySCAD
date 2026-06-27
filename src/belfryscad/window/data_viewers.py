"""
Data viewer windows launched from the debugger's variable context menu.

- ListViewer: scrollable table for lists and OscObject values
- VNFViewer: 3D mesh viewer for [vertices, faces] structures
- PathViewer: 2D/3D path viewer with point markers and connecting lines
"""
from __future__ import annotations
import math
import numpy as np

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QCheckBox, QMenu, QLabel, QPushButton,
    QSplitter, QTabWidget, QWidget, QComboBox,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, QPoint, Signal, QTimer
from PySide6.QtGui import (
    QFont, QMouseEvent, QWheelEvent, QFontMetrics, QImage, QPainter, QColor,
)

from belfryscad.engine.renderer import _nice_spacings, _fmt_tick


def _fmt_short(v) -> str:
    from belfryscad.window.debugger import _fmt
    return _fmt(v)


def _is_list(v) -> bool:
    return isinstance(v, list)


def _is_oscobject(v) -> bool:
    from belfryscad.engine.evaluator import OscObject
    return isinstance(v, OscObject)


def _is_numeric_point(v) -> bool:
    return (_is_list(v)
            and len(v) in (2, 3)
            and all(isinstance(x, (int, float)) for x in v))


def _is_path(v) -> bool:
    return (_is_list(v)
            and len(v) >= 2
            and all(_is_numeric_point(p) for p in v))


def _is_grid(v) -> bool:
    return (_is_list(v)
            and len(v) >= 2
            and all(_is_list(row) and len(row) >= 2
                    and all(_is_numeric_point(p) for p in row) for row in v)
            and len(set(len(row) for row in v)) == 1)


def _is_vnf(v) -> bool:
    if not (_is_list(v) and len(v) == 2):
        return False
    verts, faces = v[0], v[1]
    if not (_is_list(verts) and len(verts) >= 3
            and _is_list(faces) and len(faces) >= 1):
        return False
    if not all(_is_numeric_point(p) and len(p) == 3 for p in verts):
        return False
    return all(_is_list(f) and len(f) >= 3
               and all(isinstance(i, (int, float)) for i in f) for f in faces)


_HEADER_STYLE = (
    "QHeaderView::section {"
    "  background-color: #e8e8e8;"
    "  border: 1px solid #c0c0c0;"
    "  padding: 2px 4px;"
    "}"
)


def _style_table_headers(table: QTableWidget):
    table.horizontalHeader().setStyleSheet(_HEADER_STYLE)
    table.verticalHeader().setStyleSheet(_HEADER_STYLE)


# ---------------------------------------------------------------------------
# Shared 3D viewport widget (orbit / pan / zoom, with labelled axes)
# ---------------------------------------------------------------------------

class _SimpleViewport(QOpenGLWidget):
    """Lightweight 3D viewport with orbit camera and labelled axis ticks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._ctx = None
        self._line_prog = None
        self._mesh_prog = None
        self._edge_prog = None
        self._label_prog = None
        self._label_quad_vbo = None
        self._label_quad_vao = None
        self._label_tex_cache: dict[str, tuple] = {}
        self._label_texture_scale = 4
        self._buffers: list[dict] = []
        self._line_buffers: list[dict] = []
        self._point_buffers: list[dict] = []
        self._depth_test_points = False
        self._axes_vbo = None
        self._axes_vao = None
        self._viewport = (400, 300)
        self._last_mouse: QPoint | None = None
        self._mouse_button = None
        self._gl_ready = False
        self._pending_load = None

        # Camera state
        self.azimuth = 295.0
        self.elevation = 35.0
        self.distance = 50.0
        self.target = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        self.fov = 45.0
        self.orthographic = False
        self.show_edges = False
        self.show_axes = True
        self._scene_bb_min: np.ndarray | None = None
        self._scene_bb_max: np.ndarray | None = None

    # -- GL lifecycle --

    def initializeGL(self):
        import moderngl
        self._ctx = moderngl.create_context(require=330)
        self._line_prog = self._ctx.program(
            vertex_shader=_LINE_VERT, fragment_shader=_LINE_FRAG,
        )
        self._mesh_prog = self._ctx.program(
            vertex_shader=_MESH_VERT, fragment_shader=_MESH_FRAG,
        )
        self._edge_prog = self._ctx.program(
            vertex_shader=_LINE_VERT, fragment_shader=_LINE_FRAG,
        )
        self._label_prog = self._ctx.program(
            vertex_shader=_LABEL_VERT, fragment_shader=_LABEL_FRAG,
        )
        quad = np.array([
            [-1.0, -1.0, 0.0, 1.0],
            [ 1.0, -1.0, 1.0, 1.0],
            [ 1.0,  1.0, 1.0, 0.0],
            [-1.0,  1.0, 0.0, 0.0],
        ], dtype=np.float32)
        self._label_quad_vbo = self._ctx.buffer(quad.tobytes())
        self._label_quad_vao = self._ctx.vertex_array(
            self._label_prog,
            [(self._label_quad_vbo, "2f 2f", "in_position", "in_uv")],
        )
        self._gl_ready = True
        if self._pending_load is not None:
            fn = self._pending_load
            self._pending_load = None
            fn()
        # Repaint all other QOpenGLWidgets so their GL state is restored
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            for child in w.findChildren(QOpenGLWidget):
                if child is not self:
                    child.update()

    def resizeGL(self, w, h):
        if self._ctx:
            self._ctx.viewport = (0, 0, w, h)
            self._viewport = (w, h)

    def paintEvent(self, event):
        super().paintEvent(event)

    def paintGL(self):
        if self._ctx is None:
            return
        try:
            self._paintGL_inner()
        except Exception as e:
            import traceback
            print("data_viewers paintGL error:", traceback.format_exc())

    def _paintGL_inner(self):
        import moderngl as mgl
        fbo = self._ctx.detect_framebuffer(self.defaultFramebufferObject())
        fbo.use()
        w, h = self._viewport
        aspect = w / h if h > 0 else 1.0
        self._ctx.clear(0.82, 0.82, 0.82)
        self._ctx.enable(mgl.DEPTH_TEST)

        mvp = self._mvp(aspect)

        # Solid meshes
        if self._buffers:
            if self.show_edges:
                self._ctx.polygon_offset = (2.0, 2.0)
                self._ctx.enable_direct(0x8037)
            view = self._view_matrix()
            light = np.array([0.6, 0.8, 1.0], dtype=np.float32)
            light /= np.linalg.norm(light)
            L_world = (view[:3, :3].T @ light).astype(np.float32)
            L_world /= np.linalg.norm(L_world)
            self._mesh_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            self._mesh_prog["light_dir"].value = tuple(L_world)
            self._mesh_prog["eye_pos"].value = tuple(self._eye_position())
            for buf in self._buffers:
                self._mesh_prog["object_color"].value = buf["color"]
                self._mesh_prog["backface_color"].value = buf.get(
                    "backface_color", (0.8, 0.0, 0.8, 1.0))
                buf["vao"].render()
            if self.show_edges:
                self._ctx.disable_direct(0x8037)
                self._edge_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
                for buf in self._buffers:
                    if buf.get("edge_vao"):
                        buf["edge_vao"].render(mgl.LINES)

        # Lines
        if self._line_buffers:
            old_lw = self._ctx.line_width
            self._ctx.line_width = getattr(self, '_line_width', 1.0)
            self._line_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            for buf in self._line_buffers:
                buf["vao"].render(mgl.LINES)
            self._ctx.line_width = old_lw

        # Points (rendered on top of lines)
        if self._point_buffers:
            if not self._depth_test_points:
                self._ctx.disable(mgl.DEPTH_TEST)
            self._line_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            for buf in self._point_buffers:
                buf["vao"].render(mgl.TRIANGLES)
            if not self._depth_test_points:
                self._ctx.enable(mgl.DEPTH_TEST)

        self._render_extra(mvp)

        # Axes with ticks and labels
        if self.show_axes:
            self._render_axes(mvp)
            self._render_axis_labels(mvp)

    def _render_extra(self, mvp: np.ndarray):
        """Hook for subclasses to render additional geometry."""
        pass

    # -- Axis rendering (ported from SceneRenderer) --

    def _render_axes(self, mvp: np.ndarray):
        import moderngl as mgl
        L = self.distance * 2.5
        label_spacing, major_spacing, minor_spacing = _nice_spacings(L)
        red = np.array([0.85, 0.15, 0.15], dtype=np.float32)
        green = np.array([0.15, 0.65, 0.15], dtype=np.float32)
        blue = np.array([0.25, 0.35, 0.9], dtype=np.float32)
        gray = np.array([0.2, 0.2, 0.2], dtype=np.float32)
        axis_colors = [red, green, blue]

        w, h = self._viewport
        px_to_world = (self.distance
                       * math.tan(math.radians(self.fov / 2))
                       / max(h, 1))
        minor_len = px_to_world * 24
        tick_len = minor_len * 2

        rows: list[np.ndarray] = []

        for i in range(3):
            p0 = np.zeros(3, dtype=np.float32)
            p1 = np.zeros(3, dtype=np.float32)
            p1[i] = float(L)
            rows.append(np.concatenate([p0, axis_colors[i]]))
            rows.append(np.concatenate([p1, axis_colors[i]]))

        for i in range(3):
            p0 = np.zeros(3, dtype=np.float32)
            p1 = np.zeros(3, dtype=np.float32)
            p1[i] = -float(L)
            rows.append(np.concatenate([p0, gray]))
            rows.append(np.concatenate([p1, gray]))

        eye = self._eye_position().astype(np.float64)
        view_dir = eye - np.asarray(self.target, dtype=np.float64)
        view_norm = np.linalg.norm(view_dir)
        end_on_axis = [False, False, False]
        if view_norm > 1e-9:
            view_dir /= view_norm
            for ai in range(3):
                if abs(view_dir[ai]) > math.cos(math.radians(5.0)):
                    end_on_axis[ai] = True

        perp_axis = [1, 0, 1]
        minor_len_actual = tick_len * 0.5
        major_steps = max(1, round(major_spacing / minor_spacing))
        if major_steps <= 2:
            minor_spacing = major_spacing
            major_spacing = label_spacing
            major_steps = max(1, round(major_spacing / minor_spacing))

        k = 1
        while True:
            t = k * minor_spacing
            if t > L + 1e-9:
                break
            is_major = (k % major_steps == 0)
            length = tick_len if is_major else minor_len_actual
            for sign in (1.0, -1.0):
                pos = sign * t
                for ai in range(3):
                    if not is_major and end_on_axis[ai]:
                        continue
                    pi = perp_axis[ai]
                    p0 = np.zeros(3, dtype=np.float32)
                    p1 = np.zeros(3, dtype=np.float32)
                    p0[ai] = float(pos)
                    p1[ai] = float(pos)
                    p0[pi] = 0.0
                    p1[pi] = float(length)
                    color = axis_colors[ai] if sign > 0.0 else gray
                    rows.append(np.concatenate([p0, color]))
                    rows.append(np.concatenate([p1, color]))
            k += 1

        geo = np.array(rows, dtype=np.float32)

        if self._axes_vbo is not None:
            self._axes_vao.release()
            self._axes_vbo.release()

        self._axes_vbo = self._ctx.buffer(geo.tobytes())
        self._axes_vao = self._ctx.vertex_array(
            self._line_prog,
            [(self._axes_vbo, "3f 3f", "in_position", "in_color")],
        )

        self._line_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        self._ctx.enable(mgl.BLEND)
        self._axes_vao.render(mgl.LINES)
        self._ctx.disable(mgl.BLEND)

    def _axis_tick_world_points(self) -> list[tuple[np.ndarray, str, int]]:
        L = self.distance * 2.5
        spacing, _, _ = _nice_spacings(L)
        eye = self._eye_position().astype(np.float64)

        view_dir = eye - np.asarray(self.target, dtype=np.float64)
        view_norm = np.linalg.norm(view_dir)
        end_on_axis = [False, False, False]
        if view_norm > 1e-9:
            view_dir /= view_norm
            for ai in range(3):
                if abs(view_dir[ai]) > math.cos(math.radians(5.0)):
                    end_on_axis[ai] = True

        result = []
        t = spacing
        while t <= L + 1e-9:
            for sign in (1.0, -1.0):
                pos = sign * t
                lbl = _fmt_tick(pos, spacing)
                for ai in range(3):
                    if end_on_axis[ai]:
                        continue
                    world = np.zeros(3, dtype=np.float64)
                    world[ai] = pos
                    result.append((world, lbl, ai))
            t += spacing
        return result

    def _get_label_texture(self, text: str):
        cached = self._label_tex_cache.get(text)
        if cached is not None:
            return cached

        import moderngl as mgl
        font = QFont("Helvetica")
        font.setPixelSize(12 * self._label_texture_scale)
        fm = QFontMetrics(font)
        tw = max(1, fm.horizontalAdvance(text))
        th = max(1, fm.height())

        img = QImage(tw, th, QImage.Format.Format_RGBA8888)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(font)
        painter.setPen(QColor("#222222"))
        painter.drawText(0, fm.ascent(), text)
        painter.end()

        tex = self._ctx.texture((tw, th), 4, bytes(img.constBits()))
        tex.filter = (mgl.LINEAR, mgl.LINEAR)

        result = (tex, tw, th)
        self._label_tex_cache[text] = result
        return result

    def _render_axis_labels(self, mvp: np.ndarray):
        import moderngl as mgl
        w, h = self._viewport
        px_to_world = (self.distance
                       * math.tan(math.radians(self.fov / 2))
                       / max(h, 1))

        view = self._view_matrix()
        right = view[0, :3].astype(np.float64)
        up = view[1, :3].astype(np.float64)
        label_scale = 3.0
        gap = 6 * px_to_world * label_scale

        perp_axis = [1, 0, 1]

        self._ctx.enable(mgl.BLEND)
        self._label_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        self._label_prog["tex"].value = 0

        for world_pos, text, ai in self._axis_tick_world_points():
            tex, tw, th = self._get_label_texture(text)
            half_w = (tw / self._label_texture_scale / 2) * px_to_world * label_scale
            half_h = (th / self._label_texture_scale / 2) * px_to_world * label_scale

            perp_dir = np.zeros(3, dtype=np.float64)
            perp_dir[perp_axis[ai]] = -1.0
            center = world_pos + perp_dir * (half_h + gap)

            tex.use(location=0)
            self._label_prog["center"].write(center.astype(np.float32).tobytes())
            self._label_prog["right"].write(right.astype(np.float32).tobytes())
            self._label_prog["up"].write(up.astype(np.float32).tobytes())
            self._label_prog["half_size"].write(
                np.array([half_w, half_h], dtype=np.float32).tobytes())
            self._label_quad_vao.render(mgl.TRIANGLE_FAN)

        self._ctx.disable(mgl.BLEND)

    # -- Cleanup --

    def hideEvent(self, event):
        super().hideEvent(event)
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            for child in w.findChildren(QOpenGLWidget):
                if child is not self:
                    child.update()

    def closeEvent(self, event):
        self._gl_ready = False
        super().closeEvent(event)

    def _release_all(self):
        for buf in self._buffers + self._line_buffers + self._point_buffers:
            buf["vao"].release()
            buf["vbo"].release()
            if buf.get("edge_vao"):
                buf["edge_vao"].release()
                buf["edge_vbo"].release()
        self._buffers.clear()
        self._line_buffers.clear()
        self._point_buffers.clear()

    # -- Camera math --

    def _eye_position(self) -> np.ndarray:
        az, el = math.radians(self.azimuth), math.radians(self.elevation)
        return self.target + self.distance * np.array([
            math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el),
        ], dtype=np.float32)

    def _view_matrix(self) -> np.ndarray:
        eye = self._eye_position()
        up = np.array([0, 0, 1], dtype=np.float32)
        f = self.target - eye
        fn = np.linalg.norm(f)
        if fn < 1e-9:
            f = np.array([0, 0, -1], dtype=np.float32)
        else:
            f /= fn
        s = np.cross(f, up)
        sn = np.linalg.norm(s)
        if sn < 1e-6:
            s = np.array([1, 0, 0], dtype=np.float32)
        else:
            s /= sn
        u = np.cross(s, f)
        m = np.eye(4, dtype=np.float32)
        m[0, :3] = s
        m[1, :3] = u
        m[2, :3] = -f
        m[:3, 3] = [-s @ eye, -u @ eye, f @ eye]
        return m

    def _projection_matrix(self, aspect: float) -> np.ndarray:
        if self.orthographic:
            half_h = self.distance * math.tan(math.radians(self.fov / 2))
            hw, hh = half_h * aspect, half_h
            m = np.zeros((4, 4), dtype=np.float32)
            m[0, 0] = 1.0 / hw
            m[1, 1] = 1.0 / hh
            m[2, 2] = -2.0 / 20000.0
            m[3, 3] = 1.0
            return m
        fov = math.radians(self.fov)
        t = math.tan(fov / 2)
        near, far = 0.1, 10000.0
        m = np.zeros((4, 4), dtype=np.float32)
        m[0, 0] = 1.0 / (aspect * t)
        m[1, 1] = 1.0 / t
        m[2, 2] = -(far + near) / (far - near)
        m[2, 3] = -2.0 * far * near / (far - near)
        m[3, 2] = -1.0
        return m

    def _mvp(self, aspect: float) -> np.ndarray:
        return self._projection_matrix(aspect) @ self._view_matrix()

    def frame_bounds(self, bb_min: np.ndarray, bb_max: np.ndarray):
        self._scene_bb_min = bb_min.copy()
        self._scene_bb_max = bb_max.copy()
        center = (bb_min + bb_max) / 2
        extent = np.linalg.norm(bb_max - bb_min)
        self.target = center.astype(np.float32)
        self.distance = max(extent * 1.2, 1.0)

    def schedule_load(self, fn):
        """Schedule a geometry load function to run once GL is initialized."""
        if self._gl_ready:
            fn()
        else:
            self._pending_load = fn

    # -- View presets & keyboard shortcuts --

    def set_view_preset(self, preset: str):
        if preset == "top":
            self.azimuth, self.elevation = 0, 90
        elif preset == "bottom":
            self.azimuth, self.elevation = 0, -90
        elif preset == "front":
            self.azimuth, self.elevation = 270, 0
        elif preset == "back":
            self.azimuth, self.elevation = 90, 0
        elif preset == "left":
            self.azimuth, self.elevation = 180, 0
        elif preset == "right":
            self.azimuth, self.elevation = 0, 0
        elif preset == "iso":
            self.azimuth, self.elevation = 295, 35
        elif preset == "all":
            if self._scene_bb_min is not None:
                self.frame_bounds(self._scene_bb_min, self._scene_bb_max)
            self.update()
            return
        self.update()

    # -- Mouse interaction --

    def mousePressEvent(self, event: QMouseEvent):
        self._last_mouse = event.position().toPoint()
        self._mouse_button = event.button()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._last_mouse = None
        self._mouse_button = None

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._last_mouse is None:
            return
        pos = event.position().toPoint()
        dx = pos.x() - self._last_mouse.x()
        dy = pos.y() - self._last_mouse.y()
        self._last_mouse = pos
        if self._mouse_button == Qt.MouseButton.LeftButton:
            self.azimuth -= dx * 0.5
            self.elevation = max(-89, min(89, self.elevation + dy * 0.5))
        elif self._mouse_button == Qt.MouseButton.RightButton:
            view = self._view_matrix()
            right = view[0, :3].astype(np.float32)
            up = view[1, :3].astype(np.float32)
            scale = self.distance * 0.001
            self.target -= right * dx * scale
            self.target += up * dy * scale
        self.update()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        factor = 1.01 if delta < -5 else 0.99 if delta > 5 else 1.0
        new_dist = max(0.1, self.distance * factor)

        pos = event.position()
        w, h = self._viewport
        if w > 0 and h > 0:
            ndc_x = (pos.x() / w) * 2.0 - 1.0
            ndc_y = 1.0 - (pos.y() / h) * 2.0
            aspect = w / h
            half_h = self.distance * math.tan(math.radians(self.fov / 2))
            if self.orthographic:
                dx_world = ndc_x * half_h * aspect
                dy_world = ndc_y * half_h
            else:
                dx_world = ndc_x * half_h * aspect
                dy_world = ndc_y * half_h
            scale_change = 1.0 - new_dist / self.distance
            view = self._view_matrix()
            right = view[0, :3].astype(np.float32)
            up = view[1, :3].astype(np.float32)
            self.target += (right * dx_world + up * dy_world) * scale_change

        self.distance = new_dist
        self.update()

    # -- Geometry upload helpers (must be called after GL init) --

    def upload_mesh(self, positions: np.ndarray, normals: np.ndarray,
                    color: tuple = (0.9, 0.85, 0.1, 1.0),
                    backface_color: tuple | None = None,
                    edge_positions: np.ndarray | None = None,
                    edge_colors: np.ndarray | None = None):
        interleaved = np.concatenate([positions, normals], axis=1).astype(np.float32)
        vbo = self._ctx.buffer(interleaved.tobytes())
        vao = self._ctx.vertex_array(
            self._mesh_prog, [(vbo, "3f 3f", "in_position", "in_normal")],
        )
        buf = {"vbo": vbo, "vao": vao, "color": color}
        if backface_color is not None:
            buf["backface_color"] = backface_color
        if edge_positions is not None and edge_colors is not None:
            edge_data = np.concatenate([edge_positions, edge_colors], axis=1).astype(np.float32)
            edge_vbo = self._ctx.buffer(edge_data.tobytes())
            edge_vao = self._ctx.vertex_array(
                self._edge_prog, [(edge_vbo, "3f 3f", "in_position", "in_color")],
            )
            buf["edge_vbo"] = edge_vbo
            buf["edge_vao"] = edge_vao
        self._buffers.append(buf)

    def upload_lines(self, data: np.ndarray):
        vbo = self._ctx.buffer(data.astype(np.float32).tobytes())
        vao = self._ctx.vertex_array(
            self._line_prog, [(vbo, "3f 3f", "in_position", "in_color")],
        )
        self._line_buffers.append({"vbo": vbo, "vao": vao})

    def upload_points(self, data: np.ndarray):
        vbo = self._ctx.buffer(data.astype(np.float32).tobytes())
        vao = self._ctx.vertex_array(
            self._line_prog, [(vbo, "3f 3f", "in_position", "in_color")],
        )
        self._point_buffers.append({"vbo": vbo, "vao": vao})


# Shaders
_LINE_VERT = """
#version 330 core
in vec3 in_position;
in vec3 in_color;
uniform mat4 mvp;
out vec3 v_color;
void main() {
    gl_Position = mvp * vec4(in_position, 1.0);
    v_color = in_color;
}
"""

_LINE_FRAG = """
#version 330 core
in vec3 v_color;
out vec4 fragColor;
void main() {
    fragColor = vec4(v_color, 1.0);
}
"""

_MESH_VERT = """
#version 330 core
in vec3 in_position;
in vec3 in_normal;
uniform mat4 mvp;
out vec3 v_normal;
out vec3 v_world_pos;
void main() {
    v_world_pos = in_position;
    v_normal = in_normal;
    gl_Position = mvp * vec4(in_position, 1.0);
}
"""

_MESH_FRAG = """
#version 330 core
in vec3 v_normal;
in vec3 v_world_pos;
uniform vec4 object_color;
uniform vec4 backface_color;
uniform vec3 light_dir;
uniform vec3 eye_pos;
out vec4 fragColor;
void main() {
    vec3 n = normalize(v_normal);
    vec3 col;
    if (!gl_FrontFacing) {
        n = -n;
        col = backface_color.rgb;
    } else {
        col = object_color.rgb;
    }
    vec3 L = normalize(light_dir);
    float diff = max(dot(n, L), 0.0);
    float fill = max(dot(n, normalize(-light_dir * vec3(1.0, 1.0, 0.3))), 0.0);
    vec3 lit = 0.35 * col + 0.50 * diff * col + 0.20 * fill * col;
    vec3 V = normalize(eye_pos - v_world_pos);
    vec3 H = normalize(L + V);
    float spec = pow(max(dot(n, H), 0.0), 64.0) * 0.5;
    lit += vec3(spec);
    fragColor = vec4(lit, object_color.a);
}
"""

_LABEL_VERT = """
#version 330 core
in vec2 in_position;
in vec2 in_uv;
uniform mat4 mvp;
uniform vec3 center;
uniform vec3 right;
uniform vec3 up;
uniform vec2 half_size;
out vec2 v_uv;
void main() {
    vec3 world = center
        + right * (in_position.x * half_size.x)
        + up    * (in_position.y * half_size.y);
    gl_Position = mvp * vec4(world, 1.0);
    v_uv = in_uv;
}
"""

_LABEL_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D tex;
out vec4 fragColor;
void main() {
    fragColor = texture(tex, v_uv);
}
"""


# ---------------------------------------------------------------------------
# List / Object Viewer
# ---------------------------------------------------------------------------

class ListViewer(QDialog):
    """Scrollable table displaying a list (with indices) or OscObject (with keys)."""

    def __init__(self, title: str, value, parent=None):
        super().__init__(parent)
        self._title = title
        self.setWindowTitle(f"List Viewer: {title}")
        self.resize(500, 400)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._value = value

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setColumnCount(2)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._entries: list[tuple[str, object]] = []
        self._populate(value)

    def _populate(self, value):
        if _is_oscobject(value):
            self._table.setHorizontalHeaderLabels(["Key", "Value"])
            for k, v in value.items():
                self._entries.append((str(k), v))
        elif _is_list(value):
            self._table.setHorizontalHeaderLabels(["Index", "Value"])
            for i, v in enumerate(value):
                self._entries.append((str(i), v))
        else:
            return

        self._table.setRowCount(len(self._entries))
        for row, (key, val) in enumerate(self._entries):
            key_item = QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, key_item)
            val_item = QTableWidgetItem(_fmt_short(val))
            val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, val_item)

    def _context_menu(self, pos):
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._entries):
            return
        key, val = self._entries[row]

        menu = QMenu(self)
        sub_title = f"{self._title}[{key}]"
        if _is_list(val) or _is_oscobject(val):
            menu.addAction("View in List...", lambda: _open_list_viewer(
                sub_title, val, self))
        if _is_vnf(val):
            menu.addAction("View as VNF...", lambda: _open_vnf_viewer(
                sub_title, val, self))
        if _is_path(val):
            menu.addAction("View as Path...", lambda: _open_path_viewer(
                sub_title, val, self))
        if menu.isEmpty():
            return
        menu.exec(self._table.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------------------
# VNF Viewport (adds face picking and highlight)
# ---------------------------------------------------------------------------

class _VNFViewport(_SimpleViewport):
    face_clicked = Signal(int)
    def __init__(self, parent=None):
        super().__init__(parent)
        self._depth_test_points = True
        self.setMouseTracking(True)
        self._cpu_positions: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._tri_to_face: np.ndarray = np.zeros(0, dtype=np.int32)
        self._highlight_vao = None
        self._highlight_vbo = None
        self._vert_marker_vao_r = None
        self._vert_marker_vbo_r = None
        self._vert_marker_vao_w = None
        self._vert_marker_vbo_w = None
        self._vert_blink_red = True
        self._vert_indices: list[int] = []
        self._vert_blink_timer = QTimer(self)
        self._vert_blink_timer.setInterval(250)
        self._vert_blink_timer.timeout.connect(self._blink_tick)
        self._verts_3d: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._selected_face: int = -1
        self._drag_started = False
        self._press_pos: QPoint | None = None

    def set_face_data(self, cpu_positions: np.ndarray, tri_to_face: np.ndarray,
                      verts_3d: np.ndarray | None = None):
        self._cpu_positions = cpu_positions
        self._tri_to_face = tri_to_face
        if verts_3d is not None:
            self._verts_3d = verts_3d

    def highlight_face(self, face_idx: int):
        if face_idx == self._selected_face:
            return
        self._selected_face = face_idx
        self._rebuild_highlight()
        self.update()

    def _rebuild_highlight(self):
        if self._highlight_vao is not None:
            self._highlight_vao.release()
            self._highlight_vbo.release()
            self._highlight_vao = None
            self._highlight_vbo = None

        if self._selected_face < 0 or not self._gl_ready:
            return

        mask = self._tri_to_face == self._selected_face
        tri_indices = np.where(mask)[0]
        if len(tri_indices) == 0:
            return

        positions = []
        normals = []
        for ti in tri_indices:
            v0 = self._cpu_positions[ti * 3]
            v1 = self._cpu_positions[ti * 3 + 1]
            v2 = self._cpu_positions[ti * 3 + 2]
            n = np.cross(v1 - v0, v2 - v0)
            ln = np.linalg.norm(n)
            if ln > 0:
                n /= ln
            positions.extend([v0, v1, v2])
            normals.extend([n, n, n])

        pos_arr = np.array(positions, dtype=np.float32)
        norm_arr = np.array(normals, dtype=np.float32)
        interleaved = np.concatenate([pos_arr, norm_arr], axis=1)
        self._highlight_vbo = self._ctx.buffer(interleaved.tobytes())
        self._highlight_vao = self._ctx.vertex_array(
            self._mesh_prog, [(self._highlight_vbo, "3f 3f", "in_position", "in_normal")],
        )

    def _blink_tick(self):
        self._vert_blink_red = not self._vert_blink_red
        self.update()

    def highlight_vertices(self, indices: list[int]):
        self._release_vert_markers()
        self._vert_indices = []

        if not indices or not self._gl_ready or len(self._verts_3d) == 0:
            self._vert_blink_timer.stop()
            self.update()
            return

        valid_indices = [vi for vi in indices if 0 <= vi < len(self._verts_3d)]
        self._vert_indices = valid_indices
        self._vert_blink_red = True
        self._vert_blink_timer.start()
        self._build_vert_markers()
        self.update()

    def _release_vert_markers(self):
        for attr in ("_vert_marker_vao_r", "_vert_marker_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_vert_marker_vbo_r", "_vert_marker_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_vert_markers(self):
        self._release_vert_markers()
        if not self._vert_indices or not self._gl_ready:
            return

        _, vh = self._viewport
        if vh > 0:
            world_per_px = 2.0 * self.distance * math.tan(math.radians(self.fov / 2)) / vh
        else:
            world_per_px = self.distance * 0.003
        r = world_per_px * 3.5
        px = np.array([r, 0, 0])
        nx = np.array([-r, 0, 0])
        py = np.array([0, r, 0])
        ny = np.array([0, -r, 0])
        pz = np.array([0, 0, r])
        nz = np.array([0, 0, -r])
        octa_faces = [
            (pz, px, py), (pz, py, nx), (pz, nx, ny), (pz, ny, px),
            (nz, py, px), (nz, nx, py), (nz, ny, nx), (nz, px, ny),
        ]

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_vert_marker_vao_r", "_vert_marker_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_vert_marker_vao_w", "_vert_marker_vbo_w"),
        ]:
            tris = []
            for vi in self._vert_indices:
                pt = self._verts_3d[vi]
                for v0, v1, v2 in octa_faces:
                    tris.append(np.concatenate([pt + v0, color_val]))
                    tris.append(np.concatenate([pt + v1, color_val]))
                    tris.append(np.concatenate([pt + v2, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._line_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if self._vert_indices:
            self._build_vert_markers()

    def _render_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        # Vertex markers (swap red/white)
        vao = self._vert_marker_vao_r if self._vert_blink_red else self._vert_marker_vao_w
        if vao is not None:
            self._line_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            vao.render(mgl.TRIANGLES)

        if self._highlight_vao is None:
            return
        import moderngl as mgl
        self._ctx.polygon_offset = (-1.0, -1.0)
        self._ctx.enable_direct(0x8037)
        view = self._view_matrix()
        light = np.array([0.6, 0.8, 1.0], dtype=np.float32)
        light /= np.linalg.norm(light)
        L_world = (view[:3, :3].T @ light).astype(np.float32)
        L_world /= np.linalg.norm(L_world)
        self._mesh_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        self._mesh_prog["light_dir"].value = tuple(L_world)
        self._mesh_prog["eye_pos"].value = tuple(self._eye_position())
        self._mesh_prog["object_color"].value = (0.2, 0.9, 0.3, 1.0)
        self._mesh_prog["backface_color"].value = (0.8, 0.0, 0.8, 1.0)
        self._highlight_vao.render()
        self._ctx.disable_direct(0x8037)

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None and self._last_mouse is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._last_mouse is None and self._vert_indices:
            pos = event.position().toPoint()
            w, h = self._viewport
            aspect = w / h if h > 0 else 1.0
            mvp = self._mvp(aspect)
            best_idx = -1
            best_dist_sq = float("inf")
            threshold = 12.0
            for vi in self._vert_indices:
                pt = self._verts_3d[vi]
                clip = mvp @ np.array([pt[0], pt[1], pt[2], 1.0], dtype=np.float32)
                if clip[3] == 0:
                    continue
                ndc = clip[:2] / clip[3]
                sx = (ndc[0] * 0.5 + 0.5) * w
                sy = (1.0 - (ndc[1] * 0.5 + 0.5)) * h
                dx, dy = sx - pos.x(), sy - pos.y()
                d2 = dx * dx + dy * dy
                if d2 < best_dist_sq:
                    best_dist_sq = d2
                    best_idx = vi
            if best_idx >= 0 and best_dist_sq < threshold * threshold:
                pt = self._verts_3d[best_idx]
                self.setToolTip(f"[{best_idx}]: ({pt[0]:g}, {pt[1]:g}, {pt[2]:g})")
            else:
                self.setToolTip("")
        elif not self._vert_indices:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            face = self._pick_face(pos.x(), pos.y())
            self.highlight_face(face)
            self.face_clicked.emit(face)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def _pick_face(self, px: float, py: float) -> int:
        n_tris = len(self._cpu_positions) // 3
        if n_tris == 0:
            return -1
        w, h = self._viewport
        if w == 0 or h == 0:
            return -1

        aspect = w / h
        view = self._view_matrix()
        proj = self._projection_matrix(aspect)
        inv_vp = np.linalg.inv((proj @ view).astype(np.float64))

        ndc_x = (2.0 * px / w) - 1.0
        ndc_y = 1.0 - (2.0 * py / h)

        near_ndc = np.array([ndc_x, ndc_y, -1.0, 1.0], dtype=np.float64)
        far_ndc = np.array([ndc_x, ndc_y, 1.0, 1.0], dtype=np.float64)
        near_w = inv_vp @ near_ndc
        far_w = inv_vp @ far_ndc
        near_w /= near_w[3]
        far_w /= far_w[3]

        ray_o = near_w[:3]
        ray_d = far_w[:3] - near_w[:3]
        ray_d /= np.linalg.norm(ray_d)

        # Vectorized Moller-Trumbore
        v0 = self._cpu_positions[0::3].astype(np.float64)
        v1 = self._cpu_positions[1::3].astype(np.float64)
        v2 = self._cpu_positions[2::3].astype(np.float64)

        e1 = v1 - v0
        e2 = v2 - v0
        h = np.cross(ray_d, e2)
        a = np.sum(e1 * h, axis=1)

        valid = np.abs(a) > 1e-10
        f = np.where(valid, 1.0 / np.where(valid, a, 1.0), 0.0)

        s = ray_o - v0
        u = f * np.sum(s * h, axis=1)
        valid &= (u >= 0.0) & (u <= 1.0)

        q = np.cross(s, e1)
        v = f * np.sum(ray_d * q, axis=1)
        valid &= (v >= 0.0) & (u + v <= 1.0)

        t = f * np.sum(e2 * q, axis=1)
        valid &= t > 1e-10

        if not np.any(valid):
            return -1

        t_vals = np.where(valid, t, np.inf)
        best_tri = int(np.argmin(t_vals))
        return int(self._tri_to_face[best_tri])

    def closeEvent(self, event):
        self._vert_blink_timer.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# VNF Viewer
# ---------------------------------------------------------------------------

class VNFViewer(QDialog):
    """3D mesh viewer for VNF [vertices, faces] structures with vertex/face tables."""

    def __init__(self, title: str, vnf_value: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"VNF Viewer: {title}")
        self.resize(900, 560)
        self._vnf = vnf_value
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Viewport — match main window's perspective mode
        self._vp = _VNFViewport(splitter)
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            if hasattr(w, '_viewport'):
                self._vp.orthographic = w._viewport._renderer.camera.orthographic
                break
        self._vp.face_clicked.connect(self._on_viewport_face_clicked)
        splitter.addWidget(self._vp)

        # Tables in a tab widget
        self._tab_widget = QTabWidget(splitter)

        self._vert_table = self._make_vert_table(vnf_value[0])
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._tab_widget.addTab(self._vert_table, f"Vertices ({len(vnf_value[0])})")

        self._face_table = self._make_face_table(vnf_value[1])
        self._face_table.itemSelectionChanged.connect(self._on_face_table_selection)
        self._tab_widget.addTab(self._face_table, f"Faces ({len(vnf_value[1])})")

        splitter.addWidget(self._tab_widget)
        vt = self._vert_table
        fm = vt.fontMetrics()
        vh_w = fm.horizontalAdvance(str(max(len(vnf_value[0]) - 1, 0))) + 20
        table_w = (vh_w
                   + sum(vt.columnWidth(j) for j in range(vt.columnCount()))
                   + vt.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._load_mesh)

    @staticmethod
    def _make_vert_table(verts) -> QTableWidget:
        t = QTableWidget(len(verts), 3)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["X", "Y", "Z"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(verts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, v in enumerate(verts):
            for j in range(3):
                item = QTableWidgetItem(f"{v[j]:g}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(3):
            t.setColumnWidth(j, min_w)
        return t

    @staticmethod
    def _make_face_table(faces) -> QTableWidget:
        t = QTableWidget(len(faces), 1)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["Vertex Indices"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(faces))])
        _style_table_headers(t)
        t.horizontalHeader().setStretchLastSection(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, f in enumerate(faces):
            text = "[" + ", ".join(str(int(idx)) for idx in f) + "]"
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(i, 0, item)
        return t

    def _load_mesh(self):
        verts_raw, faces_raw = self._vnf
        verts = np.array(verts_raw, dtype=np.float32)
        all_positions = []
        all_normals = []
        all_edge_starts = []
        all_edge_ends = []
        tri_to_face = []

        for fi, face in enumerate(faces_raw):
            idxs = [int(i) for i in face]
            if len(idxs) < 3:
                continue
            v0 = verts[idxs[0]]
            for k in range(1, len(idxs) - 1):
                v1, v2 = verts[idxs[k]], verts[idxs[k + 1]]
                # Reverse winding: OpenSCAD uses CW from outside, OpenGL expects CCW
                n = np.cross(v2 - v0, v1 - v0)
                ln = np.linalg.norm(n)
                if ln > 0:
                    n /= ln
                all_positions.extend([v0, v2, v1])
                all_normals.extend([n, n, n])
                tri_to_face.append(fi)
            for k in range(len(idxs)):
                all_edge_starts.append(verts[idxs[k]])
                all_edge_ends.append(verts[idxs[(k + 1) % len(idxs)]])

        if not all_positions:
            return

        positions = np.array(all_positions, dtype=np.float32)
        normals = np.array(all_normals, dtype=np.float32)

        self._vp.set_face_data(positions, np.array(tri_to_face, dtype=np.int32), verts)

        edge_color = np.array([0.15, 0.15, 0.15], dtype=np.float32)
        starts = np.array(all_edge_starts, dtype=np.float32)
        ends = np.array(all_edge_ends, dtype=np.float32)
        n_edges = len(starts)
        cols = np.tile(edge_color, (n_edges, 1))
        edge_data = np.empty((n_edges * 2, 6), dtype=np.float32)
        edge_data[0::2] = np.concatenate([starts, cols], axis=1)
        edge_data[1::2] = np.concatenate([ends, cols], axis=1)

        self._vp.upload_mesh(positions, normals,
                             color=(0.9, 0.85, 0.1, 1.0),
                             edge_positions=edge_data[:, :3],
                             edge_colors=edge_data[:, 3:])

        bb_min = verts.min(axis=0)
        bb_max = verts.max(axis=0)
        self._vp.frame_bounds(bb_min, bb_max)
        self._vp.update()

    def _on_vert_table_selection(self):
        if self._syncing:
            return
        rows = self._vert_table.selectionModel().selectedRows()
        indices = [r.row() for r in rows]
        self._vp.highlight_vertices(indices)

    def _on_face_table_selection(self):
        if self._syncing:
            return
        rows = self._face_table.selectionModel().selectedRows()
        if rows:
            face_idx = rows[0].row()
            self._syncing = True
            self._vp.highlight_face(face_idx)
            self._select_face_vertices(face_idx)
            self._syncing = False
        else:
            self._syncing = True
            self._vp.highlight_face(-1)
            self._vert_table.clearSelection()
            self._vp.highlight_vertices([])
            self._syncing = False

    def _on_viewport_face_clicked(self, face_idx: int):
        if self._syncing:
            return
        self._syncing = True
        self._tab_widget.setCurrentIndex(1)
        if 0 <= face_idx < self._face_table.rowCount():
            self._face_table.selectRow(face_idx)
            self._face_table.scrollTo(self._face_table.model().index(face_idx, 0))
            self._select_face_vertices(face_idx)
        else:
            self._face_table.clearSelection()
            self._vert_table.clearSelection()
            self._vp.highlight_vertices([])
        self._syncing = False

    def _select_face_vertices(self, face_idx: int):
        """Select the vertices referenced by the given face in the vertex table."""
        from PySide6.QtCore import QItemSelectionModel
        faces_raw = self._vnf[1]
        if face_idx < 0 or face_idx >= len(faces_raw):
            return
        vert_indices = [int(i) for i in faces_raw[face_idx]]
        sel = self._vert_table.selectionModel()
        sel.clearSelection()
        model = self._vert_table.model()
        for vi in vert_indices:
            if 0 <= vi < self._vert_table.rowCount():
                idx = model.index(vi, 0)
                sel.select(idx, QItemSelectionModel.SelectionFlag.Select
                           | QItemSelectionModel.SelectionFlag.Rows)
        self._vp.highlight_vertices(vert_indices)



# ---------------------------------------------------------------------------
# Path Viewer
# ---------------------------------------------------------------------------

class PathViewer(QDialog):
    """2D/3D path viewer with vertex table, selectable markers, and hover tooltips."""

    def __init__(self, title: str, path_value: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Path Viewer: {title}")
        self.resize(900, 520)

        self._path = path_value
        self._is_2d = all(len(p) == 2 for p in path_value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _PathViewport(path_value, self._is_2d, self)
        splitter.addWidget(self._vp)

        self._vert_table = self._make_vert_table(path_value, self._is_2d)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.addWidget(QLabel(f"Path Points ({len(path_value)})"))
        tc_layout.addWidget(self._vert_table, 1)
        splitter.addWidget(table_container)
        t = self._vert_table
        fm = t.fontMetrics()
        vh_w = max(fm.horizontalAdvance(str(max(len(path_value) - 1, 0))),
                   fm.horizontalAdvance("0000")) + 20
        table_w = (vh_w
                   + sum(t.columnWidth(j) for j in range(t.columnCount()))
                   + t.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        self._closed_cb = QCheckBox("Close Path")
        self._closed_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._closed_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._closed_cb)
        self._bezier_cb = QCheckBox("Bezier")
        self._bezier_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._bezier_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._bezier_cb)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._do_initial_load)

    @staticmethod
    def _make_vert_table(path_value: list, is_2d: bool) -> QTableWidget:
        cols = 2 if is_2d else 3
        t = QTableWidget(len(path_value), cols)
        t.setFont(QFont("Menlo", 11))
        headers = ["X", "Y"] if is_2d else ["X", "Y", "Z"]
        t.setHorizontalHeaderLabels(headers)
        t.setVerticalHeaderLabels([str(i) for i in range(len(path_value))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, p in enumerate(path_value):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(cols):
            t.setColumnWidth(j, min_w)
        return t

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        indices = sorted(r.row() for r in rows)
        self._vp.set_selected(indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        self._vert_table.clearSelection()
        if 0 <= vi < self._vert_table.rowCount():
            self._vert_table.selectRow(vi)

    def _do_initial_load(self):
        self._vp.load_path(self._path, self._closed_cb.isChecked(),
                           self._bezier_cb.isChecked())

    def _rebuild(self, _=None):
        if self._vp._gl_ready:
            self._vp.load_path(self._path, self._closed_cb.isChecked(),
                               self._bezier_cb.isChecked())



class _PathViewport(_SimpleViewport):
    """Viewport subclass with selectable vertex markers and hover tooltips."""
    vertex_clicked = Signal(int)

    def __init__(self, path_value: list, is_2d: bool, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._line_width = 2.0
        self._press_pos = None
        self._drag_started = False
        self._path_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._is_2d = is_2d
        self._selected_indices: list[int] = []
        self._blink_red = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(250)
        self._blink_timer.timeout.connect(self._blink_tick)
        # Separate buffers for selected vertex markers (red/white blink pair)
        self._sel_vao_r = None
        self._sel_vbo_r = None
        self._sel_vao_w = None
        self._sel_vbo_w = None
        if is_2d:
            self.azimuth = 0.0
            self.elevation = 90.0
            self.orthographic = True
        else:
            self.orthographic = False

    def _blink_tick(self):
        self._blink_red = not self._blink_red
        self.update()

    @staticmethod
    def _tessellate_bezier(pts: np.ndarray, closed: bool,
                           steps: int = 32) -> list[np.ndarray]:
        """Return list of line-segment endpoint pairs for cubic Bezier curves.

        Each group of 4 points (P0, C1, C2, P3) is one cubic segment, with
        shared endpoints between consecutive segments (P3 of one = P0 of next).
        Open path: needs 3k+1 points for k segments.
        Closed path: needs 3k points (last segment wraps to pts[0]).
        """
        n = len(pts)
        segments: list[tuple[int, int, int, int]] = []
        if closed:
            num_segs = n // 3
            for s in range(num_segs):
                i0 = (s * 3) % n
                i1 = (s * 3 + 1) % n
                i2 = (s * 3 + 2) % n
                i3 = (s * 3 + 3) % n
                segments.append((i0, i1, i2, i3))
        else:
            num_segs = (n - 1) // 3
            for s in range(num_segs):
                i0 = s * 3
                segments.append((i0, i0 + 1, i0 + 2, i0 + 3))

        pairs: list[np.ndarray] = []
        t_vals = np.linspace(0.0, 1.0, steps + 1, dtype=np.float32)
        for i0, i1, i2, i3 in segments:
            p0, p1, p2, p3 = pts[i0], pts[i1], pts[i2], pts[i3]
            omt = 1.0 - t_vals
            curve = (omt**3)[:, None] * p0 + \
                    (3 * omt**2 * t_vals)[:, None] * p1 + \
                    (3 * omt * t_vals**2)[:, None] * p2 + \
                    (t_vals**3)[:, None] * p3
            for j in range(steps):
                pairs.append(curve[j])
                pairs.append(curve[j + 1])
        return pairs

    def load_path(self, path_value: list, closed: bool, bezier: bool = False):
        self._release_all()
        self._release_sel_markers()

        pts_3d = []
        for p in path_value:
            if len(p) == 2:
                pts_3d.append([p[0], p[1], 0.0])
            else:
                pts_3d.append([p[0], p[1], p[2]])
        pts = np.array(pts_3d, dtype=np.float32)
        self._path_pts = pts

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_bounds(bb_min, bb_max)

        line_color = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        if bezier and len(pts) >= 4:
            pairs = self._tessellate_bezier(pts, closed)
            if pairs:
                line_data = np.empty((len(pairs), 6), dtype=np.float32)
                for i, pt in enumerate(pairs):
                    line_data[i] = np.concatenate([pt, line_color])
                self.upload_lines(line_data)
            handle_color = np.array([0.0, 0.0, 0.0], dtype=np.float32)
            handle_pairs = []
            n = len(pts)
            if closed:
                num_segs = n // 3
                for s in range(num_segs):
                    i0 = (s * 3) % n
                    i1 = (s * 3 + 1) % n
                    i2 = (s * 3 + 2) % n
                    i3 = (s * 3 + 3) % n
                    handle_pairs.append(pts[i0])
                    handle_pairs.append(pts[i1])
                    handle_pairs.append(pts[i2])
                    handle_pairs.append(pts[i3])
            else:
                num_segs = (n - 1) // 3
                for s in range(num_segs):
                    i0 = s * 3
                    handle_pairs.append(pts[i0])
                    handle_pairs.append(pts[i0 + 1])
                    handle_pairs.append(pts[i0 + 2])
                    handle_pairs.append(pts[i0 + 3])
            if handle_pairs:
                hdata = np.empty((len(handle_pairs), 6), dtype=np.float32)
                for i, pt in enumerate(handle_pairs):
                    hdata[i] = np.concatenate([pt, handle_color])
                self.upload_lines(hdata)
        else:
            n = len(pts)
            seg_count = n if closed else n - 1
            line_data = np.empty((seg_count * 2, 6), dtype=np.float32)
            for i in range(seg_count):
                j = (i + 1) % n
                line_data[i * 2] = np.concatenate([pts[i], line_color])
                line_data[i * 2 + 1] = np.concatenate([pts[j], line_color])
            if seg_count > 0:
                self.upload_lines(line_data)

        self._build_point_markers()
        self.update()

    def _octa_faces(self, r):
        px = np.array([r, 0, 0])
        nx = np.array([-r, 0, 0])
        py = np.array([0, r, 0])
        ny = np.array([0, -r, 0])
        pz = np.array([0, 0, r])
        nz = np.array([0, 0, -r])
        if self._is_2d:
            return [
                (py, px, ny), (ny, px, py),
                (py, nx, ny), (ny, nx, py),
            ]
        return [
            (pz, px, py), (pz, py, nx), (pz, nx, ny), (pz, ny, px),
            (nz, py, px), (nz, nx, py), (nz, ny, nx), (nz, px, ny),
        ]

    def _world_per_px_radius(self):
        _, vh = self._viewport
        if vh > 0:
            world_per_px = 2.0 * self.distance * math.tan(math.radians(self.fov / 2)) / vh
        else:
            world_per_px = self.distance * 0.003
        return world_per_px * 3.5

    def _build_point_markers(self):
        for buf in self._point_buffers:
            buf["vao"].release()
            buf["vbo"].release()
        self._point_buffers.clear()

        pts = self._path_pts
        if len(pts) == 0 or not self._gl_ready:
            return

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            for v0, v1, v2 in faces:
                marker_tris.append(np.concatenate([pt + v0, green]))
                marker_tris.append(np.concatenate([pt + v1, green]))
                marker_tris.append(np.concatenate([pt + v2, green]))

        if marker_tris:
            self.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected(self, indices: list[int]):
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.update()
            return

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
        self.update()

    def _release_sel_markers(self):
        for attr in ("_sel_vao_r", "_sel_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_sel_vbo_r", "_sel_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_sel_markers(self):
        self._release_sel_markers()
        if not self._selected_indices or not self._gl_ready:
            return

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        pts = self._path_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    for v0, v1, v2 in faces:
                        tris.append(np.concatenate([pt + v0, color_val]))
                        tris.append(np.concatenate([pt + v1, color_val]))
                        tris.append(np.concatenate([pt + v2, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._line_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def _render_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        vao = self._sel_vao_r if self._blink_red else self._sel_vao_w
        if vao is not None:
            self._line_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            self._ctx.disable(mgl.DEPTH_TEST)
            vao.render(mgl.TRIANGLES)
            self._ctx.enable(mgl.DEPTH_TEST)

    def frame_bounds(self, bb_min, bb_max):
        super().frame_bounds(bb_min, bb_max)
        if len(self._path_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if len(self._path_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
            self.update()

    def closeEvent(self, event):
        self._blink_timer.stop()
        super().closeEvent(event)

    def _pick_vertex(self, px: float, py: float) -> int:
        if len(self._path_pts) == 0:
            return -1
        w, h = self._viewport
        aspect = w / h if h > 0 else 1.0
        mvp = self._mvp(aspect)
        best_idx = -1
        best_dist_sq = float("inf")
        threshold = 12.0
        for i, pt3 in enumerate(self._path_pts):
            clip = mvp @ np.array([pt3[0], pt3[1], pt3[2], 1.0], dtype=np.float32)
            if clip[3] == 0:
                continue
            ndc = clip[:2] / clip[3]
            sx = (ndc[0] * 0.5 + 0.5) * w
            sy = (1.0 - (ndc[1] * 0.5 + 0.5)) * h
            dx, dy = sx - px, sy - py
            d2 = dx * dx + dy * dy
            if d2 < best_dist_sq:
                best_dist_sq = d2
                best_idx = i
        if best_idx >= 0 and best_dist_sq < threshold * threshold:
            return best_idx
        return -1

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None and self._last_mouse is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._last_mouse is None:
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            if vi >= 0:
                pt = self._path_pts[vi]
                coords = f"({pt[0]:g}, {pt[1]:g}" + (f", {pt[2]:g})" if not self._is_2d else ")")
                self.setToolTip(f"[{vi}]: {coords}")
            else:
                self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Grid Viewer
# ---------------------------------------------------------------------------

class GridViewer(QDialog):
    """3D grid viewer for lists of lists of points with quad mesh faces."""

    def __init__(self, title: str, grid_value: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Grid Viewer: {title}")
        self.resize(900, 520)

        self._grid = grid_value
        self._rows = len(grid_value)
        self._cols = len(grid_value[0]) if self._rows > 0 else 0
        all_pts = [p for row in grid_value for p in row]
        self._is_2d = all(len(p) == 2 for p in all_pts)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _GridViewport(grid_value, self._is_2d, self)
        splitter.addWidget(self._vp)

        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)

        row_bar = QHBoxLayout()
        row_bar.addWidget(QLabel("Row:"))
        self._row_combo = QComboBox()
        for i in range(self._rows):
            self._row_combo.addItem(str(i))
        self._row_combo.currentIndexChanged.connect(self._on_row_changed)
        row_bar.addWidget(self._row_combo, 1)
        tc_layout.addLayout(row_bar)

        self._pts_label = QLabel()
        tc_layout.addWidget(self._pts_label)

        self._vert_table = self._make_vert_table(grid_value[0], self._is_2d)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        tc_layout.addWidget(self._vert_table, 1)

        splitter.addWidget(table_container)
        t = self._vert_table
        fm = t.fontMetrics()
        vh_w = max(fm.horizontalAdvance(str(max(self._cols - 1, 0))),
                   fm.horizontalAdvance("0000")) + 20
        table_w = (vh_w
                   + sum(t.columnWidth(j) for j in range(t.columnCount()))
                   + t.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        self._faces_cb = QCheckBox("Faces")
        self._faces_cb.setChecked(True)
        self._faces_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._faces_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._faces_cb)
        self._col_wrap_cb = QCheckBox("Col Wrap")
        self._col_wrap_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._col_wrap_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._col_wrap_cb)
        self._row_wrap_cb = QCheckBox("Row Wrap")
        self._row_wrap_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._row_wrap_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._row_wrap_cb)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._on_row_changed(0)
        self._vp.schedule_load(self._do_initial_load)

    @staticmethod
    def _make_vert_table(row_pts: list, is_2d: bool) -> QTableWidget:
        cols = 2 if is_2d else 3
        t = QTableWidget(len(row_pts), cols)
        t.setFont(QFont("Menlo", 11))
        headers = ["X", "Y"] if is_2d else ["X", "Y", "Z"]
        t.setHorizontalHeaderLabels(headers)
        t.setVerticalHeaderLabels([str(i) for i in range(len(row_pts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setStyleSheet(
            "QTableWidget::item:selected:!active {"
            "  background: palette(highlight);"
            "  color: palette(highlighted-text);"
            "}"
        )
        for i, p in enumerate(row_pts):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(cols):
            t.setColumnWidth(j, min_w)
        return t

    def _populate_table(self, row_pts: list):
        cols = 2 if self._is_2d else 3
        self._vert_table.setRowCount(len(row_pts))
        self._vert_table.setVerticalHeaderLabels(
            [str(i) for i in range(len(row_pts))])
        for i, p in enumerate(row_pts):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._vert_table.setItem(i, j, item)

    def _on_row_changed(self, row_idx: int):
        if row_idx < 0 or row_idx >= self._rows:
            return
        row_pts = self._grid[row_idx]
        self._pts_label.setText(f"Row Points ({len(row_pts)})")
        self._populate_table(row_pts)
        self._vert_table.selectAll()
        self._vp.set_selected_row(row_idx)

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        col_indices = sorted(r.row() for r in rows)
        row_idx = self._row_combo.currentIndex()
        global_indices = [row_idx * self._cols + c for c in col_indices]
        self._vp.set_selected(global_indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        if vi < 0:
            return
        row_idx = vi // self._cols
        col_idx = vi % self._cols
        if row_idx != self._row_combo.currentIndex():
            self._row_combo.setCurrentIndex(row_idx)
        self._vert_table.clearSelection()
        if 0 <= col_idx < self._vert_table.rowCount():
            self._vert_table.selectRow(col_idx)

    def _do_initial_load(self):
        self._vp.load_grid(self._grid,
                           col_wrap=self._col_wrap_cb.isChecked(),
                           row_wrap=self._row_wrap_cb.isChecked(),
                           draw_faces=self._faces_cb.isChecked())

    def _rebuild(self, _=None):
        if self._vp._gl_ready:
            self._vp.load_grid(self._grid,
                               col_wrap=self._col_wrap_cb.isChecked(),
                               row_wrap=self._row_wrap_cb.isChecked(),
                               draw_faces=self._faces_cb.isChecked())


class _GridViewport(_SimpleViewport):
    """Viewport for grid data with quad mesh faces and selectable vertex markers."""
    vertex_clicked = Signal(int)

    def __init__(self, grid_value: list, is_2d: bool, parent=None):
        super().__init__(parent)
        self._depth_test_points = True
        self.setMouseTracking(True)
        self._press_pos = None
        self._drag_started = False
        self._all_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._grid_rows = len(grid_value)
        self._grid_cols = len(grid_value[0]) if self._grid_rows > 0 else 0
        self._is_2d = is_2d
        self._selected_indices: list[int] = []
        self._selected_row: int = -1
        self._blink_red = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(250)
        self._blink_timer.timeout.connect(self._blink_tick)
        self._sel_vao_r = None
        self._sel_vbo_r = None
        self._sel_vao_w = None
        self._sel_vbo_w = None
        self.show_edges = True  # enables polygon offset fill so skeleton lines render in front of faces
        if is_2d:
            self.azimuth = 0.0
            self.elevation = 90.0
            self.orthographic = True
        else:
            self.orthographic = False

    def _blink_tick(self):
        self._blink_red = not self._blink_red
        self.update()

    def load_grid(self, grid_value: list, col_wrap: bool = False,
                  row_wrap: bool = False, draw_faces: bool = True):
        self._release_all()
        self._release_sel_markers()

        pts_3d = []
        for row in grid_value:
            for p in row:
                if len(p) == 2:
                    pts_3d.append([p[0], p[1], 0.0])
                else:
                    pts_3d.append([p[0], p[1], p[2]])
        pts = np.array(pts_3d, dtype=np.float32)
        self._all_pts = pts
        rows = self._grid_rows
        cols = self._grid_cols

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_bounds(bb_min, bb_max)

        r_range = rows if row_wrap else rows - 1
        c_range = cols if col_wrap else cols - 1

        # Row lines (orange) and column lines (blue) — always drawn in both modes
        row_color = np.array([0.85, 0.45, 0.1], dtype=np.float32)
        col_color = np.array([0.15, 0.45, 0.85], dtype=np.float32)
        line_verts = []
        for r in range(rows):
            for c in range(c_range):
                a = r * cols + c
                b = r * cols + (c + 1) % cols
                line_verts.append(np.concatenate([pts[a], row_color]))
                line_verts.append(np.concatenate([pts[b], row_color]))
        for r in range(r_range):
            for c in range(cols):
                a = r * cols + c
                b = ((r + 1) % rows) * cols + c
                line_verts.append(np.concatenate([pts[a], col_color]))
                line_verts.append(np.concatenate([pts[b], col_color]))
        if line_verts:
            self.upload_lines(np.array(line_verts, dtype=np.float32))

        # Quad faces (faces mode only); polygon offset fill (from show_edges=True)
        # ensures the skeleton lines render in front of the mesh faces.
        if draw_faces and r_range >= 1 and c_range >= 1:
            tris_pos = []
            tris_norm = []
            for r in range(r_range):
                for c in range(c_range):
                    i00 = r * cols + c
                    i01 = r * cols + (c + 1) % cols
                    i10 = ((r + 1) % rows) * cols + c
                    i11 = ((r + 1) % rows) * cols + (c + 1) % cols
                    p00, p01, p10, p11 = pts[i00], pts[i01], pts[i10], pts[i11]
                    n1 = np.cross(p01 - p00, p11 - p00)
                    ln1 = np.linalg.norm(n1)
                    if ln1 > 0:
                        n1 /= ln1
                    tris_pos.extend([p00, p01, p11])
                    tris_norm.extend([n1, n1, n1])
                    n2 = np.cross(p11 - p00, p10 - p00)
                    ln2 = np.linalg.norm(n2)
                    if ln2 > 0:
                        n2 /= ln2
                    tris_pos.extend([p00, p11, p10])
                    tris_norm.extend([n2, n2, n2])
            if tris_pos:
                self.upload_mesh(np.array(tris_pos, dtype=np.float32),
                                 np.array(tris_norm, dtype=np.float32),
                                 backface_color=(0.9, 0.85, 0.1, 1.0))

        self._build_point_markers()
        if self._selected_indices:
            self._build_sel_markers()
        self.update()

    def _octa_faces(self, r):
        px = np.array([r, 0, 0])
        nx = np.array([-r, 0, 0])
        py = np.array([0, r, 0])
        ny = np.array([0, -r, 0])
        pz = np.array([0, 0, r])
        nz = np.array([0, 0, -r])
        if self._is_2d:
            return [
                (py, px, ny), (ny, px, py),
                (py, nx, ny), (ny, nx, py),
            ]
        return [
            (pz, px, py), (pz, py, nx), (pz, nx, ny), (pz, ny, px),
            (nz, py, px), (nz, nx, py), (nz, ny, nx), (nz, px, ny),
        ]

    def _world_per_px_radius(self):
        _, vh = self._viewport
        if vh > 0:
            world_per_px = 2.0 * self.distance * math.tan(math.radians(self.fov / 2)) / vh
        else:
            world_per_px = self.distance * 0.003
        return world_per_px * 3.5

    def _build_point_markers(self):
        for buf in self._point_buffers:
            buf["vao"].release()
            buf["vbo"].release()
        self._point_buffers.clear()

        pts = self._all_pts
        if len(pts) == 0 or not self._gl_ready:
            return

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            for v0, v1, v2 in faces:
                marker_tris.append(np.concatenate([pt + v0, green]))
                marker_tris.append(np.concatenate([pt + v1, green]))
                marker_tris.append(np.concatenate([pt + v2, green]))

        if marker_tris:
            self.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected_row(self, row_idx: int):
        self._selected_row = row_idx
        cols = self._grid_cols
        self.set_selected([row_idx * cols + c for c in range(cols)])

    def set_selected(self, indices: list[int]):
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.update()
            return

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
        self.update()

    def _release_sel_markers(self):
        for attr in ("_sel_vao_r", "_sel_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_sel_vbo_r", "_sel_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_sel_markers(self):
        self._release_sel_markers()
        if not self._selected_indices or not self._gl_ready:
            return

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        pts = self._all_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    for v0, v1, v2 in faces:
                        tris.append(np.concatenate([pt + v0, color_val]))
                        tris.append(np.concatenate([pt + v1, color_val]))
                        tris.append(np.concatenate([pt + v2, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._line_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def _render_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        vao = self._sel_vao_r if self._blink_red else self._sel_vao_w
        if vao is not None:
            self._line_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            vao.render(mgl.TRIANGLES)

    def frame_bounds(self, bb_min, bb_max):
        super().frame_bounds(bb_min, bb_max)
        if len(self._all_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if len(self._all_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
            self.update()

    def closeEvent(self, event):
        self._blink_timer.stop()
        super().closeEvent(event)

    def _pick_vertex(self, px: float, py: float) -> int:
        if len(self._all_pts) == 0:
            return -1
        w, h = self._viewport
        aspect = w / h if h > 0 else 1.0
        mvp = self._mvp(aspect)
        best_idx = -1
        best_dist_sq = float("inf")
        threshold = 12.0
        for i, pt3 in enumerate(self._all_pts):
            clip = mvp @ np.array([pt3[0], pt3[1], pt3[2], 1.0], dtype=np.float32)
            if clip[3] == 0:
                continue
            ndc = clip[:2] / clip[3]
            sx = (ndc[0] * 0.5 + 0.5) * w
            sy = (1.0 - (ndc[1] * 0.5 + 0.5)) * h
            dx, dy = sx - px, sy - py
            d2 = dx * dx + dy * dy
            if d2 < best_dist_sq:
                best_dist_sq = d2
                best_idx = i
        if best_idx >= 0 and best_dist_sq < threshold * threshold:
            return best_idx
        return -1

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None and self._last_mouse is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._last_mouse is None:
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            if vi >= 0:
                pt = self._all_pts[vi]
                r, c = vi // self._grid_cols, vi % self._grid_cols
                coords = f"({pt[0]:g}, {pt[1]:g}" + (f", {pt[2]:g})" if not self._is_2d else ")")
                self.setToolTip(f"[{r},{c}]: {coords}")
            else:
                self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Factory helpers (used by debugger context menu)
# ---------------------------------------------------------------------------

def _open_list_viewer(title: str, value, parent=None):
    dlg = ListViewer(title, value, parent)
    dlg.show()


def _open_vnf_viewer(title: str, value, parent=None):
    dlg = VNFViewer(title, value, parent)
    dlg.show()


def _open_path_viewer(title: str, value, parent=None):
    dlg = PathViewer(title, value, parent)
    dlg.show()


def _open_grid_viewer(title: str, value, parent=None):
    dlg = GridViewer(title, value, parent)
    dlg.show()


def build_viewer_menu(menu: QMenu, name: str, value, parent=None):
    """Add viewer actions to a QMenu based on the value's type."""
    if _is_list(value) or _is_oscobject(value):
        menu.addAction("View in List...", lambda: _open_list_viewer(name, value, parent))
    if _is_vnf(value):
        menu.addAction("View as VNF...", lambda: _open_vnf_viewer(name, value, parent))
    if _is_grid(value):
        menu.addAction("View as Grid...", lambda: _open_grid_viewer(name, value, parent))
    if _is_path(value):
        menu.addAction("View as Path...", lambda: _open_path_viewer(name, value, parent))
