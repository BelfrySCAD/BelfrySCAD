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
  placed by a translate, rotation and uniform scale. A click whose ray hits
  it lands on its surface.
"""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QMessageBox, QWidget)

from belfryscad.window.background_image import _IMAGE_FILTER, load_image

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
    path: str
    aspect: float = 1.0            # height / width
    width: float = 100.0
    center: tuple = (0.0, 0.0, 0.0)
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

    def set_plane(self, name: str):
        """Put the image square on a preset plane, upright, keeping its centre."""
        self.normal, _ = PLANES[name]
        self.rotation = 0.0

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

    def fit(self, lo, hi):
        """Centre on the box (lo, hi), sized to cover it as seen square-on."""
        lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
        right, up, _ = self.basis()
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                            for z in (lo[2], hi[2])])
        center = (lo + hi) / 2
        w = float(np.ptp(corners @ right))
        h = float(np.ptp(corners @ up))
        size = max(w, h / self.aspect if self.aspect else 0)
        self.width = size * 1.1 if size > 0 else self.width
        self.center = tuple(float(v) for v in center)

    def corners(self) -> np.ndarray:
        right, up, _ = self.basis()
        c = np.asarray(self.center, dtype=np.float64)
        w, h = self.width / 2, self.height / 2
        return np.array([c + sx * w * right + sy * h * up for sx, sy in
                         ((-1, -1), (1, -1), (1, 1), (-1, 1))])


@dataclass
class RefModel:
    name: str
    tris: np.ndarray               # (n, 3, 3), as read
    translate: tuple = (0.0, 0.0, 0.0)
    rotate: tuple = (0.0, 0.0, 0.0)    # degrees about X, then Y, then Z, as rotate([x, y, z])
    scale: float = 1.0
    opacity: float = 0.35
    visible: bool = True
    _cache: tuple = field(default=(None, None), repr=False, compare=False)

    def matrix(self) -> np.ndarray:
        def axis(i, deg):
            c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
            m = np.eye(3)
            j, k = [(1, 2), (2, 0), (0, 1)][i]
            m[j, j], m[j, k], m[k, j], m[k, k] = c, -s, s, c
            return m
        R = axis(2, self.rotate[2]) @ axis(1, self.rotate[1]) @ axis(0, self.rotate[0])
        m = np.eye(4)
        m[:3, :3] = R * self.scale
        m[:3, 3] = self.translate
        return m

    def world_tris(self) -> np.ndarray:
        key = (tuple(self.translate), tuple(self.rotate), self.scale, id(self.tris))
        if self._cache[0] != key:
            m = self.matrix()
            w = self.tris.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
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

    def fit(self, lo, hi):
        """Scale and move the model to sit centred in the box (lo, hi)."""
        self.translate, self.scale = (0.0, 0.0, 0.0), 1.0
        mlo, mhi = self.bounds()
        size = float(np.max(mhi - mlo))
        want = float(np.max(np.asarray(hi) - np.asarray(lo)))
        if size > 0 and want > 0:
            self.scale = want / size
        mlo, mhi = self.bounds()
        self.translate = tuple(float(v) for v in (np.asarray(lo) + np.asarray(hi)) / 2 - (mlo + mhi) / 2)


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

    def restore_references(self, key: str):
        self.ref_key = key
        images, model = _session.get(key, ([], None))
        self.ref_images = [replace(i) for i in images]
        self.ref_model = replace(model) if model is not None else None
        self._ref_textures = {}

    def references_edited(self):
        if self.ref_key:
            _session[self.ref_key] = ([replace(i) for i in self.ref_images],
                                      replace(self.ref_model) if self.ref_model else None)
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
        super()._paint_under(mvp)
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
            key = (id(m.tris), tuple(m.translate), tuple(m.rotate), m.scale)
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

    # -- the menu -----------------------------------------------------------------

    def _exec_menu(self, menu, event):
        ref = menu.addMenu("Reference")
        ref.addAction("Add Image…", self.add_reference_image)
        for k, img in enumerate(self.ref_images):
            sub = ref.addMenu(f"Image {k + 1}: {os.path.basename(img.path)}")
            sub.addAction("Face This Image", lambda img=img: self.face_image(img))
            plane = sub.addMenu("Plane")
            for name in PLANES:
                plane.addAction(name, lambda img=img, name=name: self._edit(lambda: img.set_plane(name)))
            sub.addAction("Placement…", lambda img=img: self._image_placement(img))
            fit = sub.addAction("Fit to Path", lambda img=img: self._edit(lambda: img.fit(*self._path_box())))
            fit.setEnabled(self._path_box() is not None)
            sub.addSeparator()
            sub.addAction("Show" if not img.visible else "Hide",
                          lambda img=img: self._edit(lambda: setattr(img, "visible", not img.visible)))
            sub.addAction("Remove", lambda img=img: self._edit(lambda: self.ref_images.remove(img)))
        ref.addSeparator()
        ref.addAction("Load Model…", self.load_reference_model)
        ref.addAction("Use Current Render", self.use_current_render)
        m = self.ref_model
        if m is not None:
            sub = ref.addMenu(f"Model: {m.name}")
            sub.addAction("Placement…", self._model_placement)
            fit = sub.addAction("Fit to Path", lambda: self._edit(lambda: m.fit(*self._path_box())))
            fit.setEnabled(self._path_box() is not None)
            sub.addSeparator()
            sub.addAction("Show" if not m.visible else "Hide",
                          lambda: self._edit(lambda: setattr(m, "visible", not m.visible)))
            sub.addAction("Remove", lambda: self._edit(lambda: setattr(self, "ref_model", None)))
        super()._exec_menu(menu, event)

    def _edit(self, fn):
        fn()
        self.references_edited()

    def _path_box(self):
        pts = getattr(self, "_path_pts", None)
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
            model.translate, model.rotate, model.scale = (self.ref_model.translate,
                                                          self.ref_model.rotate, self.ref_model.scale)
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
            ("Centre:", img.center, 3), ("Normal:", img.normal, 4),
            ("Rotation (°):", [img.rotation], 2), ("Width:", [img.width], 3)])
        if dlg.exec():
            center, normal, (rotation,), (width,) = dlg.values()
            if not any(normal) or width <= 0:
                return
            img.center, img.normal, img.rotation, img.width = tuple(center), tuple(normal), rotation, width
            self.references_edited()

    def _model_placement(self):
        m = self.ref_model
        dlg = _PlacementDialog("Model Placement", self, [
            ("Translate:", m.translate, 3), ("Rotate (°):", m.rotate, 2), ("Scale:", [m.scale], 4)])
        if dlg.exec():
            t, r, (s,) = dlg.values()
            if s <= 0:
                return
            m.translate, m.rotate, m.scale = tuple(t), tuple(r), s
            self.references_edited()


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
