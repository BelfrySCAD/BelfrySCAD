"""A reference image under the 2D path and region editors, to trace over (#788).

The image lies in the XY plane, under the shape, and is never written into the
script. Its placement is kept for the session only, per editor title, so closing
and reopening an editor on the same literal finds it where it was left.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QPushButton,
                               QSlider, QVBoxLayout)

#: Which point of the image sits at (x, y), as fractions of its width and
#: height measured from its bottom-left corner.
ANCHORS = {
    "Top Left": (0.0, 1.0), "Top": (0.5, 1.0), "Top Right": (1.0, 1.0),
    "Left": (0.0, 0.5), "Center": (0.5, 0.5), "Right": (1.0, 0.5),
    "Bottom Left": (0.0, 0.0), "Bottom": (0.5, 0.0), "Bottom Right": (1.0, 0.0),
}

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)"
#: Bigger photos are scaled down to this before upload: past every GL's
#: texture limit otherwise, and more detail than a trace needs.
_MAX_TEXTURE = 8192


@dataclass
class BackgroundImage:
    path: str = ""
    aspect: float = 1.0        # height / width, from the file
    width: float = 100.0       # in model units
    x: float = 0.0             # where the anchor point sits
    y: float = 0.0
    anchor: str = "Center"
    rotation: float = 0.0      # degrees, counter-clockwise
    opacity: float = 0.5
    visible: bool = True

    @property
    def height(self) -> float:
        return self.width * self.aspect

    def _rot(self):
        a = math.radians(self.rotation)
        return math.cos(a), math.sin(a)

    def center(self) -> tuple[float, float]:
        """The image's centre in model coordinates."""
        fx, fy = ANCHORS[self.anchor]
        lx, ly = (fx - 0.5) * self.width, (fy - 0.5) * self.height
        c, s = self._rot()
        return self.x - (c * lx - s * ly), self.y - (s * lx + c * ly)

    def place_center(self, cx: float, cy: float):
        """Move the image so its centre is at (cx, cy), keeping the anchor."""
        ox, oy = self.center()
        self.x += cx - ox
        self.y += cy - oy

    def fit(self, bounds):
        """Scale and move the image to fit inside `bounds`
        ((min_x, min_y, max_x, max_y)), centred, at its current rotation."""
        x0, y0, x1, y1 = bounds
        bw, bh = x1 - x0, y1 - y0
        if bw <= 0 and bh <= 0:
            return
        c, s = abs(self._rot()[0]), abs(self._rot()[1])
        # Extent of the rotated image per unit of width.
        ex, ey = c + s * self.aspect, s + c * self.aspect
        scales = [d / e for d, e in ((bw, ex), (bh, ey)) if d > 0 and e > 0]
        self.width = min(scales)
        self.place_center((x0 + x1) / 2, (y0 + y1) / 2)

    def scale_about(self, px: float, py: float, factor: float):
        """Scale by `factor`, keeping model point (px, py) where it is."""
        self.width *= factor
        self.x = px + (self.x - px) * factor
        self.y = py + (self.y - py) * factor


#: The session's images, by editor window title.
_session: dict[str, BackgroundImage] = {}


