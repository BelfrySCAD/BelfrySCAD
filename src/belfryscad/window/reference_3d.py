"""Reference geometry for the 3D path and turtle3d editors, to trace over (#836).

Two kinds, both drawn half-transparent behind the path and never written into
the script, and both kept for the session per editor title, as the 2D
background image is:

* **Images on planes.** Any number, each on its own plane: a centre, a
  normal and a rotation within the plane, with XY, XZ and YZ as presets.
  Facing one (Face This Image, or any view within 15 degrees of square-on)
  makes clicks and drags work on planes parallel to it.
* **A model.** A mesh file read through the evaluator's own `import()` --
  STL, OBJ, OFF, 3MF, AMF or X3D -- or the main window's last render,
  placed by an anchor, a rotation and a uniform scale. A click whose ray hits
  it lands on its surface.
"""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QUndoCommand
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
                               QMessageBox, QWidget)

from belfryscad.window.background_image import (ANCHORS, _IMAGE_FILTER, ImageSizeDialog,
                                                  anchor_name, load_image)

#: The plane presets: name -> (normal, the image's "up" there). Each is
#: seen square-on from the side its normal points to, upright.
PLANES = {
    "XY": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "XZ": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "YZ": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
}
_MODEL_FILTER = "Meshes (*.stl *.obj *.off *.3mf *.amf *.x3d)"
#: How near square-on, in degrees, a view must be to count as facing an image.
FACING_DEGREES = 15.0


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


@dataclass
class RefImage:
    """An image on a plane. `position` is where its `anchor` sits -- one of
    the 2D background image's nine (`ANCHORS`: fractions of its width from
    the left and height from the bottom) -- and it turns and grows about
    that point, as the 2D one does."""
    path: str
    aspect: float = 1.0            # height / width
    width: float = 100.0
    position: tuple = (0.0, 0.0, 0.0)
    anchor: tuple = (0.5, 0.5)
    normal: tuple = (0.0, 0.0, 1.0)
    rotation: float = 0.0          # degrees about the normal, from upright
    opacity: float = 0.5
    visible: bool = True

    @property
    def height(self) -> float:
        return self.width * self.aspect

    def basis(self):
        """(right, up, normal), unit vectors. Upright is world +Z projected
        onto the plane (+Y for a horizontal one), then turned by `rotation`."""
        n = _unit(self.normal)
        for ref in ((0, 0, 1), (0, 1, 0)):
            up = np.array(ref, dtype=np.float64) - (n @ ref) * n
            if np.linalg.norm(up) > 1e-6:
                break
        up = _unit(up)
        right = np.cross(up, n)
        a = math.radians(self.rotation)
        c, s = math.cos(a), math.sin(a)
        return c * right + s * up, -s * right + c * up, n

    def point_at(self, fx: float, fy: float) -> np.ndarray:
        """The point at fraction (fx, fy) of the image's width and height
        from its bottom-left corner."""
        right, up, _ = self.basis()
        ax, ay = self.anchor
        return (np.asarray(self.position, dtype=np.float64)
                + (fx - ax) * self.width * right + (fy - ay) * self.height * up)

    @property
    def center(self) -> tuple:
        return tuple(float(v) for v in self.point_at(0.5, 0.5))

    @center.setter
    def center(self, c):
        """Move the image so its centre is at `c`, keeping the anchor."""
        shift = np.asarray(c, dtype=np.float64) - np.asarray(self.center)
        self.position = tuple(float(v) for v in np.asarray(self.position) + shift)

    def set_plane(self, name: str):
        """Put the image square on a preset plane, upright, keeping its centre."""
        c = self.center
        self.normal, _ = PLANES[name]
        self.rotation = 0.0
        self.center = c

    def hit(self, o, d):
        """Where the ray (o, d) meets the image itself, or None."""
        right, up, n = self.basis()
        denom = float(n @ d)
        if abs(denom) < 1e-12:
            return None
        c = np.asarray(self.center, dtype=np.float64)
        t = float(n @ (c - o)) / denom
        if t <= 0:
            return None
        p = o + t * d
        if abs((p - c) @ right) <= self.width / 2 and abs((p - c) @ up) <= self.height / 2:
            return p
        return None

    def scale_about(self, p, k: float):
        """Grow by `k` about the point `p` on the image, which stays put."""
        p = np.asarray(p, dtype=np.float64)
        self.width *= k
        self.position = tuple(float(v) for v in p + (np.asarray(self.position) - p) * k)

    def _box_in_plane(self, lo, hi):
        """The box (lo, hi)'s extent along the image's right and up."""
        right, up, _ = self.basis()
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                            for z in (lo[2], hi[2])], dtype=np.float64)
        return corners @ right, corners @ up

    def fit(self, lo, hi):
        """Centre on the box (lo, hi), sized to cover it as seen square-on."""
        lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
        r, u = self._box_in_plane(lo, hi)
        size = max(float(np.ptp(r)), float(np.ptp(u)) / self.aspect if self.aspect else 0)
        if size > 0:
            self.width = size * 1.1
        self.center = tuple(float(v) for v in (lo + hi) / 2)

    def anchor_to_box(self, lo, hi):
        """Put the anchor on the same point of the box (lo, hi) as seen
        square-on -- Top Left on its top-left corner -- staying on the
        image's own plane."""
        right, up, n = self.basis()
        r, u = self._box_in_plane(lo, hi)
        fx, fy = self.anchor
        depth = float(np.asarray(self.position) @ n)
        p = ((r.min() + fx * np.ptp(r)) * right + (u.min() + fy * np.ptp(u)) * up + depth * n)
        self.position = tuple(float(v) for v in p)

    def corners(self) -> np.ndarray:
        return np.array([self.point_at(fx, fy) for fx, fy in ((0, 0), (1, 0), (1, 1), (0, 1))])


