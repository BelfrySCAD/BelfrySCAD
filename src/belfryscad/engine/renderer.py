"""
ModernGL mesh renderer: uploads geometry and draws it with a simple Phong shader.
"""
from __future__ import annotations
import math
import numpy as np
from typing import Optional

import moderngl as mgl
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

from belfryscad.engine.evaluator import ColoredBody

_VERT = """
#version 330 core
in vec3 in_position;
in vec3 in_normal;
uniform mat4 mvp;
uniform mat4 model;
out vec3 v_normal;
out vec3 v_world_pos;
void main() {
    vec4 world = model * vec4(in_position, 1.0);
    v_world_pos = world.xyz;
    v_normal = mat3(model) * in_normal;
    gl_Position = mvp * vec4(in_position, 1.0);
}
"""

_FRAG = """
#version 330 core
in vec3 v_normal;
in vec3 v_world_pos;
uniform vec4 object_color;
uniform vec3 light_dir;
uniform vec3 eye_pos;
uniform bool flat_preview;
out vec4 fragColor;
void main() {
    vec3 n = normalize(v_normal);
    if (!gl_FrontFacing) {
        if (!flat_preview) {
            fragColor = vec4(1.0, 0.0, 1.0, 1.0);
            return;
        }
        n = -n;
    }
    vec3 L = normalize(light_dir);
    float diff_key  = max(dot(n, L), 0.0);
    float diff_fill = max(dot(n, normalize(-light_dir * vec3(1.0, 1.0, 0.3))), 0.0);
    vec3 col = object_color.rgb;
    vec3 lit = 0.35 * col
             + 0.50 * diff_key  * col
             + 0.20 * diff_fill * col;

    vec3 V = normalize(eye_pos - v_world_pos);
    vec3 H = normalize(L + V);
    float spec = pow(max(dot(n, H), 0.0), 64.0) * 0.5;
    lit += vec3(spec);

    fragColor = vec4(lit, object_color.a);
}
"""

_GIZMO_VERT = """
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

_GIZMO_FRAG = """
#version 330 core
in vec3 v_color;
out vec4 fragColor;
void main() {
    fragColor = vec4(v_color, 1.0);
}
"""

_EDGE_VERT = """
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


