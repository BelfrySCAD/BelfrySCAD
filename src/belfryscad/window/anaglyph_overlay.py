"""The orientation cube and axis indicator in anaglyph stereo.

They are QPainter overlays on top of the GL view, so the renderer's anaglyph
composite never reaches them: drawn plain, the axis indicator's red axis
vanishes behind the red lens and neither shows any depth. In anaglyph mode
each is painted once per eye, turned about the view's up axis by each eye's
toe-in angle (the same IPD / screen distance / depth trim the 3D view uses),
over the viewport background, then combined with the renderer's own
matrices. Its centre sits at the screen; nearer parts come out of it.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtGui import QImage, QPainter

from belfryscad.engine.renderer import anaglyph_matrices


def eye_rotations(camera, rot: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(left, right) eye view rotations for an overlay drawn with `rot`.
    Turning the object by +a about the view's up axis is the camera moving
    left: a near point moves right in the left eye and left in the right,
    so it sits in front of the screen."""
    a = math.atan(camera.viewer_ipd / (2.0 * camera.viewer_screen_dist)) * camera.stereo_depth_scale

    def yaw(t):
        c, s = math.cos(t), math.sin(t)
        return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
    return yaw(a) @ rot, yaw(-a) @ rot


def _render(widget, draw, rot) -> np.ndarray:
    """`draw` with widget._rot = `rot`, as an h x w x 4 premultiplied RGBA
    array in 0..1 at the widget's device pixel ratio."""
    dpr = widget.devicePixelRatioF()
    w, h = round(widget.width() * dpr), round(widget.height() * dpr)
    img = QImage(w, h, QImage.Format.Format_RGBA8888_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(0)
    saved, widget._rot = widget._rot, rot
    try:
        p = QPainter(img)
        draw(p)
        p.end()
    finally:
        widget._rot = saved
    rows = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())
    return rows[:, :w * 4].reshape(h, w, 4) / 255.0


def paint(widget, draw) -> None:
    """Paint `widget` with `draw(painter)`, which reads widget._rot: plainly,
    or as an anaglyph when widget.stereo() returns (camera, bg_rgb)."""
    stereo = widget.stereo() if widget.stereo is not None else None
    if stereo is None:
        p = QPainter(widget)
        draw(p)
        p.end()
        return
    camera, bg = stereo
    bg = np.asarray(bg[:3], dtype=np.float64)
    left_rot, right_rot = eye_rotations(camera, widget._rot)
    # Each eye over the background, so where only one eye has the overlay
    # the other sees what the 3D view's composite shows there.
    eyes = [_render(widget, draw, r) for r in (left_rot, right_rot)]
    rgb_l, rgb_r = (e[..., :3] + bg * (1.0 - e[..., 3:]) for e in eyes)
    m_l, m_r = anaglyph_matrices(camera)
    out = np.clip(rgb_l @ np.asarray(m_l).T + rgb_r @ np.asarray(m_r).T, 0.0, 1.0)
    alpha = np.maximum(eyes[0][..., 3:], eyes[1][..., 3:])
    rgba = np.ascontiguousarray((np.concatenate([out * alpha, alpha], axis=2) * 255).astype(np.uint8))
    h, w = rgba.shape[:2]
    img = QImage(rgba.data, w, h, 4 * w, QImage.Format.Format_RGBA8888_Premultiplied)
    img.setDevicePixelRatio(widget.devicePixelRatioF())
    p = QPainter(widget)
    p.drawImage(0, 0, img)
    p.end()
    # Hit-testing state (the cube's _drawn) from the centre view, not an eye.
    _render(widget, draw, widget._rot)