#: A model's anchors: a point of its bounding box, as fractions along X, Y
#: and Z -- the 2D image's nine at each of three heights.
MODEL_LEVELS = {"Top": 1.0, "Center": 0.5, "Bottom": 0.0}
_FLAT = {"Top Left": "Back Left", "Top": "Back", "Top Right": "Back Right",
         "Bottom Left": "Front Left", "Bottom": "Front", "Bottom Right": "Front Right"}


def model_anchor_name(anchor) -> str:
    """`Top Back Left`, `Bottom`, `Center`..."""
    fx, fy, fz = anchor
    flat = anchor_name((fx, fy))
    flat = _FLAT.get(flat, flat)
    level = next(n for n, z in MODEL_LEVELS.items() if z == fz)
    if level == "Center":
        return flat
    return level if flat == "Center" else f"{level} {flat}"


@dataclass
class RefModel:
    """A mesh to trace over. Turned (as `rotate([x, y, z])`) and scaled
    about its `anchor` -- a point of its bounding box, as fractions along X,
    Y and Z -- which then sits at `position`."""
    name: str
    tris: np.ndarray               # (n, 3, 3), as read
    position: tuple = (0.0, 0.0, 0.0)
    rotate: tuple = (0.0, 0.0, 0.0)
    scale: float = 1.0
    anchor: tuple = (0.5, 0.5, 0.5)
    opacity: float = 0.35
    visible: bool = True
    _cache: tuple = field(default=(None, None), repr=False, compare=False)

    def _turned(self) -> np.ndarray:
        """The triangles rotated and scaled, before the anchor goes to `position`."""
        def axis(i, deg):
            c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
            m = np.eye(3)
            j, k = [(1, 2), (2, 0), (0, 1)][i]
            m[j, j], m[j, k], m[k, j], m[k, k] = c, -s, s, c
            return m
        R = axis(2, self.rotate[2]) @ axis(1, self.rotate[1]) @ axis(0, self.rotate[0])
        return self.tris.reshape(-1, 3) @ (R * self.scale).T

    def world_tris(self) -> np.ndarray:
        key = (tuple(self.position), tuple(self.rotate), self.scale, tuple(self.anchor), id(self.tris))
        if self._cache[0] != key:
            w = self._turned()
            lo, hi = w.min(axis=0), w.max(axis=0)
            w = w - (lo + np.asarray(self.anchor) * (hi - lo)) + np.asarray(self.position)
            self._cache = (key, w.reshape(-1, 3, 3))
        return self._cache[1]

    def bounds(self):
        w = self.world_tris().reshape(-1, 3)
        return w.min(axis=0), w.max(axis=0)

    def hit(self, o, d):
        """The nearest point where the ray (o, d) meets the model, or None."""
        from belfryscad.engine.renderer import _moller_trumbore_batch
        w = self.world_tris()
        if not len(w):
            return None
        i, t = _moller_trumbore_batch(np.asarray(o, dtype=np.float64), np.asarray(d, dtype=np.float64),
                                      w[:, 0], w[:, 1], w[:, 2])
        return None if i is None else np.asarray(o, dtype=np.float64) + t * np.asarray(d, dtype=np.float64)

    def anchor_to_box(self, lo, hi):
        """Put the anchor on the same point of the box (lo, hi)."""
        lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
        self.position = tuple(float(v) for v in lo + np.asarray(self.anchor) * (hi - lo))

    def fit(self, lo, hi):
        """Scale and move the model to sit centred in the box (lo, hi)."""
        self.scale = 1.0
        w = self._turned()
        size = float(np.max(w.max(axis=0) - w.min(axis=0)))
        want = float(np.max(np.asarray(hi) - np.asarray(lo)))
        if size > 0 and want > 0:
            self.scale = want / size
        mlo, mhi = self.bounds()
        shift = (np.asarray(lo) + np.asarray(hi)) / 2 - (mlo + mhi) / 2
        self.position = tuple(float(v) for v in np.asarray(self.position) + shift)