def load_image(path: str) -> QImage | None:
    img = QImage(path)
    if img.isNull():
        return None
    if max(img.width(), img.height()) > _MAX_TEXTURE:
        img = img.scaled(_MAX_TEXTURE, _MAX_TEXTURE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img.convertToFormat(QImage.Format.Format_RGBA8888)


_VERT = """
#version 330 core
in vec2 in_position;
in vec2 in_uv;
uniform mat4 mvp;
uniform vec2 center;
uniform vec2 right;
uniform vec2 up;
out vec2 v_uv;
void main() {
    vec2 p = center + right * in_position.x + up * in_position.y;
    gl_Position = mvp * vec4(p, 0.0, 1.0);
    // Behind everything at z=0, which is where the shape is drawn.
    gl_Position.z += 0.0001 * gl_Position.w;
    v_uv = in_uv;
}
"""

_FRAG = """
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


class BackgroundImageViewportMixin:
    """For a top-down 2D viewport: draws `self.background` under the shape,
    and can hand the next plain clicks' model points to a callback."""

    background: BackgroundImage | None = None
    _bg_prog = None
    _bg_vao = None
    _bg_tex = None
    _bg_tex_path = ""
    _bg_pick = None            # (points still wanted, collected, callback)

    def set_background(self, bg: BackgroundImage | None):
        self.background = bg
        self.update()

    def _paint_background(self, mvp: np.ndarray):
        bg = self.background
        if bg is None or not bg.visible or not bg.path:
            return
        import moderngl as mgl
        ctx = self._ctx
        if self._bg_prog is None:
            self._bg_prog = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
            # Top row of a QImage is uploaded first, so it is v=0.
            quad = np.array([-0.5, -0.5, 0, 1, 0.5, -0.5, 1, 1, -0.5, 0.5, 0, 0,
                             0.5, 0.5, 1, 0], dtype=np.float32)
            self._bg_vao = ctx.vertex_array(
                self._bg_prog, [(ctx.buffer(quad.tobytes()), "2f 2f", "in_position", "in_uv")])
        if self._bg_tex_path != bg.path:
            if self._bg_tex is not None:
                self._bg_tex.release()
                self._bg_tex = None
            self._bg_tex_path = bg.path
            img = load_image(bg.path)
            if img is not None:
                self._bg_tex = ctx.texture((img.width(), img.height()), 4,
                                           bytes(img.constBits()))
                self._bg_tex.build_mipmaps()
                self._bg_tex.filter = (mgl.LINEAR_MIPMAP_LINEAR, mgl.LINEAR)
        if self._bg_tex is None:
            return
        c, s = bg._rot()
        prog = self._bg_prog
        prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        prog["center"].value = bg.center()
        prog["right"].value = (c * bg.width, s * bg.width)
        prog["up"].value = (-s * bg.height, c * bg.height)
        prog["opacity"].value = bg.opacity
        self._bg_tex.use(0)
        prog["tex"].value = 0
        ctx.enable(mgl.BLEND)
        # Destination alpha left alone: Qt composites the widget by it, and
        # blending it too darkened and tinted the whole image.
        ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA, mgl.ZERO, mgl.ONE
        fbo = self._renderer._active_fbo
        fbo.depth_mask = False
        self._bg_vao.render(mgl.TRIANGLE_STRIP)
        fbo.depth_mask = True
        ctx.disable(mgl.BLEND)

    def shape_bounds(self):
        """(min_x, min_y, max_x, max_y) of the shape, or None if empty."""
        pts = getattr(self, "_path_pts", None)
        if pts is None:
            pts = getattr(self, "_all_pts", None)
        if pts is None or len(pts) == 0:
            return None
        lo, hi = pts[:, :2].min(axis=0), pts[:, :2].max(axis=0)
        return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])

    def pick_points(self, n: int, callback):
        """Pass the next `n` plain left clicks' model (x, y) to `callback`
        as a list, instead of selecting or orbiting."""
        self._bg_pick = (n, [], callback)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def cancel_pick(self):
        self._bg_pick = None
        self.unsetCursor()

    def mousePressEvent(self, event):
        if (self._bg_pick is not None and event.button() == Qt.MouseButton.LeftButton
                and not event.modifiers()):
            pos = event.position()
            o, d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
            if abs(d[2]) > 1e-9:
                t = -o[2] / d[2]
                n, got, callback = self._bg_pick
                got.append((float(o[0] + t * d[0]), float(o[1] + t * d[1])))
                if len(got) >= n:
                    self.cancel_pick()
                    callback(got)
            # The subclass's release handler selects on a click it saw pressed.
            self._press_pos = None
            return
        super().mousePressEvent(event)


