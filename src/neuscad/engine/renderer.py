"""
ModernGL mesh renderer: uploads geometry and draws it with a simple Phong shader.
"""
from __future__ import annotations
import math
import numpy as np
from typing import Optional

import moderngl as mgl

from neuscad.engine.evaluator import ColoredBody

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
out vec4 fragColor;
void main() {
    vec3 n = normalize(v_normal);
    // Key light from upper-front-right
    float diff_key  = max(dot(n, normalize(light_dir)), 0.0);
    // Soft fill light from the opposite side
    float diff_fill = max(dot(n, normalize(-light_dir * vec3(1.0, 1.0, 0.3))), 0.0);
    vec3 col = object_color.rgb;
    vec3 lit = 0.35 * col                   // ambient
             + 0.50 * diff_key  * col       // key light
             + 0.20 * diff_fill * col;      // fill light
    fragColor = vec4(lit, object_color.a);
}
"""


class Camera:
    """Spherical-coordinate orbit camera."""

    def __init__(self):
        self.azimuth = 45.0    # degrees, rotation around Z
        self.elevation = 35.0  # degrees above horizon
        self.distance = 50.0
        self.target = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        self.fov = 45.0

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
        return _perspective(math.radians(self.fov), aspect, 0.1, 10000.0)

    def eye_position(self) -> np.ndarray:
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        return self.target + self.distance * np.array([
            math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el),
        ], dtype=np.float32)

    def frame_bounds(self, bb_min: np.ndarray, bb_max: np.ndarray):
        """Adjust distance and target so the given bounding box fits in view."""
        center = (bb_min + bb_max) / 2
        extent = np.linalg.norm(bb_max - bb_min)
        self.target = center.astype(np.float32)
        self.distance = max(extent * 1.5, 1.0)


class MeshBuffer:
    def __init__(self, ctx: mgl.Context, vbo: mgl.Buffer, ibo: mgl.Buffer, vao: mgl.VertexArray, num_indices: int, color: tuple):
        self.ctx = ctx
        self.vbo = vbo
        self.ibo = ibo
        self.vao = vao
        self.num_indices = num_indices
        self.color = color


class SceneRenderer:
    def __init__(self):
        self._ctx: Optional[mgl.Context] = None
        self._prog: Optional[mgl.Program] = None
        self._buffers: list[MeshBuffer] = []
        self.camera = Camera()
        self._viewport: tuple[int, int] = (800, 600)
        self._default_color = (0.6, 0.7, 0.85, 1.0)

    def initialize(self, ctx: mgl.Context):
        self._ctx = ctx
        self._prog = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)

    def set_viewport(self, w: int, h: int):
        self._viewport = (w, h)

    def load_geometry(self, bodies: list[ColoredBody]):
        """Upload colored Manifold bodies to GPU. Replaces current scene."""
        self._clear_buffers()
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
        self._buffers.clear()

    def _upload_body(self, cb: ColoredBody) -> Optional[MeshBuffer]:
        mesh = cb.body.to_mesh()
        verts = np.array(mesh.vert_properties, dtype=np.float32)  # (N, 3)
        tris = np.array(mesh.tri_verts, dtype=np.int32)            # (T, 3)

        if len(verts) == 0 or len(tris) == 0:
            return None

        # Flat shading: unroll triangles so each corner gets its face normal.
        # This gives 3*T unique vertices — no index buffer needed.
        v0 = verts[tris[:, 0]]  # (T, 3)
        v1 = verts[tris[:, 1]]
        v2 = verts[tris[:, 2]]

        face_normals = np.cross(v1 - v0, v2 - v0)          # (T, 3)
        lengths = np.linalg.norm(face_normals, axis=1, keepdims=True)
        lengths = np.where(lengths == 0, 1, lengths)
        face_normals /= lengths                              # normalised

        # Repeat each face normal for its 3 corners
        normals_per_corner = np.repeat(face_normals, 3, axis=0)   # (3T, 3)
        positions_per_corner = np.concatenate([v0, v1, v2], axis=1).reshape(-1, 3)  # (3T, 3)

        interleaved = np.concatenate(
            [positions_per_corner, normals_per_corner], axis=1
        ).astype(np.float32)

        vbo = self._ctx.buffer(interleaved.tobytes())
        vao = self._ctx.vertex_array(
            self._prog,
            [(vbo, "3f 3f", "in_position", "in_normal")],
        )
        color = cb.color if cb.color is not None else self._default_color
        # num_indices = number of vertices (no IBO)
        return MeshBuffer(self._ctx, vbo, None, vao, len(interleaved), color)

    def paint(self, bg_color: tuple = (0.15, 0.15, 0.15, 1.0), qt_fbo_id: int = 0):
        if self._ctx is None or self._prog is None:
            return

        # Bind Qt's FBO — QOpenGLWidget does NOT use FBO 0; detect it each frame.
        fbo = self._ctx.detect_framebuffer(qt_fbo_id)
        fbo.use()

        w, h = self._viewport
        aspect = w / h if h > 0 else 1.0

        view = self.camera.view_matrix()
        proj = self.camera.projection_matrix(aspect)
        mvp = proj @ view
        model = np.eye(4, dtype=np.float32)

        self._ctx.clear(*bg_color[:3])
        self._ctx.enable(mgl.DEPTH_TEST)

        self._prog["light_dir"].value = (0.6, 0.8, 1.0)
        self._prog["model"].write(model.T.astype(np.float32).tobytes())
        self._prog["mvp"].write(mvp.T.astype(np.float32).tobytes())

        for buf in self._buffers:
            self._prog["object_color"].value = buf.color
            buf.vao.render()

    def release(self):
        self._clear_buffers()


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