def load_model_triangles(path: str) -> tuple[np.ndarray, list[str]]:
    """The triangles of a mesh file, read by the evaluator's own `import()`,
    and its warnings. Raises on a file it cannot read."""
    from openscad_cpp_evaluator import Evaluator
    msgs: list[str] = []
    fd, script = tempfile.mkstemp(suffix=".scad")
    try:
        with os.fdopen(fd, "w") as f:
            f.write('import("' + path.replace("\\", "/").replace('"', '\\"') + '");\n')
        bodies, _ = Evaluator(echo_fn=msgs.append).evaluate(script)
    finally:
        os.unlink(script)
    tris = [_body_tris(cb.body) for cb in bodies]
    tris = [t for t in tris if len(t)]
    if not tris:
        raise ValueError("The file has no triangles this can show.")
    return np.concatenate(tris), [m for m in msgs if "WARNING" in m or "ERROR" in m]


def _body_tris(body) -> np.ndarray:
    mesh = body.to_mesh()
    v = np.array(mesh.vert_properties, dtype=np.float64)[:, :3]
    f = np.array(mesh.tri_verts, dtype=np.int64)
    return v[f] if len(v) and len(f) else np.zeros((0, 3, 3))


def current_render_triangles() -> np.ndarray | None:
    """The main window's last render, as triangles, or None."""
    for w in QApplication.topLevelWidgets():
        vp = getattr(w, "_viewport", None)
        bufs = getattr(getattr(vp, "_renderer", None), "_buffers", None)
        if bufs:
            tris = [np.stack([b.cpu_v0, b.cpu_v1, b.cpu_v2], axis=1) for b in bufs
                    if getattr(b, "cpu_v0", None) is not None and len(b.cpu_v0)]
            if tris:
                return np.concatenate(tris).astype(np.float64)
    return None


#: Each editor title's references, for the session: ([RefImage], RefModel | None).
_session: dict[str, tuple] = {}


def _same_refs(a, b) -> bool:
    """Two ([RefImage], RefModel | None) snapshots alike. A model's
    triangles are compared by identity: copies share the array, which
    nothing changes once read, and == on it is no single bool."""
    (ia, ma), (ib, mb) = a, b
    if ia != ib:
        return False
    if ma is None or mb is None:
        return ma is mb
    return ma.tris is mb.tris and replace(ma, tris=None) == replace(mb, tris=None)


class _ReferencesCmd(QUndoCommand):
    """One change to the references, on the editor's own undo stack beside
    its edits to the path, as the 2D background image's _BackgroundCmd.
    Pushed after the change is made, so the first redo() -- which
    QUndoStack.push runs -- has nothing to do."""

    def __init__(self, viewport, before, after):
        super().__init__("Reference")
        self._vp, self._before, self._after = viewport, before, after
        self._done = True

    def undo(self):
        self._vp._apply_references(self._before)

    def redo(self):
        if self._done:
            self._done = False
            return
        self._vp._apply_references(self._after)


_IMAGE_VERT = """
#version 330 core
in vec2 in_position;
in vec2 in_uv;
uniform mat4 mvp;
uniform vec3 center;
uniform vec3 right;
uniform vec3 up;
out vec2 v_uv;
void main() {
    gl_Position = mvp * vec4(center + right * in_position.x + up * in_position.y, 1.0);
    v_uv = in_uv;
}
"""
_IMAGE_FRAG = """
#version 330 core
in vec2 v_uv;
uniform sampler2D tex;
uniform float opacity;
out vec4 fragColor;
void main() {
    vec4 c = texture(tex, v_uv);
    fragColor = vec4(c.rgb, c.a * opacity);
}
"""
_MODEL_VERT = """
#version 330 core
in vec3 in_position;
in vec3 in_normal;
uniform mat4 mvp;
out vec3 v_normal;
void main() {
    gl_Position = mvp * vec4(in_position, 1.0);
    v_normal = in_normal;
}
"""
_MODEL_FRAG = """
#version 330 core
in vec3 v_normal;
uniform vec3 view_dir;
uniform vec4 color;
out vec4 fragColor;
void main() {
    float lit = 0.45 + 0.55 * abs(dot(normalize(v_normal), view_dir));
    fragColor = vec4(color.rgb * lit, color.a);
}
"""
_MODEL_COLOR = (0.45, 0.6, 0.9)