class BackgroundImagePanel(QDialog):
    """Choose, place and scale an editor's background image."""

    def __init__(self, key: str, viewport, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Background Image")
        self._key = key
        self._vp = viewport
        self._bg = _session.get(key) or BackgroundImage()
        self._updating = False

        form = QFormLayout()
        file_row = QHBoxLayout()
        self._file_label = QLabel()
        self._file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        file_row.addWidget(self._file_label, 1)
        file_row.addWidget(choose)
        file_row.addWidget(clear)
        form.addRow("Image:", file_row)

        self._visible = QCheckBox("Show")
        self._visible.toggled.connect(self._changed)
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(5, 100)
        self._opacity.valueChanged.connect(self._changed)
        vis_row = QHBoxLayout()
        vis_row.addWidget(self._visible)
        vis_row.addWidget(QLabel("Opacity"))
        vis_row.addWidget(self._opacity, 1)
        form.addRow("", vis_row)

        self._anchor = QComboBox()
        self._anchor.addItems(list(ANCHORS))
        self._anchor.currentTextChanged.connect(self._anchor_changed)
        form.addRow("Anchor:", self._anchor)

        def spin(lo, hi, decimals=3, suffix=""):
            sb = QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(decimals)
            sb.setSuffix(suffix)
            sb.setKeyboardTracking(False)
            sb.valueChanged.connect(self._changed)
            return sb
        self._x, self._y = spin(-1e6, 1e6), spin(-1e6, 1e6)
        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("X"))
        pos_row.addWidget(self._x, 1)
        pos_row.addWidget(QLabel("Y"))
        pos_row.addWidget(self._y, 1)
        form.addRow("Anchor at:", pos_row)

        self._width = spin(1e-6, 1e6)
        self._height_label = QLabel()
        size_row = QHBoxLayout()
        size_row.addWidget(self._width, 1)
        size_row.addWidget(self._height_label)
        form.addRow("Width:", size_row)

        self._rotation = spin(-360.0, 360.0, 2, "°")
        form.addRow("Rotation:", self._rotation)

        align_row = QHBoxLayout()
        for label, slot in (("Anchor at Origin", self._anchor_at_origin),
                            ("Center on Shape", self._center_on_shape),
                            ("Fit to Shape", self._fit_to_shape)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            align_row.addWidget(b)
        form.addRow("Align:", align_row)

        scale_row = QHBoxLayout()
        self._scale_btn = QPushButton("Scale from Two Points…")
        self._scale_btn.setToolTip("Click two points on the image a known distance apart, "
                                   "then enter that distance")
        self._scale_btn.clicked.connect(self._start_two_point_scale)
        self._move_btn = QPushButton("Move Point To…")
        self._move_btn.setToolTip("Click a point on the image, then enter the "
                                  "coordinates it should sit at")
        self._move_btn.clicked.connect(self._start_move_point)
        scale_row.addWidget(self._scale_btn)
        scale_row.addWidget(self._move_btn)
        form.addRow("Calibrate:", scale_row)
        self._hint = QLabel()
        form.addRow("", self._hint)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close)
        layout.addLayout(bottom)

        self._refresh()
        if self._bg.path:
            self._vp.set_background(self._bg)

    # -- state <-> controls ------------------------------------------------

    def _refresh(self):
        bg = self._bg
        self._updating = True
        has = bool(bg.path)
        self._file_label.setText(bg.path.rsplit("/", 1)[-1] if has else "None")
        self._file_label.setToolTip(bg.path)
        self._visible.setChecked(bg.visible)
        self._opacity.setValue(round(bg.opacity * 100))
        self._anchor.setCurrentText(bg.anchor)
        self._x.setValue(bg.x)
        self._y.setValue(bg.y)
        self._width.setValue(bg.width)
        self._rotation.setValue(bg.rotation)
        self._height_label.setText(f"Height: {bg.height:.3f}")
        for w in (self._visible, self._opacity, self._anchor, self._x, self._y, self._width,
                  self._rotation, self._scale_btn, self._move_btn):
            w.setEnabled(has)
        self._updating = False

    def _changed(self, *_):
        if self._updating:
            return
        bg = self._bg
        bg.visible = self._visible.isChecked()
        bg.opacity = self._opacity.value() / 100
        bg.x, bg.y = self._x.value(), self._y.value()
        bg.width = self._width.value()
        bg.rotation = self._rotation.value()
        self._height_label.setText(f"Height: {bg.height:.3f}")
        self._apply()

    def _apply(self):
        _session[self._key] = replace(self._bg)
        self._vp.set_background(self._bg)

    def _anchor_changed(self, name: str):
        """Changing the anchor keeps the image where it is: only which of
        its points (x, y) names moves."""
        if self._updating:
            return
        cx, cy = self._bg.center()
        self._bg.anchor = name
        self._bg.x = self._bg.y = 0.0
        self._bg.place_center(cx, cy)
        self._refresh()
        self._apply()

    # -- actions -----------------------------------------------------------

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Background Image", "", _IMAGE_FILTER)
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            self._hint.setText("Not an image this build can read.")
            return
        first = not self._bg.path
        self._bg.path = path
        self._bg.aspect = img.height() / img.width()
        self._bg.visible = True
        if first:
            bounds = self._vp.shape_bounds()
            if bounds is not None:
                self._bg.fit(bounds)
        self._hint.setText("")
        self._refresh()
        self._apply()

    def _clear(self):
        self._vp.cancel_pick()
        self._bg = BackgroundImage()
        _session.pop(self._key, None)
        self._vp.set_background(None)
        self._hint.setText("")
        self._refresh()

    def _anchor_at_origin(self):
        self._bg.x = self._bg.y = 0.0
        self._refresh()
        self._apply()

    def _center_on_shape(self):
        bounds = self._vp.shape_bounds()
        if bounds is not None:
            self._bg.place_center((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
            self._refresh()
            self._apply()

    def _fit_to_shape(self):
        bounds = self._vp.shape_bounds()
        if bounds is not None:
            self._bg.fit(bounds)
            self._refresh()
            self._apply()

    def _start_two_point_scale(self):
        self._hint.setText("Click the first point on the image, then the second.")
        self._vp.pick_points(2, self._finish_two_point_scale)

    def _finish_two_point_scale(self, pts):
        (ax, ay), (bx, by) = pts
        measured = math.hypot(bx - ax, by - ay)
        self._hint.setText("")
        if measured <= 0:
            return
        real, ok = QInputDialog.getDouble(
            self, "Scale from Two Points",
            f"The points are {measured:.4g} apart now. Real distance:",
            measured, 1e-6, 1e6, 4)
        if ok and real > 0:
            self._bg.scale_about(ax, ay, real / measured)
            self._refresh()
            self._apply()

    def _start_move_point(self):
        self._hint.setText("Click the point on the image to move.")
        self._vp.pick_points(1, self._finish_move_point)

    def _finish_move_point(self, pts):
        (px, py), = pts
        self._hint.setText("")
        text, ok = QInputDialog.getText(self, "Move Point To",
                                        f"Point clicked: {px:.4g}, {py:.4g}\nMove it to (x, y):",
                                        text="0, 0")
        if not ok:
            return
        try:
            tx, ty = (float(v) for v in text.replace("[", "").replace("]", "").split(","))
        except ValueError:
            self._hint.setText("Enter two numbers, x and y.")
            return
        self._bg.x += tx - px
        self._bg.y += ty - py
        self._refresh()
        self._apply()

    def closeEvent(self, event):
        self._vp.cancel_pick()
        self._hint.setText("")
        super().closeEvent(event)


def restore_session_background(key: str, viewport):
    """Show the session's image for `key`, if there is one."""
    bg = _session.get(key)
    if bg is not None:
        viewport.set_background(replace(bg))
