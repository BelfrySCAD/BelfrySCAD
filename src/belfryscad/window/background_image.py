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
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout)

#: The anchor's choices: which point of the image sits at (x, y), as a
#: fraction of its width (from the left) and height (from the bottom).
ANCHORS = {
    "Top Left": (0.0, 1.0), "Top": (0.5, 1.0), "Top Right": (1.0, 1.0),
    "Left": (0.0, 0.5), "Center": (0.5, 0.5), "Right": (1.0, 0.5),
    "Bottom Left": (0.0, 0.0), "Bottom": (0.5, 0.0), "Bottom Right": (1.0, 0.0),
}


def anchor_name(anchor) -> str:
    return next(n for n, a in ANCHORS.items() if a == tuple(anchor))

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
    anchor: tuple[float, float] = (0.5, 0.5)
    rotation: float = 0.0      # degrees, counter-clockwise
    opacity: float = 0.5       # ponytail: fixed; a control for it was more UI than it earned
    visible: bool = True

    @property
    def height(self) -> float:
        return self.width * self.aspect

    def _rot(self):
        a = math.radians(self.rotation)
        return math.cos(a), math.sin(a)

    def center(self) -> tuple[float, float]:
        """The image's centre in model coordinates."""
        fx, fy = self.anchor
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
    background_key = ""          # its _session key
    background_listener = None   # called after the image is moved or scaled here
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

    def background_edited(self):
        if self.background is not None and self.background_key:
            _session[self.background_key] = replace(self.background)
        if self.background_listener is not None:
            self.background_listener()
        self.update()

    def _model_point(self, pos) -> tuple[float, float] | None:
        o, d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
        if abs(d[2]) < 1e-9:
            return None
        t = -o[2] / d[2]
        return float(o[0] + t * d[0]), float(o[1] + t * d[1])

    def _image_point(self, pos) -> tuple[float, float] | None:
        """The model point under `pos` if it is on the shown image."""
        bg = self.background
        if bg is None or not bg.visible or not bg.path:
            return None
        pt = self._model_point(pos)
        if pt is None:
            return None
        cx, cy = bg.center()
        c, s = bg._rot()
        dx, dy = pt[0] - cx, pt[1] - cy
        if abs(c * dx + s * dy) <= bg.width / 2 and abs(c * dy - s * dx) <= bg.height / 2:
            return pt
        return None

    def _exec_menu(self, menu, event):
        """Show a context menu, with the image's own actions added when the
        click is on it; nothing at all if that leaves the menu empty."""
        bg = self.background
        if bg is not None and bg.path:
            if not menu.isEmpty():
                menu.addSeparator()
            pt = self._image_point(event.pos())
            if pt is not None:
                menu.addAction("Move This Point To…", lambda: self._move_image_point(pt))
                menu.addAction("Scale from This Point…", lambda: self._scale_image_from(pt))
            # On every right-click, not only on the image: they are how to
            # get back an image moved out of view.
            anchors = menu.addMenu("Image Anchor")
            for i, (name, a) in enumerate(ANCHORS.items()):
                if i and i % 3 == 0:
                    anchors.addSeparator()
                act = anchors.addAction(name, lambda a=a: self._set_anchor(a))
                act.setCheckable(True)
                act.setChecked(a == tuple(bg.anchor))
            align = menu.addMenu("Align Image")
            align.addAction("Anchor to Origin", self._anchor_to_origin)
            on_shape = align.addAction(f"Anchor to Shape's {anchor_name(bg.anchor)}",
                                       self._anchor_to_shape)
            align.addSeparator()
            fit = align.addAction("Fit to Shape", self._fit_to_shape)
            for act in (on_shape, fit):
                act.setEnabled(self.shape_bounds() is not None)
            menu.addSeparator()
            menu.addAction("Show Background Image" if not bg.visible else "Hide Background Image",
                           self._toggle_background)
            menu.addAction("Clear Background Image", self.clear_background)
        if menu.isEmpty():
            return
        # On macOS, contextMenuEvent fires on the right-button *press*,
        # not after release (unlike a plain click) -- so this reset
        # must happen only once we're sure a menu is actually about to
        # show, right before QMenu.exec(), here. Resetting unconditionally at
        # the top of contextMenuEvent (an earlier version of this fix) reset
        # _last_mouse/_mouse_button on every right-click including
        # blank-space ones that show no menu at all, which broke
        # ordinary right-drag panning outright. Qt still doesn't
        # reliably deliver this widget's own mouseReleaseEvent for a
        # click that *does* open a menu (the popup's own event loop can
        # swallow it), so the reset is still needed here -- just scoped
        # to only the paths that actually call exec().
        self._last_mouse = None
        self._mouse_button = None
        menu.exec(event.globalPos())

    def _toggle_background(self):
        self.background.visible = not self.background.visible
        self.background_edited()

    def clear_background(self):
        self.cancel_pick()
        self.background = None
        _session.pop(self.background_key, None)
        self.background_edited()

    def _set_anchor(self, anchor):
        """Make another point of the image the anchor; the image moves so
        that point sits at (x, y)."""
        self.background.anchor = anchor
        self.background_edited()

    def _anchor_to_origin(self):
        self.background.x = self.background.y = 0.0
        self.background_edited()

    def _anchor_to_shape(self):
        """Put the anchor on the same point of the shape's bounding box:
        Top Left on its top-left corner, Center on its centre."""
        x0, y0, x1, y1 = self.shape_bounds()
        fx, fy = self.background.anchor
        self.background.x, self.background.y = x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)
        self.background_edited()

    def _fit_to_shape(self):
        self.background.fit(self.shape_bounds())
        self.background_edited()

    def _move_image_point(self, pt):
        px, py = pt
        text, ok = QInputDialog.getText(self, "Move Point To",
                                        f"Point clicked: {px:.4g}, {py:.4g}\nMove it to (x, y):",
                                        text="0, 0")
        if not ok:
            return
        try:
            tx, ty = (float(v) for v in text.replace("[", "").replace("]", "").split(","))
        except ValueError:
            return
        self.background.x += tx - px
        self.background.y += ty - py
        self.background_edited()

    def _scale_image_from(self, a):
        self._show_delta("Click a second point on the image (Esc cancels)")
        self.pick_points(1, lambda pts: self._finish_scale(a, pts[0]))

    def _finish_scale(self, a, b):
        measured = math.hypot(b[0] - a[0], b[1] - a[1])
        if measured <= 0:
            return
        real, ok = QInputDialog.getDouble(
            self, "Scale from Two Points",
            f"The points are {measured:.4g} apart now. Real distance:",
            measured, 1e-6, 1e6, 4)
        if ok and real > 0:
            self.background.scale_about(a[0], a[1], real / measured)
            self.background_edited()

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
        self._delta_label.hide()

    def keyPressEvent(self, event):
        if self._bg_pick is not None and event.key() == Qt.Key.Key_Escape:
            self.cancel_pick()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if (self._bg_pick is not None and event.button() == Qt.MouseButton.LeftButton
                and not event.modifiers()):
            pt = self._model_point(event.position())
            if pt is not None:
                n, got, callback = self._bg_pick
                got.append(pt)
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
        # The viewport's own image, so its right-click tools and this
        # panel edit one object.
        self._bg = viewport.background or BackgroundImage()
        viewport.background_key = key
        viewport.background_listener = self._sync
        self._updating = False

        form = QFormLayout()
        file_row = QHBoxLayout()
        self._file_label = QLabel()
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose)
        file_row.addWidget(self._file_label, 1)
        file_row.addWidget(choose)
        form.addRow("Image:", file_row)

        def spin(lo, hi, decimals=3, suffix=""):
            sb = QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(decimals)
            sb.setSuffix(suffix)
            sb.setKeyboardTracking(False)
            sb.valueChanged.connect(self._changed)
            return sb

        # Which point of the image is the anchor is chosen from the image's
        # right-click menu; here is where it sits.
        self._anchor_label = QLabel()
        self._x, self._y = spin(-1e6, 1e6), spin(-1e6, 1e6)
        anchor_row = QHBoxLayout()
        anchor_row.addWidget(self._anchor_label)
        anchor_row.addSpacing(12)
        anchor_row.addWidget(QLabel("at X"))
        anchor_row.addWidget(self._x, 1)
        anchor_row.addWidget(QLabel("Y"))
        anchor_row.addWidget(self._y, 1)
        form.addRow("Anchor:", anchor_row)

        self._width, self._height = spin(1e-6, 1e6), spin(1e-6, 1e6)
        self._rotation = spin(-360.0, 360.0, 2, "°")
        size_row = QHBoxLayout()
        size_row.addWidget(self._width, 1)
        size_row.addWidget(QLabel("Height"))
        size_row.addWidget(self._height, 1)
        size_row.addSpacing(12)
        size_row.addWidget(QLabel("Rotation"))
        size_row.addWidget(self._rotation, 1)
        form.addRow("Width:", size_row)

        self._hint = QLabel("Right-click the image to change its anchor, align it, move "
                            "a point of it to given coordinates, scale it from two "
                            "points a known distance apart, hide it or clear it.")
        self._hint.setWordWrap(True)
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
        self._anchor_label.setText(anchor_name(bg.anchor))
        self._x.setValue(bg.x)
        self._y.setValue(bg.y)
        self._width.setValue(bg.width)
        self._height.setValue(bg.height)
        self._rotation.setValue(bg.rotation)
        for w in (self._x, self._y,
                  self._width, self._height, self._rotation):
            w.setEnabled(has)
        self._updating = False

    def _changed(self, *_):
        if self._updating:
            return
        bg = self._bg
        bg.x, bg.y = self._x.value(), self._y.value()
        bg.rotation = self._rotation.value()
        # Width and height are one size: whichever was edited wins.
        if self.sender() is self._height:
            bg.width = self._height.value() / bg.aspect
        else:
            bg.width = self._width.value()
        self._refresh()
        self._apply()

    def _apply(self):
        self._vp.set_background(self._bg)
        self._vp.background_edited()

    # -- actions -----------------------------------------------------------

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Background Image", "", _IMAGE_FILTER)
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "Background Image", "Not an image this build can read.")
            return
        first = not self._bg.path
        self._bg.path = path
        self._bg.aspect = img.height() / img.width()
        self._bg.visible = True
        if first:
            bounds = self._vp.shape_bounds()
            if bounds is not None:
                self._bg.fit(bounds)
        self._refresh()
        self._apply()

    def _sync(self):
        """The viewport changed or cleared the image (its right-click menu)."""
        self._bg = self._vp.background or BackgroundImage()
        self._refresh()

    def closeEvent(self, event):
        self._vp.cancel_pick()
        super().closeEvent(event)


def restore_session_background(key: str, viewport):
    """Show the session's image for `key`, if there is one."""
    viewport.background_key = key
    bg = _session.get(key)
    if bg is not None:
        viewport.set_background(replace(bg))