class Reference3DViewportMixin:
    """For a 3D viewport: draws `ref_images` and `ref_model` behind
    everything, offers them in the context menu, and gives clicks a plane
    (`_pick_plane_normal`) and a surface (`_reference_hit`) to land on."""

    ref_images: list = []
    ref_model: RefModel | None = None
    ref_key = ""
    _ref_progs = None
    _ref_textures: dict = {}
    _ref_model_vao = None
    _ref_model_key = None

    _ref_committed = ([], None)     # the references as the last undo step left them

    def restore_references(self, key: str):
        self.ref_key = key
        images, model = _session.get(key, ([], None))
        self.ref_images = [replace(i) for i in images]
        self.ref_model = replace(model) if model is not None else None
        self._ref_textures = {}
        self._ref_committed = self._ref_snapshot()

    def _ref_snapshot(self):
        return ([replace(i) for i in self.ref_images],
                replace(self.ref_model) if self.ref_model else None)

    def references_edited(self):
        if self.ref_key:
            _session[self.ref_key] = self._ref_snapshot()
        # One undo step per change; a Move Image drag's is pushed on
        # release, for the whole drag.
        if self._ref_drag is None:
            before, after = self._ref_committed, self._ref_snapshot()
            self._ref_committed = after
            stack = getattr(self.window(), "_undo_stack", None)   # editors only
            if stack is not None and not _same_refs(before, after):
                stack.push(_ReferencesCmd(self, before, after))
        self._extend_scene_bounds()
        self.update()

    def _apply_references(self, snap):
        """Undo/Redo: put the references back as they were, without a new
        step, leaving any Move Image or second-point pick under way."""
        if self._ref_moving is not None or self._ref_pick is not None:
            self._ref_moving = self._ref_drag = self._ref_pick = None
            self._delta_label.hide()
            self.unsetCursor()
        images, model = snap
        self.ref_images = [replace(i) for i in images]
        self.ref_model = replace(model) if model is not None else None
        self._ref_committed = snap
        if self.ref_key:
            _session[self.ref_key] = self._ref_snapshot()
        self._extend_scene_bounds()
        self.update()

    # -- what clicks land on --------------------------------------------------

    def faced_image(self) -> RefImage | None:
        """The shown image the view is most nearly square-on to, within
        FACING_DEGREES, or None."""
        cam = self._renderer.camera
        view = _unit(np.asarray(cam.target, dtype=np.float64) - cam.eye_position())
        best, best_cos = None, math.cos(math.radians(FACING_DEGREES))
        for img in self.ref_images:
            if img.visible:
                c = abs(float(view @ _unit(img.normal)))
                if c >= best_cos:
                    best, best_cos = img, c
        return best

    def _pick_plane_normal(self):
        img = self.faced_image()
        return None if img is None else _unit(img.normal)

    def _reference_nudge(self, key, magnitude: float):
        """An arrow key's step along a faced image's plane: screen-right and
        screen-up projected onto it, so a press moves along the image as it
        is seen. None when no image is faced (the axis-plane nudge then)."""
        img = self.faced_image()
        if img is None:
            return None
        view = self._renderer.camera.view_matrix()
        n = _unit(img.normal)
        row = {Qt.Key.Key_Right: (0, 1), Qt.Key.Key_Left: (0, -1),
               Qt.Key.Key_Up: (1, 1), Qt.Key.Key_Down: (1, -1)}.get(key)
        if row is None:
            return None
        v = np.asarray(view[row[0], :3], dtype=np.float64)
        v = _unit(v - (v @ n) * n)
        return (row[1] * magnitude * v).astype(np.float32)

    def _reference_hit(self, o, d):
        m = self.ref_model
        if m is None or not m.visible:
            return None
        return m.hit(o, d)

    # -- drawing ----------------------------------------------------------------

    def _extend_scene_bounds(self):
        """Widen the camera's scene bounds to the references, so the clip
        planes never cut one off."""
        cam = self._renderer.camera
        boxes = [] if cam.scene_bounds is None else [tuple(np.asarray(b, dtype=np.float64)
                                                           for b in cam.scene_bounds)]
        for img in self.ref_images:
            c = img.corners()
            boxes.append((c.min(axis=0), c.max(axis=0)))
        if self.ref_model is not None:
            boxes.append(self.ref_model.bounds())
        if boxes:
            lo = np.min([b[0] for b in boxes], axis=0)
            hi = np.max([b[1] for b in boxes], axis=0)
            cam.scene_bounds = (lo.astype(np.float32), hi.astype(np.float32))

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        super().frame_scene(bb_min, bb_max, reframe=reframe)
        self._extend_scene_bounds()

    def _paint_under(self, mvp: np.ndarray):
        under = getattr(super(), "_paint_under", None)   # the 2D background image, where there is one
        if under is not None:
            under(mvp)
        if not self.ref_images and self.ref_model is None:
            return
        import moderngl as mgl
        ctx = self._ctx
        if self._ref_progs is None:
            img = ctx.program(vertex_shader=_IMAGE_VERT, fragment_shader=_IMAGE_FRAG)
            quad = np.array([-0.5, -0.5, 0, 1, 0.5, -0.5, 1, 1, -0.5, 0.5, 0, 0,
                             0.5, 0.5, 1, 0], dtype=np.float32)
            img_vao = ctx.vertex_array(img, [(ctx.buffer(quad.tobytes()), "2f 2f", "in_position", "in_uv")])
            self._ref_progs = (img, img_vao, ctx.program(vertex_shader=_MODEL_VERT,
                                                         fragment_shader=_MODEL_FRAG))
        img_prog, img_vao, model_prog = self._ref_progs
        ctx.enable(mgl.BLEND)
        # Destination alpha left alone, as the 2D background image does.
        ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA, mgl.ZERO, mgl.ONE
        fbo = self._renderer._active_fbo
        # Behind everything: they write no depth, so the path always shows.
        fbo.depth_mask = False
        mvp32 = mvp.T.astype(np.float32).tobytes()
        m = self.ref_model
        if m is not None and m.visible:
            key = (id(m.tris), tuple(m.position), tuple(m.rotate), m.scale, tuple(m.anchor))
            if self._ref_model_key != key:
                if self._ref_model_vao is not None:
                    self._ref_model_vao.release()
                w = m.world_tris()
                n = np.cross(w[:, 1] - w[:, 0], w[:, 2] - w[:, 0])
                n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
                data = np.concatenate([w, np.repeat(n[:, None, :], 3, axis=1)], axis=2)
                buf = ctx.buffer(data.astype(np.float32).tobytes())
                self._ref_model_vao = ctx.vertex_array(model_prog, [(buf, "3f 3f", "in_position", "in_normal")])
                self._ref_model_key = key
            cam = self._renderer.camera
            view = _unit(np.asarray(cam.target, dtype=np.float64) - cam.eye_position())
            model_prog["mvp"].write(mvp32)
            model_prog["view_dir"].value = tuple(float(v) for v in view)
            model_prog["color"].value = (*_MODEL_COLOR, m.opacity)
            self._ref_model_vao.render(mgl.TRIANGLES)
        for img in self.ref_images:
            if not img.visible:
                continue
            tex = self._ref_textures.get(img.path)
            if tex is None:
                q = load_image(img.path)
                if q is None:
                    continue
                tex = ctx.texture((q.width(), q.height()), 4, bytes(q.constBits()))
                tex.build_mipmaps()
                tex.filter = (mgl.LINEAR_MIPMAP_LINEAR, mgl.LINEAR)
                self._ref_textures[img.path] = tex
            right, up, _ = img.basis()
            img_prog["mvp"].write(mvp32)
            img_prog["center"].value = tuple(float(v) for v in img.center)
            img_prog["right"].value = tuple(float(v) for v in right * img.width)
            img_prog["up"].value = tuple(float(v) for v in up * img.height)
            img_prog["opacity"].value = img.opacity
            tex.use(0)
            img_prog["tex"].value = 0
            img_vao.render(mgl.TRIANGLE_STRIP)
        fbo.depth_mask = True
        ctx.disable(mgl.BLEND)

    # -- moving an image with the mouse ----------------------------------------------

    _ref_moving = None          # the RefImage being moved, while in Move Image
    _ref_drag = None            # (mode, image at press, press point, press ray)
    _ref_pick = None            # (image, first point), while Scale from This Point waits

    def begin_move_image(self, img: RefImage):
        """Drags move `img` until Esc or Return: in its plane; with Shift
        along its normal; with Option turning it about its anchor."""
        self._ref_moving = img
        img.visible = True
        self._show_delta("Moving image: drag it in its plane, Shift-drag along its normal, "
                         "Option-drag to turn it about its anchor. Esc when done.")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocus()
        self.references_edited()

    def end_move_image(self):
        if self._ref_moving is None:
            return
        self._ref_moving = self._ref_drag = None
        self._delta_label.hide()
        self.unsetCursor()
        self.references_edited()

    def event(self, ev):
        # Ahead of the editor's own handlers, which would select or drag
        # the vertices underneath.
        if self._ref_pick is not None:
            if ev.type() == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
                self._end_pick()
                return True
            if (ev.type() == QEvent.Type.MouseButtonPress
                    and ev.button() == Qt.MouseButton.LeftButton):
                img, a = self._ref_pick
                self._end_pick()
                b = _plane_point(img, a, *self._ray(ev.position()))
                if b is not None:
                    self._finish_scale(img, a, b)
                return True
        if self._ref_moving is not None and ev.type() in (
                QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            if self._move_image_mouse(ev):
                return True
        if (self._ref_moving is not None and ev.type() == QEvent.Type.KeyPress
                and ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.end_move_image()
            return True
        return super().event(ev)

    def _ray(self, pos):
        o, d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
        return np.asarray(o, dtype=np.float64), np.asarray(d, dtype=np.float64)

    def _image_under(self, pos):
        """(image, point) for the nearest shown image under `pos`, or None."""
        o, d = self._ray(pos)
        hits = [(float((p - o) @ d), img, p) for img in self.ref_images
                if img.visible and (p := img.hit(o, d)) is not None]
        return min(hits, key=lambda h: h[0])[1:] if hits else None

    # As the 2D background image's tools of the same names.

    def _move_image_point(self, img: RefImage, p):
        dlg = _PlacementDialog("Move Point To", self, [("Move the point clicked to:", p, 3)])
        if dlg.exec():
            img.position = tuple(float(v) for v in
                                 np.asarray(img.position) + np.asarray(dlg.values()[0]) - p)
            self.references_edited()

    def _scale_image_from(self, img: RefImage, a):
        self._ref_pick = (img, a)
        self._show_delta("Click a second point on the image (Esc cancels)")
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocus()

    def _end_pick(self):
        self._ref_pick = None
        self._delta_label.hide()
        self.unsetCursor()

    def _finish_scale(self, img: RefImage, a, b):
        measured = float(np.linalg.norm(b - a))
        if measured <= 0:
            return
        real, ok = QInputDialog.getDouble(
            self, "Scale from Two Points",
            f"The points are {measured:.4g} apart now. Real distance:",
            measured, 1e-6, 1e6, 4)
        if ok and real > 0:
            img.scale_about(a, real / measured)
            self.references_edited()

    def _move_image_mouse(self, ev) -> bool:
        img = self._ref_moving
        t = ev.type()
        if t == QEvent.Type.MouseButtonDblClick:
            return True
        left = Qt.MouseButton.LeftButton
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease) and ev.button() != left:
            return False            # right and middle drags still turn and pan the view
        if t == QEvent.Type.MouseMove and ev.buttons() not in (Qt.MouseButton.NoButton, left):
            return False
        pos = ev.position()
        o, d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
        o, d = np.asarray(o, dtype=np.float64), np.asarray(d, dtype=np.float64)
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            mods = ev.modifiers()
            mode = ("normal" if mods & Qt.KeyboardModifier.ShiftModifier
                    else "turn" if mods & Qt.KeyboardModifier.AltModifier else "plane")
            p0 = _plane_point(img, img.center, o, d)
            if p0 is None and mode != "normal":
                return True
            self._ref_drag = (mode, replace(img), p0, (o, d))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if t == QEvent.Type.MouseMove and self._ref_drag is not None:
            drag_ref_image(img, *self._ref_drag, o, d)
            self.references_edited()
            return True
        if t == QEvent.Type.MouseButtonRelease and self._ref_drag is not None:
            self._ref_drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.references_edited()        # the drag's one undo step
            return True
        return t != QEvent.Type.MouseMove     # a left click off the image does nothing else

    # -- the menu -----------------------------------------------------------------

    def _exec_menu(self, menu, event):
        ref = menu.addMenu("Reference")
        ref.addAction("Add Image…", self.add_reference_image)
        under = self._image_under(event.pos())
        for k, img in enumerate(self.ref_images):
            sub = ref.addMenu(f"Image {k + 1}: {os.path.basename(img.path)}")
            sub.addAction("Face This Image", lambda img=img: self.face_image(img))
            plane = sub.addMenu("Plane")
            for name in PLANES:
                plane.addAction(name, lambda img=img, name=name: self._edit(lambda: img.set_plane(name)))
            move = sub.addAction("Move Image", lambda img=img: (
                self.end_move_image() if self._ref_moving is img else self.begin_move_image(img)))
            move.setCheckable(True)
            move.setChecked(self._ref_moving is img)
            if under is not None and under[0] is img:
                p = under[1]
                sub.addAction("Move This Point To…", lambda img=img: self._move_image_point(img, p))
            sub.addAction("Move Anchor To…", lambda img=img: self._move_anchor(img))
            if under is not None and under[0] is img:
                sub.addAction("Scale from This Point…", lambda img=img: self._scale_image_from(img, p))
            sub.addAction("Set Image Size…", lambda img=img: self._image_size(img))
            sub.addAction("Placement…", lambda img=img: self._image_placement(img))
            sub.addSeparator()
            anchors = sub.addMenu("Image Anchor")
            for i, (name, a) in enumerate(ANCHORS.items()):
                if i and i % 3 == 0:
                    anchors.addSeparator()
                act = anchors.addAction(name, lambda img=img, a=a: self._edit(lambda: setattr(img, "anchor", a)))
                act.setCheckable(True)
                act.setChecked(tuple(img.anchor) == a)
            self._align_menu(sub, "Align Image", img, anchor_name(img.anchor))
            sub.addSeparator()
            sub.addAction("Show" if not img.visible else "Hide",
                          lambda img=img: self._edit(lambda: setattr(img, "visible", not img.visible)))
            sub.addAction("Remove", lambda img=img: self._edit(lambda: self._remove_image(img)))
        ref.addSeparator()
        ref.addAction("Load Model…", self.load_reference_model)
        ref.addAction("Use Current Render", self.use_current_render)
        m = self.ref_model
        if m is not None:
            sub = ref.addMenu(f"Model: {m.name}")
            sub.addAction("Move Anchor To…", lambda: self._move_anchor(m))
            sub.addAction("Placement…", self._model_placement)
            sub.addSeparator()
            anchors = sub.addMenu("Model Anchor")
            for level, fz in MODEL_LEVELS.items():
                lm = anchors.addMenu(level)
                for i, (fx, fy) in enumerate(ANCHORS.values()):
                    if i and i % 3 == 0:
                        lm.addSeparator()
                    a = (fx, fy, fz)
                    act = lm.addAction(model_anchor_name(a),
                                       lambda a=a: self._edit(lambda: setattr(m, "anchor", a)))
                    act.setCheckable(True)
                    act.setChecked(tuple(m.anchor) == a)
            self._align_menu(sub, "Align Model", m, model_anchor_name(m.anchor))
            sub.addSeparator()
            sub.addAction("Show" if not m.visible else "Hide",
                          lambda: self._edit(lambda: setattr(m, "visible", not m.visible)))
            sub.addAction("Remove", lambda: self._edit(lambda: setattr(self, "ref_model", None)))
        base = getattr(super(), "_exec_menu", None)
        if base is not None:
            base(menu, event)
            return
        # As BackgroundImageViewportMixin._exec_menu does, and why: Qt may
        # never deliver this press's release, leaving a pan armed.
        self._last_mouse = None
        self._mouse_button = None
        menu.exec(event.globalPos())

    def _align_menu(self, parent, title, ref, anchor_label):
        """Align ▸ as the 2D image has it: the anchor to the origin or to the
        same point of the path's bounding box, or fit to the path."""
        box = self._path_box()
        align = parent.addMenu(title)
        align.addAction("Anchor to Origin", lambda: self._edit(lambda: _anchor_to_origin(ref)))
        on_path = align.addAction(f"Anchor to Path's {anchor_label}",
                                  lambda: self._edit(lambda: ref.anchor_to_box(*box)))
        align.addSeparator()
        fit = align.addAction("Fit to Path", lambda: self._edit(lambda: ref.fit(*box)))
        for act in (on_path, fit):
            act.setEnabled(box is not None)

    def _move_anchor(self, ref):
        name = anchor_name(ref.anchor) if isinstance(ref, RefImage) else model_anchor_name(ref.anchor)
        dlg = _PlacementDialog("Move Anchor To", self, [(f"{name} anchor at:", ref.position, 3)])
        if dlg.exec():
            ref.position = tuple(dlg.values()[0])
            self.references_edited()

    def _image_size(self, img: RefImage):
        """Width and height, linked by the image's proportions; it grows or
        shrinks about its anchor."""
        size = ImageSizeDialog(img.width, img.aspect, self)
        if size.exec() == QDialog.DialogCode.Accepted:
            img.width = size.image_width()
            self.references_edited()

    def _remove_image(self, img):
        if self._ref_moving is img:
            self.end_move_image()
        self.ref_images.remove(img)

    def _edit(self, fn):
        fn()
        self.references_edited()

    def _path_box(self):
        """The bounding box of what is being edited: a path's points, or a
        VNF's vertices. None when it has no extent."""
        pts = getattr(self, "_path_pts", None)
        if pts is None:
            pts = getattr(self, "_verts_3d", None)
        if pts is None or len(pts) < 2:
            return None
        lo, hi = pts.min(axis=0).astype(np.float64), pts.max(axis=0).astype(np.float64)
        if np.all(hi - lo <= 0):
            return None
        return lo, hi

    def add_reference_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Reference Image", "", _IMAGE_FILTER)
        if not path:
            return
        q = load_image(path)
        if q is None:
            QMessageBox.warning(self, "Reference Image", "Not an image this build can read.")
            return
        # On the axis plane the view is nearest square-on to, fitted to the path.
        from belfryscad.window.viewport import _view_locked_axis
        name = ("YZ", "XZ", "XY")[_view_locked_axis(self._renderer.camera)]
        img = RefImage(path, aspect=q.height() / q.width())
        img.set_plane(name)
        box = self._path_box()
        if box is not None:
            img.fit(*box)
        self.ref_images.append(img)
        self.references_edited()

    def face_image(self, img: RefImage):
        """Look at the image square-on, upright, centred on it."""
        right, up, n = img.basis()
        cam = self._renderer.camera
        cam.target = np.asarray(img.center, dtype=np.float32)
        cam._set_from_eye_and_up(cam.target + n * cam.distance, up)
        if hasattr(self, "_on_zoom_changed"):
            self._on_zoom_changed()
        self.update()

    def _set_model(self, name, tris, warnings=()):
        model = RefModel(name, tris)
        box = self._path_box()
        if box is not None and self.ref_model is None:
            model.fit(*box)
        elif self.ref_model is not None:
            # A replacement keeps the old one's placement.
            old = self.ref_model
            model.position, model.rotate, model.scale, model.anchor = (old.position, old.rotate,
                                                                       old.scale, old.anchor)
        self.ref_model = model
        self.references_edited()
        if warnings:
            QMessageBox.information(self, "Reference Model", "\n".join(warnings[:10]))

    def load_reference_model(self):
        path, _ = QFileDialog.getOpenFileName(self, "Reference Model", "", _MODEL_FILTER)
        if not path:
            return
        try:
            tris, warnings = load_model_triangles(path)
        except Exception as e:     # whatever import() raises: say so, never crash
            QMessageBox.warning(self, "Reference Model", f"Could not read {os.path.basename(path)}:\n{e}")
            return
        self._set_model(os.path.basename(path), tris, warnings)

    def use_current_render(self):
        tris = current_render_triangles()
        if tris is None:
            QMessageBox.information(self, "Reference Model", "Nothing is rendered yet.")
            return
        self._set_model("Current Render", tris)

    def _image_placement(self, img: RefImage):
        right, up, n = img.basis()
        dlg = _PlacementDialog("Image Placement", self, [
            (f"{anchor_name(img.anchor)} anchor at:", img.position, 3), ("Normal:", img.normal, 4),
            ("Rotation (°):", [img.rotation], 2), ("Width:", [img.width], 3)])
        if dlg.exec():
            position, normal, (rotation,), (width,) = dlg.values()
            if not any(normal) or width <= 0:
                return
            img.position, img.normal, img.rotation, img.width = tuple(position), tuple(normal), rotation, width
            self.references_edited()

    def _model_placement(self):
        m = self.ref_model
        dlg = _PlacementDialog("Model Placement", self, [
            (f"{model_anchor_name(m.anchor)} anchor at:", m.position, 3),
            ("Rotate (°):", m.rotate, 2), ("Scale:", [m.scale], 4)])
        if dlg.exec():
            t, r, (s,) = dlg.values()
            if s <= 0:
                return
            m.position, m.rotate, m.scale = tuple(t), tuple(r), s
            self.references_edited()


def _anchor_to_origin(ref):
    ref.position = (0.0, 0.0, 0.0)


def _plane_point(img: RefImage, through, o, d):
    """Where the ray (o, d) meets the plane through `through` parallel to
    `img`, or None."""
    n = _unit(img.normal)
    denom = float(n @ d)
    if abs(denom) < 1e-12:
        return None
    t = float(n @ (np.asarray(through, dtype=np.float64) - o)) / denom
    return o + t * d


def drag_ref_image(img: RefImage, mode: str, start: RefImage, p0, ray0, o, d):
    """One step of a Move Image drag, from the image as it was at the press
    (`start`, pressed at `p0` on its plane, along `ray0`) to the ray (o, d)
    under the pointer now. "plane": shift by the pointer's travel across
    the plane. "normal": along the normal, by how far along it the
    pointer's ray now passes. "turn": about the anchor, by the angle the
    pointer swept round it, as the 2D image turns."""
    c0 = np.asarray(start.center, dtype=np.float64)
    n = _unit(start.normal)
    if mode == "plane":
        p = _plane_point(start, c0, o, d)
        if p is not None:
            img.center = tuple(float(v) for v in c0 + (p - p0))
    elif mode == "normal":
        def along(o_, d_):
            # The parameter on the line c0 + s*n nearest the ray (o_, d_).
            w = c0 - o_
            b, dd, e = float(n @ d_), float(d_ @ d_), float(n @ w)
            den = dd - b * b
            return None if abs(den) < 1e-12 else (b * float(d_ @ w) - dd * e) / den
        s0, s = along(*ray0), along(o, d)
        if s0 is not None and s is not None:
            img.center = tuple(float(v) for v in c0 + (s - s0) * n)
    elif mode == "turn":
        p = _plane_point(start, c0, o, d)
        if p is not None and p0 is not None:
            right, up, _ = start.basis()
            k = np.asarray(start.position, dtype=np.float64)
            a0 = math.atan2((p0 - k) @ up, (p0 - k) @ right)
            a1 = math.atan2((p - k) @ up, (p - k) @ right)
            img.rotation = (start.rotation + math.degrees(a1 - a0) + 180) % 360 - 180


class _PlacementDialog(QDialog):
    """Rows of number boxes: (label, initial values, decimals)."""

    def __init__(self, title, parent, rows):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self._rows = []
        for label, values, decimals in rows:
            line = QWidget()
            box = QHBoxLayout(line)
            box.setContentsMargins(0, 0, 0, 0)
            spins = []
            for v in values:
                sb = QDoubleSpinBox()
                sb.setRange(-1e7, 1e7)
                sb.setDecimals(decimals)
                sb.setValue(float(v))
                box.addWidget(sb)
                spins.append(sb)
            form.addRow(label, line)
            self._rows.append(spins)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return [[sb.value() for sb in spins] for spins in self._rows]