class Camera:
    """Spherical-coordinate orbit camera."""

    def __init__(self):
        self.azimuth = 295.0
        self.elevation = 35.0
        self.distance = 50.0
        self.target = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        self.fov = 45.0
        self.orthographic = False
        self.stereo = False
        self.viewer_ipd = 65.0           # mm — interpupillary distance
        self.viewer_screen_dist = 600.0  # mm — eye-to-screen distance
        self.stereo_depth_scale = 0.75   # comfort trim (1.0 = geometrically correct)
        self.screen_dpi = 96.0           # physical DPI, updated from QScreen at pref-apply

    def view_matrix(self) -> np.ndarray:
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        eye = self.target + self.distance * np.array([
            math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el),
        ], dtype=np.float32)
        return _look_at(eye, self.target, np.array([0, 0, 1], dtype=np.float32))

    def projection_matrix(self, aspect: float) -> np.ndarray:
        if self.orthographic:
            half_h = self.distance * math.tan(math.radians(self.fov / 2))
            return _ortho(half_h * aspect, half_h, -10000.0, 10000.0)
        return _perspective(math.radians(self.fov), aspect, 0.1, 10000.0)

    def eye_position(self) -> np.ndarray:
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        return self.target + self.distance * np.array([
            math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el),
        ], dtype=np.float32)

    def stereo_view_matrices(
        self, half_vp_w: int, vp_h: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """(left_panel_view, right_panel_view) for cross-eye stereo.

        Cross-eye: left panel = right eye, right panel = left eye.
        Both cameras toe-in toward the same target point.

        Eye separation is computed from physical viewer measurements:
          stereo_fraction = (IPD / screen_dist)
                          × (physical_half_fov_h / rendered_half_fov_h)
                          × depth_scale
        This gives ~3–5 % of camera distance for typical desktop setups.
        """
        if half_vp_w <= 0 or vp_h <= 0:
            v = self.view_matrix()
            return v, v
        rendered_half_fov_h = math.atan(
            math.tan(math.radians(self.fov / 2)) * half_vp_w / vp_h
        )
        physical_half_fov_h = math.atan(
            (half_vp_w * 25.4 / self.screen_dpi) / (2.0 * self.viewer_screen_dist)
        )
        stereo_eye_sep = (
            (self.viewer_ipd / self.viewer_screen_dist)
            * (physical_half_fov_h / rendered_half_fov_h)
            * self.stereo_depth_scale
        )
        view = self.view_matrix()
        right_vec = view[0, :3].astype(np.float32)
        half = self.distance * stereo_eye_sep * 0.5
        eye = self.eye_position()
        up = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        right_eye = _look_at(eye + right_vec * half, self.target, up)
        left_eye  = _look_at(eye - right_vec * half, self.target, up)
        return right_eye, left_eye  # left panel = right eye (cross-eye)

    def frame_bounds(self, bb_min: np.ndarray, bb_max: np.ndarray):
        center = (bb_min + bb_max) / 2
        extent = np.linalg.norm(bb_max - bb_min)
        self.target = center.astype(np.float32)
        self.distance = max(extent * 1.2, 1.0)


class MeshBuffer:
    def __init__(self, ctx: mgl.Context, vbo: mgl.Buffer, ibo: Optional[mgl.Buffer],
                 vao: mgl.VertexArray, num_indices: int, color: tuple,
                 cpu_v0: np.ndarray, cpu_v1: np.ndarray, cpu_v2: np.ndarray,
                 tri_ids: np.ndarray,
                 edge_vbo: Optional[mgl.Buffer] = None,
                 edge_vao: Optional[mgl.VertexArray] = None,
                 flat_preview: bool = False):
        self.ctx = ctx
        self.vbo = vbo
        self.ibo = ibo
        self.vao = vao
        self.num_indices = num_indices
        self.color = color
        self.cpu_v0 = cpu_v0
        self.cpu_v1 = cpu_v1
        self.cpu_v2 = cpu_v2
        self.tri_ids = tri_ids
        self.original_ids: set[int] = set(int(x) for x in tri_ids)
        self.edge_vbo = edge_vbo
        self.edge_vao = edge_vao
        self.flat_preview = flat_preview


class SceneRenderer:
    def __init__(self):
        self._ctx: Optional[mgl.Context] = None
        self._prog: Optional[mgl.Program] = None
        self._gizmo_prog: Optional[mgl.Program] = None
        self._edge_prog: Optional[mgl.Program] = None
        self._label_prog: Optional[mgl.Program] = None
        self._label_quad_vbo: Optional[mgl.Buffer] = None
        self._label_quad_vao: Optional[mgl.VertexArray] = None
        self._label_tex_cache: dict[str, tuple[mgl.Texture, int, int]] = {}
        self._label_texture_scale = 4
        self._gizmo_vbo: Optional[mgl.Buffer] = None
        self._gizmo_vao: Optional[mgl.VertexArray] = None
        self._buffers: list[MeshBuffer] = []
        self.camera = Camera()
        self._viewport: tuple[int, int] = (800, 600)
        self._default_color = (0.9, 0.85, 0.1, 1.0)
        self.selected_id: Optional[int] = None
        self.show_gizmo: bool = False
        self.active_gizmo_axis: int = -1   # -1=none, 0=X, 1=Y, 2=Z
        self.drag_offset: np.ndarray = np.zeros(3, dtype=np.float32)
        self.gizmo_type: int = 0           # 0=translate, 1=rotate
        self.drag_rotation_axis: int = -1
        self.drag_rotation_angle: float = 0.0
        self.drag_scale_axis: int = -1
        self.drag_scale_factor: float = 1.0
        self.drag_scale_uniform: bool = False
        self.show_axes: bool = True
        self.show_scale_markers: bool = True
        self.show_edges: bool = False
        self.show_crosshairs: bool = False
        self.light_az_offset: float = 0.0
        self.light_el_offset: float = 0.0
        self._axes_vbo: Optional[mgl.Buffer] = None
        self._axes_vao: Optional[mgl.VertexArray] = None

    def initialize(self, ctx: mgl.Context):
        # Old GL context (if any) is already destroyed by Qt — just drop stale refs.
        self._label_tex_cache.clear()
        self._buffers.clear()
        self._ctx = ctx
        self._prog = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
        self._gizmo_prog = ctx.program(vertex_shader=_GIZMO_VERT, fragment_shader=_GIZMO_FRAG)
        self._edge_prog = ctx.program(vertex_shader=_EDGE_VERT, fragment_shader=_GIZMO_FRAG)
        self._label_prog = ctx.program(vertex_shader=_LABEL_VERT, fragment_shader=_LABEL_FRAG)

        # Unit quad (-1..1) for label billboards, fanned around vertex 0.
        quad = np.array([
            [-1.0, -1.0, 0.0, 1.0],
            [ 1.0, -1.0, 1.0, 1.0],
            [ 1.0,  1.0, 1.0, 0.0],
            [-1.0,  1.0, 0.0, 0.0],
        ], dtype=np.float32)
        self._label_quad_vbo = ctx.buffer(quad.tobytes())
        self._label_quad_vao = ctx.vertex_array(
            self._label_prog,
            [(self._label_quad_vbo, "2f 2f", "in_position", "in_uv")],
        )

    def set_viewport(self, w: int, h: int):
        self._viewport = (w, h)

    def load_geometry(self, bodies: list[ColoredBody]):
        self._clear_buffers()
        self.selected_id = None
        self.drag_offset = np.zeros(3, dtype=np.float32)
        self.drag_rotation_angle = 0.0
        self.drag_rotation_axis = -1
        self.drag_scale_axis = -1
        self.drag_scale_factor = 1.0
        self.drag_scale_uniform = False
        if not bodies or self._ctx is None:
            return

        for cb in bodies:
            if cb.body.is_empty():
                continue
            buf = self._upload_body(cb)
            if buf:
                self._buffers.append(buf)

    def _clear_buffers(self):
        for buf in self._buffers:
            buf.vao.release()
            buf.vbo.release()
            if buf.ibo is not None:
                buf.ibo.release()
            if buf.edge_vao is not None:
                buf.edge_vao.release()
            if buf.edge_vbo is not None:
                buf.edge_vbo.release()
        self._buffers.clear()

    def _upload_body(self, cb: ColoredBody) -> Optional[MeshBuffer]:
        mesh = cb.body.to_mesh()
        verts = np.array(mesh.vert_properties, dtype=np.float32)[:, :3]
        tris = np.array(mesh.tri_verts, dtype=np.int32)

        if len(verts) == 0 or len(tris) == 0:
            return None

        T = len(tris)

        run_ids = np.array(mesh.run_original_id, dtype=np.int32)
        run_idx = np.array(mesh.run_index, dtype=np.int32)
        tri_ids = np.zeros(T, dtype=np.int32)
        for i in range(len(run_idx) - 1):
            s, e = int(run_idx[i]), min(int(run_idx[i + 1]), T)
            if s < T:
                tri_ids[s:e] = run_ids[i]

        v0 = verts[tris[:, 0]]
        v1 = verts[tris[:, 1]]
        v2 = verts[tris[:, 2]]

        face_normals = np.cross(v1 - v0, v2 - v0)
        lengths = np.linalg.norm(face_normals, axis=1, keepdims=True)
        lengths = np.where(lengths == 0, 1, lengths)
        face_normals /= lengths

        normals_per_corner = np.repeat(face_normals, 3, axis=0)
        positions_per_corner = np.concatenate([v0, v1, v2], axis=1).reshape(-1, 3)

        interleaved = np.concatenate(
            [positions_per_corner, normals_per_corner], axis=1
        ).astype(np.float32)

        vbo = self._ctx.buffer(interleaved.tobytes())
        vao = self._ctx.vertex_array(
            self._prog,
            [(vbo, "3f 3f", "in_position", "in_normal")],
        )

        # Build edge line geometry: all 3 edges per triangle (full triangulation wireframe)
        T = len(tris)
        ec = np.array([0.15, 0.15, 0.15], dtype=np.float32)
        starts = np.concatenate([v0, v1, v2], axis=0)   # (3T, 3)
        ends   = np.concatenate([v1, v2, v0], axis=0)   # (3T, 3)
        cols   = np.tile(ec, (3 * T, 1))                 # (3T, 3)
        edge_rows = np.empty((6 * T, 6), dtype=np.float32)
        edge_rows[0::2] = np.concatenate([starts, cols], axis=1)
        edge_rows[1::2] = np.concatenate([ends,   cols], axis=1)
        edge_vbo = self._ctx.buffer(edge_rows.tobytes())
        edge_vao = self._ctx.vertex_array(
            self._edge_prog,
            [(edge_vbo, "3f 3f", "in_position", "in_color")],
        )

        color = cb.color if cb.color is not None else self._default_color
        return MeshBuffer(self._ctx, vbo, None, vao, len(interleaved), color,
                          cpu_v0=v0.copy(), cpu_v1=v1.copy(), cpu_v2=v2.copy(),
                          tri_ids=tri_ids, edge_vbo=edge_vbo, edge_vao=edge_vao,
                          flat_preview=cb.flat_preview)

    def paint(self, bg_color: tuple = (0.82, 0.82, 0.82, 1.0), qt_fbo_id: int = 0):
        if self._ctx is None or self._prog is None:
            return

        fbo = self._ctx.detect_framebuffer(qt_fbo_id)
        fbo.use()

        self._ctx.clear(*bg_color[:3])
        self._ctx.enable(mgl.DEPTH_TEST)
        self._ctx.enable_direct(0x809D)  # GL_MULTISAMPLE

        center_view = self.camera.view_matrix()
        L_view = np.array([0.6, 0.8, 1.0], dtype=np.float64)
        if self.light_az_offset != 0.0 or self.light_el_offset != 0.0:
            a = math.radians(self.light_az_offset)
            e = math.radians(self.light_el_offset)
            ca, sa = math.cos(a), math.sin(a)
            ce, se = math.cos(e), math.sin(e)
            Ry = np.array([[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]], dtype=np.float64)
            Rx = np.array([[1, 0, 0], [0, ce, -se], [0, se, ce]], dtype=np.float64)
            L_view = Rx @ Ry @ L_view
        L_world = (center_view[:3, :3].T @ L_view).astype(np.float32)
        L_world /= np.linalg.norm(L_world)

        w, h = self._viewport
        if self.camera.stereo:
            # Qt sets the GL viewport to device pixels before paintGL, which may
            # differ from self._viewport (logical pixels) on HiDPI displays.
            # Read the actual device-pixel viewport from the GL context so our
            # sub-viewport splits land in the right place.
            gl_vp = self._ctx.viewport  # (x, y, gl_w, gl_h) in device pixels
            gl_w, gl_h = gl_vp[2], gl_vp[3]
            half_gl_w = gl_w // 2

            aspect = half_gl_w / gl_h if gl_h > 0 else 1.0
            proj = self.camera.projection_matrix(aspect)
            left_view, right_view = self.camera.stereo_view_matrices(half_gl_w, gl_h)

            # Use logical half-width for scale-dependent calculations (labels, ticks)
            self._viewport = (w // 2, h)

            self._ctx.viewport = (0, 0, half_gl_w, gl_h)
            self._paint_scene(left_view, proj, L_world)

            self._ctx.viewport = (half_gl_w, 0, half_gl_w, gl_h)
            self._paint_scene(right_view, proj, L_world)

            self._viewport = (w, h)
            self._ctx.viewport = (0, 0, gl_w, gl_h)
        else:
            aspect = w / h if h > 0 else 1.0
            proj = self.camera.projection_matrix(aspect)
            self._paint_scene(center_view, proj, L_world)

    def _paint_scene(self, view: np.ndarray, proj: np.ndarray, L_world: np.ndarray):
        """Render one eye's worth of scene: geometry, edges, axes, labels, gizmo."""
        mvp = proj @ view
        model = np.eye(4, dtype=np.float32)
        eye_pos = -(view[:3, :3].T @ view[:3, 3]).astype(np.float32)

        self._prog["light_dir"].value = tuple(L_world)
        self._prog["eye_pos"].value = tuple(eye_pos)

        has_drag = np.any(self.drag_offset != 0)
        has_rotation = self.drag_rotation_axis >= 0 and self.drag_rotation_angle != 0.0
        has_scale = self.drag_scale_axis >= 0 and self.drag_scale_factor != 1.0

        rot_center = None
        if has_rotation:
            bbox = self._selected_buffer_bbox()
            if bbox is not None:
                rot_center, _ = bbox

        scale_center = None
        if has_scale:
            bbox = self._selected_buffer_bbox()
            if bbox is not None:
                scale_center, _ = bbox

        # When showing edges, push solid surfaces slightly away from camera so that
        # coplanar edge lines at true depth pass the depth test without z-fighting.
        if self.show_edges:
            self._ctx.polygon_offset = (2.0, 2.0)
            self._ctx.enable_direct(0x8037)  # GL_POLYGON_OFFSET_FILL

        buf_models: list[np.ndarray] = []
        for buf in self._buffers:
            is_selected = self.selected_id is not None and self.selected_id in buf.original_ids
            color = _highlight_color(buf.color) if is_selected else buf.color

            if is_selected and has_drag:
                buf_model = np.eye(4, dtype=np.float32)
                buf_model[:3, 3] = self.drag_offset
            elif is_selected and has_rotation and rot_center is not None:
                buf_model = _rotation_model(rot_center, self.drag_rotation_axis, self.drag_rotation_angle)
            elif is_selected and has_scale and scale_center is not None:
                buf_model = _scale_model(scale_center, self.drag_scale_axis,
                                         self.drag_scale_factor, self.drag_scale_uniform)
            else:
                buf_model = model

            self._prog["model"].write(buf_model.T.tobytes())
            self._prog["mvp"].write((proj @ view @ buf_model).T.astype(np.float32).tobytes())
            self._prog["object_color"].value = color
            self._prog["flat_preview"].value = buf.flat_preview
            buf.vao.render()
            buf_models.append(buf_model)

        if self.show_edges:
            self._ctx.disable_direct(0x8037)  # GL_POLYGON_OFFSET_FILL
            if self._edge_prog is not None:
                for buf, buf_model in zip(self._buffers, buf_models):
                    if buf.edge_vao is not None:
                        self._edge_prog["mvp"].write(
                            (proj @ view @ buf_model).T.astype(np.float32).tobytes()
                        )
                        buf.edge_vao.render(mgl.LINES)

        if self.show_axes and self._gizmo_prog is not None:
            self._render_axes(mvp)

        if self.show_axes and self.show_scale_markers and self._label_prog is not None:
            self._render_axis_labels(mvp)

        if self.show_crosshairs and self._gizmo_prog is not None:
            self._render_crosshairs(mvp)

        # Draw gizmo on top (no depth test so it's always visible)
        if self.show_gizmo and self.selected_id is not None and self._gizmo_prog is not None:
            self._render_gizmo(mvp)

    def _render_gizmo(self, mvp: np.ndarray):
        bbox = self._selected_buffer_bbox()
        if bbox is None:
            return
        center, _ = bbox
        scale = self.camera.distance * 0.14

        if self.gizmo_type == 0:
            if np.any(self.drag_offset != 0):
                center = center + self.drag_offset
            geo = _build_gizmo_geo(center, scale, self.active_gizmo_axis)
        elif self.gizmo_type == 1:
            geo = _build_rotate_gizmo_geo(center, scale, self.active_gizmo_axis)
        else:
            geo = _build_scale_gizmo_geo(center, scale, self.active_gizmo_axis)

        if self._gizmo_vbo is not None:
            self._gizmo_vao.release()
            self._gizmo_vbo.release()

        self._gizmo_vbo = self._ctx.buffer(geo.tobytes())
        self._gizmo_vao = self._ctx.vertex_array(
            self._gizmo_prog,
            [(self._gizmo_vbo, "3f 3f", "in_position", "in_color")],
        )

        self._ctx.disable(mgl.DEPTH_TEST)
        self._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        self._gizmo_vao.render()
        self._ctx.enable(mgl.DEPTH_TEST)

    def _render_axes(self, mvp: np.ndarray):
        L       = self.camera.distance * 2.5
        label_spacing, major_spacing, minor_spacing = _nice_spacings(L)
        red   = np.array([0.85, 0.15, 0.15], dtype=np.float32)
        green   = np.array([0.15, 0.65, 0.15], dtype=np.float32)
        blue   = np.array([0.25, 0.35, 0.9], dtype=np.float32)
        gray    = np.array([0.2, 0.2, 0.2], dtype=np.float32)
        axis_colors = [red, green, blue];

        # Tick sizes: minor ~24 px, major exactly 2×; ticks extend in the
        # positive perpendicular direction only.
        w, h = self._viewport
        px_to_world = (self.camera.distance
                       * math.tan(math.radians(self.camera.fov / 2))
                       / max(h, 1))
        minor_len = px_to_world * 24
        tick_len  = minor_len * 2

        rows: list[np.ndarray] = []

        # Positive solid axes
        for i in range(3):
            p0 = np.zeros(3, dtype=np.float32)
            p1 = np.zeros(3, dtype=np.float32)
            p1[i] = float(L)
            rows.append(np.concatenate([p0, axis_colors[i]]))
            rows.append(np.concatenate([p1, axis_colors[i]]))

        # Negative axes — solid, light gray
        for i in range(3):
            p0 = np.zeros(3, dtype=np.float32)
            p1 = np.zeros(3, dtype=np.float32)
            p1[i] = -float(L)
            rows.append(np.concatenate([p0, gray]))
            rows.append(np.concatenate([p1, gray]))

        # Suppress minor ticks for axes nearly end-on to the camera (same
        # threshold used by _axis_tick_world_points to suppress labels).
        eye = self.camera.eye_position().astype(np.float64)
        view_dir = eye - np.asarray(self.camera.target, dtype=np.float64)
        view_norm = np.linalg.norm(view_dir)
        end_on_axis = [False, False, False]
        if view_norm > 1e-9:
            view_dir /= view_norm
            for ai in range(3):
                if abs(view_dir[ai]) > math.cos(math.radians(5.0)):
                    end_on_axis[ai] = True

        # Tick marks:
        #   X-axis → perpendicular in Y
        #   Y-axis → perpendicular in X
        #   Z-axis → perpendicular in Y
        perp_axis = [1, 0, 1]   # which axis the tick extends along
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
            self._gizmo_prog,
            [(self._axes_vbo, "3f 3f", "in_position", "in_color")],
        )

        self._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        self._ctx.enable(mgl.BLEND)
        # GL_LINE_SMOOTH is intentionally left off here: its coverage-based
        # antialiasing halves the alpha of axis-aligned lines (which fall
        # exactly between pixel columns/rows), washing out the axis colors.
        self._axes_vao.render(mgl.LINES)
        self._ctx.disable(mgl.BLEND)

    def _render_crosshairs(self, mvp: np.ndarray):
        half = self.camera.distance * 2.5 / 12.0
        s = 1.0 / math.sqrt(3.0)
        dirs = [
            np.array([ s,  s,  s], dtype=np.float32),
            np.array([-s,  s,  s], dtype=np.float32),
            np.array([-s, -s,  s], dtype=np.float32),
            np.array([ s, -s,  s], dtype=np.float32),
        ]
        c = self.camera.target
        white = np.array([1.0, 1.0, 1.0], dtype=np.float32)
        rows = []
        for d in dirs:
            p0 = c - d * half
            p1 = c + d * half
            rows.append(np.concatenate([p0, white]))
            rows.append(np.concatenate([p1, white]))

        geo = np.array(rows, dtype=np.float32)
        vbo = self._ctx.buffer(geo.tobytes())
        vao = self._ctx.vertex_array(
            self._gizmo_prog,
            [(vbo, "3f 3f", "in_position", "in_color")],
        )
        self._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        self._ctx.enable(mgl.BLEND)
        self._ctx.enable_direct(0x0B20)  # GL_LINE_SMOOTH
        vao.render(mgl.LINES)
        self._ctx.disable_direct(0x0B20)
        self._ctx.disable(mgl.BLEND)
        vao.release()
        vbo.release()

    def _axis_tick_world_points(self) -> list[tuple[np.ndarray, str, int]]:
        """Return (world_position, text, axis_index) for every tick that should be labeled."""
        L = self.camera.distance * 2.5
        spacing, _, _ = _nice_spacings(L)
        eye = self.camera.eye_position().astype(np.float64)

        view_dir = eye - np.asarray(self.camera.target, dtype=np.float64)
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

    def _get_label_texture(self, text: str) -> tuple[mgl.Texture, int, int]:
        """Return (texture, pixel_width, pixel_height) for a tick-label string, cached."""
        cached = self._label_tex_cache.get(text)
        if cached is not None:
            return cached

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
        w, h = self._viewport
        px_to_world = (self.camera.distance
                       * math.tan(math.radians(self.camera.fov / 2))
                       / max(h, 1))

        view = self.camera.view_matrix()
        right = view[0, :3].astype(np.float64)
        up = view[1, :3].astype(np.float64)
        label_scale = 3.0
        gap = 6 * px_to_world * label_scale

        # Labels sit on the negative perpendicular side (opposite the ticks).
        perp_axis = [1, 0, 1]  # must match _render_axes

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
            self._label_prog["half_size"].write(np.array([half_w, half_h], dtype=np.float32).tobytes())
            self._label_quad_vao.render(mgl.TRIANGLE_FAN)

        self._ctx.disable(mgl.BLEND)

    def _selected_buffer_bbox(self) -> Optional[tuple[np.ndarray, float]]:
        if self.selected_id is None:
            return None
        for buf in self._buffers:
            if self.selected_id in buf.original_ids:
                all_v = np.vstack([buf.cpu_v0, buf.cpu_v1, buf.cpu_v2])
                bb_min = all_v.min(axis=0)
                bb_max = all_v.max(axis=0)
                center = ((bb_min + bb_max) / 2).astype(np.float32)
                extent = float(np.linalg.norm(bb_max - bb_min))
                return center, extent
        return None

    def pick_gizmo_axis(self, px: float, py: float, w: int, h: int) -> int:
        """Return which gizmo axis (0=X,1=Y,2=Z) is under the pixel, or -1."""
        if self.gizmo_type == 0:
            return self._pick_translate_axis(px, py, w, h)
        elif self.gizmo_type == 1:
            return self._pick_rotate_axis(px, py, w, h)
        else:
            return self._pick_translate_axis(px, py, w, h)  # scale uses same arrow shape

    def _pick_translate_axis(self, px: float, py: float, w: int, h: int) -> int:
        bbox = self._selected_buffer_bbox()
        if bbox is None:
            return -1
        center, _ = bbox
        if np.any(self.drag_offset != 0):
            center = center + self.drag_offset
        scale = self.camera.distance * 0.14

        aspect = w / h if h > 0 else 1.0
        view = self.camera.view_matrix()
        proj = self.camera.projection_matrix(aspect)
        vp = proj @ view

        def to_screen(p: np.ndarray):
            p4 = np.array([p[0], p[1], p[2], 1.0], dtype=np.float64)
            clip = vp @ p4
            if abs(clip[3]) < 1e-8:
                return None
            ndc = clip[:3] / clip[3]
            return np.array([(ndc[0] + 1) * 0.5 * w, (1 - ndc[1]) * 0.5 * h])

        mouse = np.array([px, py])
        sc = to_screen(center)
        if sc is None:
            return -1

        best_dist, best_axis = 12.0, -1
        for ai, ad in enumerate([np.array([scale, 0, 0]),
                                  np.array([0, scale, 0]),
                                  np.array([0, 0, scale])]):
            st = to_screen(center + ad)
            if st is None:
                continue
            seg = st - sc
            seg_len_sq = float(np.dot(seg, seg))
            if seg_len_sq < 1e-6:
                continue
            t = float(np.clip(np.dot(mouse - sc, seg) / seg_len_sq, 0.0, 1.0))
            dist = float(np.linalg.norm(mouse - (sc + t * seg)))
            if dist < best_dist:
                best_dist, best_axis = dist, ai
        return best_axis

    def _pick_rotate_axis(self, px: float, py: float, w: int, h: int) -> int:
        bbox = self._selected_buffer_bbox()
        if bbox is None:
            return -1
        center, _ = bbox
        scale = self.camera.distance * 0.14

        aspect = w / h if h > 0 else 1.0
        view = self.camera.view_matrix()
        proj = self.camera.projection_matrix(aspect)
        vp = proj @ view

        def to_screen(p: np.ndarray):
            p4 = np.array([p[0], p[1], p[2], 1.0], dtype=np.float64)
            clip = vp @ p4
            if abs(clip[3]) < 1e-8:
                return None
            ndc = clip[:3] / clip[3]
            return np.array([(ndc[0] + 1) * 0.5 * w, (1 - ndc[1]) * 0.5 * h])

        mouse = np.array([px, py])
        n_sample = 32
        angles = np.linspace(0, 2 * math.pi, n_sample, endpoint=False)
        best_dist, best_axis = 12.0, -1

        for ai in range(3):
            p1 = _AXES_PERP1[ai].astype(np.float64)
            p2 = _AXES_PERP2[ai].astype(np.float64)
            pts = center + scale * (np.cos(angles)[:, None] * p1 + np.sin(angles)[:, None] * p2)
            screen = [to_screen(p) for p in pts]
            for i in range(n_sample):
                j = (i + 1) % n_sample
                s0, s1 = screen[i], screen[j]
                if s0 is None or s1 is None:
                    continue
                seg = s1 - s0
                seg_sq = float(np.dot(seg, seg))
                if seg_sq < 1e-6:
                    dist = float(np.linalg.norm(mouse - s0))
                else:
                    t = float(np.clip(np.dot(mouse - s0, seg) / seg_sq, 0.0, 1.0))
                    dist = float(np.linalg.norm(mouse - (s0 + t * seg)))
                if dist < best_dist:
                    best_dist, best_axis = dist, ai
        return best_axis

    def camera_ray(self, px: float, py: float, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
        aspect = w / h if h > 0 else 1.0
        proj = self.camera.projection_matrix(aspect)
        view = self.camera.view_matrix()
        inv_vp = np.linalg.inv((proj @ view).astype(np.float64))

        ndc_x = (2.0 * px / w) - 1.0
        ndc_y = 1.0 - (2.0 * py / h)

        near_h = inv_vp @ np.array([ndc_x, ndc_y, -1.0, 1.0])
        far_h  = inv_vp @ np.array([ndc_x, ndc_y,  1.0, 1.0])
        near_h /= near_h[3]
        far_h  /= far_h[3]

        origin = near_h[:3].astype(np.float32)
        direction = (far_h[:3] - near_h[:3]).astype(np.float32)
        norm = np.linalg.norm(direction)
        if norm > 0:
            direction /= norm
        return origin, direction

    def ray_cast(self, ray_origin: np.ndarray, ray_dir: np.ndarray) -> Optional[int]:
        best_t = np.inf
        best_id = None
        for buf in self._buffers:
            if len(buf.tri_ids) == 0:
                continue
            idx, t = _moller_trumbore_batch(ray_origin, ray_dir,
                                            buf.cpu_v0, buf.cpu_v1, buf.cpu_v2)
            if idx is not None and t < best_t:
                best_t = t
                best_id = int(buf.tri_ids[idx])
        return best_id

    def release(self):
        self._clear_buffers()
        if self._gizmo_vbo is not None:
            self._gizmo_vao.release()
            self._gizmo_vbo.release()
            self._gizmo_vao = None
            self._gizmo_vbo = None
        if self._axes_vbo is not None:
            self._axes_vao.release()
            self._axes_vbo.release()
            self._axes_vao = None
            self._axes_vbo = None
        if self._label_quad_vbo is not None:
            self._label_quad_vao.release()
            self._label_quad_vbo.release()
            self._label_quad_vao = None
            self._label_quad_vbo = None
        for tex, _, _ in self._label_tex_cache.values():
            tex.release()
        self._label_tex_cache.clear()


# ------------------------------------------------------------------
# Gizmo geometry
# ------------------------------------------------------------------

_AXES_DIRS  = np.array([[1,0,0],[0,1,0],[0,0,1]], dtype=np.float32)
_AXES_PERP1 = np.array([[0,1,0],[1,0,0],[1,0,0]], dtype=np.float32)
_AXES_PERP2 = np.array([[0,0,1],[0,0,1],[0,1,0]], dtype=np.float32)
_AXES_COLORS = np.array([[1.0,0.18,0.18],[0.18,1.0,0.18],[0.18,0.18,1.0]], dtype=np.float32)
_HIGHLIGHT   = np.array([1.0, 1.0, 0.2], dtype=np.float32)


def _build_gizmo_geo(center: np.ndarray, length: float, active_axis: int) -> np.ndarray:
    """Return interleaved (pos3, color3) vertex data for 3 axis arrows (GL_TRIANGLES)."""
    n_seg = 8
    shaft_r  = length * 0.04
    cone_r   = length * 0.09
    shaft_t  = 0.72

    rows: list[np.ndarray] = []

    for ai in range(3):
        d  = _AXES_DIRS[ai]
        p1 = _AXES_PERP1[ai]
        p2 = _AXES_PERP2[ai]
        col = _HIGHLIGHT if ai == active_axis else _AXES_COLORS[ai]

        shaft_end = center + d * (length * shaft_t)
        cone_tip  = center + d * length

        angles  = np.linspace(0, 2 * math.pi, n_seg, endpoint=False, dtype=np.float32)
        offsets = np.cos(angles)[:, None] * p1 + np.sin(angles)[:, None] * p2  # (n,3)

        ring0 = center    + shaft_r * offsets   # (n,3)
        ring1 = shaft_end + shaft_r * offsets   # (n,3)
        cring = shaft_end + cone_r  * offsets   # (n,3)

        for i in range(n_seg):
            j = (i + 1) % n_seg

            # Shaft quad (2 triangles)
            for tri in ((ring0[i], ring0[j], ring1[i]),
                        (ring0[j], ring1[j], ring1[i])):
                for v in tri:
                    rows.append(np.concatenate([v, col]))

            # Cone side
            for v in (cring[i], cring[j], cone_tip):
                rows.append(np.concatenate([v, col]))

            # Cone base cap (back-facing to close the tip)
            for v in (shaft_end, cring[j], cring[i]):
                rows.append(np.concatenate([v, col]))

    return np.array(rows, dtype=np.float32)   # (N, 6)


def _build_rotate_gizmo_geo(center: np.ndarray, radius: float, active_axis: int) -> np.ndarray:
    """Return interleaved (pos3, color3) vertex data for 3 axis rings (GL_TRIANGLES)."""
    n_seg = 64
    half_w = radius * 0.055

    rows: list[np.ndarray] = []

    for ai in range(3):
        p1 = _AXES_PERP1[ai].astype(np.float32)
        p2 = _AXES_PERP2[ai].astype(np.float32)
        col = _HIGHLIGHT if ai == active_axis else _AXES_COLORS[ai]

        angles = np.linspace(0, 2 * math.pi, n_seg, endpoint=False, dtype=np.float32)
        dirs = np.cos(angles)[:, None] * p1 + np.sin(angles)[:, None] * p2  # (n,3)

        inner = center + (radius - half_w) * dirs
        outer = center + (radius + half_w) * dirs

        for i in range(n_seg):
            j = (i + 1) % n_seg
            for v in (inner[i], outer[i], outer[j]):
                rows.append(np.concatenate([v, col]))
            for v in (inner[i], outer[j], inner[j]):
                rows.append(np.concatenate([v, col]))

    return np.array(rows, dtype=np.float32)


def _build_scale_gizmo_geo(center: np.ndarray, length: float, active_axis: int) -> np.ndarray:
    """Return interleaved (pos3, color3) vertex data for 3 axis scale handles (GL_TRIANGLES).
    Like translate arrows but with a cube tip instead of a cone."""
    n_seg = 8
    shaft_r = length * 0.04
    box_h   = length * 0.10   # half-size of cube tip
    shaft_t = 0.72

    rows: list[np.ndarray] = []

    for ai in range(3):
        d  = _AXES_DIRS[ai]
        p1 = _AXES_PERP1[ai]
        p2 = _AXES_PERP2[ai]
        col = _HIGHLIGHT if ai == active_axis else _AXES_COLORS[ai]

        shaft_end = center + d * (length * shaft_t)
        box_ctr   = center + d * length

        # Shaft cylinder
        angles  = np.linspace(0, 2 * math.pi, n_seg, endpoint=False, dtype=np.float32)
        offsets = np.cos(angles)[:, None] * p1 + np.sin(angles)[:, None] * p2
        ring0 = center    + shaft_r * offsets
        ring1 = shaft_end + shaft_r * offsets

        for i in range(n_seg):
            j = (i + 1) % n_seg
            for tri in ((ring0[i], ring0[j], ring1[i]),
                        (ring0[j], ring1[j], ring1[i])):
                for v in tri:
                    rows.append(np.concatenate([v, col]))

        # Cube tip — 6 faces as quads
        bc   = box_ctr.astype(np.float32)
        bh_d = box_h * d.astype(np.float32)
        bh_1 = box_h * p1.astype(np.float32)
        bh_2 = box_h * p2.astype(np.float32)

        def quad(offset, a1, a2):
            fc = bc + offset
            v = [fc - a1 - a2, fc + a1 - a2, fc + a1 + a2, fc - a1 + a2]
            for tri_v in ((v[0], v[1], v[2]), (v[0], v[2], v[3])):
                for vv in tri_v:
                    rows.append(np.concatenate([vv, col]))

        quad(+bh_d, bh_1, bh_2)
        quad(-bh_d, bh_1, bh_2)
        quad(+bh_1, bh_d, bh_2)
        quad(-bh_1, bh_d, bh_2)
        quad(+bh_2, bh_d, bh_1)
        quad(-bh_2, bh_d, bh_1)

    return np.array(rows, dtype=np.float32)


def _scale_model(center: np.ndarray, axis_idx: int, factor: float, uniform: bool) -> np.ndarray:
    """4×4 scale matrix: scale around center, along one axis (or all three if uniform)."""
    if uniform:
        sx = sy = sz = factor
    else:
        sx = factor if axis_idx == 0 else 1.0
        sy = factor if axis_idx == 1 else 1.0
        sz = factor if axis_idx == 2 else 1.0
    T_c  = np.eye(4, dtype=np.float32); T_c[:3, 3]  =  center
    T_nc = np.eye(4, dtype=np.float32); T_nc[:3, 3] = -center
    S = np.diag([sx, sy, sz, 1.0]).astype(np.float32)
    return T_c @ S @ T_nc


def _rotation_model(center: np.ndarray, axis_idx: int, angle_deg: float) -> np.ndarray:
    """4×4 rotation matrix: rotate around world axis by angle_deg, pivoting at center."""
    angle_rad = math.radians(angle_deg)
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    t = 1 - c
    ax, ay, az = (float(_AXES_DIRS[axis_idx, k]) for k in range(3))
    R = np.array([
        [t*ax*ax+c,    t*ax*ay-s*az, t*ax*az+s*ay, 0],
        [t*ax*ay+s*az, t*ay*ay+c,   t*ay*az-s*ax, 0],
        [t*ax*az-s*ay, t*ay*az+s*ax, t*az*az+c,   0],
        [0,            0,            0,             1],
    ], dtype=np.float32)
    T_c  = np.eye(4, dtype=np.float32); T_c[:3, 3]  =  center
    T_nc = np.eye(4, dtype=np.float32); T_nc[:3, 3] = -center
    return T_c @ R @ T_nc


# ------------------------------------------------------------------
# Picking helpers
# ------------------------------------------------------------------

def _moller_trumbore_batch(ray_origin: np.ndarray, ray_dir: np.ndarray,
                            v0: np.ndarray, v1: np.ndarray,
                            v2: np.ndarray) -> tuple[Optional[int], float]:
    eps = 1e-8
    edge1 = v1 - v0
    edge2 = v2 - v0

    h = np.cross(ray_dir[np.newaxis, :], edge2)
    a = np.sum(edge1 * h, axis=1)

    valid = np.abs(a) > eps
    inv_a = np.where(valid, 1.0 / np.where(valid, a, 1.0), 0.0)

    s = ray_origin[np.newaxis, :] - v0
    u = inv_a * np.sum(s * h, axis=1)
    valid &= (u >= 0.0) & (u <= 1.0)

    q = np.cross(s, edge1)
    v = inv_a * (q @ ray_dir)
    valid &= (v >= 0.0) & (u + v <= 1.0)

    t = inv_a * np.sum(edge2 * q, axis=1)
    valid &= t > eps

    t_vals = np.where(valid, t, np.inf)
    hit_idx = int(np.argmin(t_vals))
    if not np.isfinite(t_vals[hit_idx]):
        return None, np.inf
    return hit_idx, float(t_vals[hit_idx])


def _nice_spacings(L: float) -> tuple[float, float, float]:
    """Return (label_spacing, major_spacing, minor_spacing).

    major_spacing is the largest round subdivision below the label interval,
    so e.g. when labels are every 2 there is a major tick at every 1.
    """
    raw = max(L, 1e-9) / 14
    mag = 10 ** math.floor(math.log10(raw))
    for f in (1, 2, 5, 10):
        if f * mag >= raw:
            spacing = float(f * mag)
            minor   = spacing / 10
            if f == 1:
                major = spacing        # labelled ticks are already the majors
            elif f == 2:
                major = spacing / 2    # 1 between labels  (e.g. 1 when labels at 2)
            elif f == 5:
                major = spacing / 5    # 4 between labels  (e.g. 1–4 when labels at 5)
            else:                      # f == 10
                major = spacing / 2    # 1 between labels  (e.g. 5 when labels at 10)
            return spacing, major, minor
    spacing = mag * 10.0
    return spacing, spacing / 2, spacing / 10


def _fmt_tick(val: float, spacing: float) -> str:
    if spacing >= 1.0:
        return str(int(round(val)))
    decimals = max(0, -math.floor(math.log10(spacing)))
    return f"{val:.{decimals}f}"


def _highlight_color(color: tuple) -> tuple:
    r, g, b, a = color
    return (min(1.0, r * 0.35),
            min(1.0, g * 0.35 + 0.65),
            min(1.0, b * 0.35),
            a)


# ------------------------------------------------------------------
# Math helpers
# ------------------------------------------------------------------

def _look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    f = target - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, up)
    r_len = np.linalg.norm(r)
    if r_len < 1e-8:
        r = np.array([1, 0, 0], dtype=np.float32)
    else:
        r /= r_len
    u = np.cross(r, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3] = r
    m[1, :3] = u
    m[2, :3] = -f
    m[0, 3] = -np.dot(r, eye)
    m[1, 3] = -np.dot(u, eye)
    m[2, 3] = np.dot(f, eye)
    return m


def _perspective(fov_rad: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(fov_rad / 2)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1
    return m


def _ortho(half_w: float, half_h: float, near: float, far: float) -> np.ndarray:
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] =  1.0 / half_w
    m[1, 1] =  1.0 / half_h
    m[2, 2] = -2.0 / (far - near)
    m[2, 3] = -(far + near) / (far - near)
    m[3, 3] =  1.0
    return m
