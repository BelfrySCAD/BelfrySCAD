"""Axis indicator: OpenSCAD's small reference axes in the viewport's
lower-left corner (#881) -- X, Y and Z in red, green and blue, turning with
the camera, each tipped with a stroked letter as OpenSCAD's
`GLView::showSmallaxes` draws them.

A plain QWidget painted with QPainter, for the reasons the orientation cube
gives (orientation_cube.py): a fixed-size 2D overlay needs nothing from the
GL context. Unlike the cube it takes no clicks; they pass through to the
viewport.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

# The scene's own axis colours (renderer._render_axes).
_AXIS_RGB = ((0.85, 0.15, 0.15), (0.15, 0.65, 0.15), (0.25, 0.35, 0.9))

# OpenSCAD's letters, as line segments in a box of half-width 1 centred on
# the label point, y up: X is a cross, Y a cross whose lower right arm
# stops at the centre, Z top bar, bottom bar and the diagonal joining them.
_GLYPHS = (
    (((-1, -1), (1, 1)), ((-1, 1), (1, -1))),
    (((-1, -1), (1, 1)), ((-1, 1), (0, 0))),
    (((-1, -1), (1, -1)), ((-1, 1), (1, 1)), ((-1, -1), (1, 1))),
)


class AxisIndicator(QWidget):
    """Fixed-size, click-through overlay. Parent it to a Viewport and keep
    `set_orientation` fed from its camera."""

    SIZE = 84        # square, logical pixels
    MARGIN = 6       # gap to the viewport's left and bottom edges
    LENGTH = 30      # axis length from the centre, logical pixels
    LABEL_AT = 1.2   # where a letter sits, in axis lengths (OpenSCAD: 12/10)
    GLYPH = 3.5      # half-width of a letter (OpenSCAD: 3 px)

    def __init__(self, parent=None, label_color=lambda: (0.2, 0.2, 0.2)):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._rot = np.eye(3)
        # The colour scheme's axes colour, read at paint time so a theme
        # change needs no hook here.
        self._label_color = label_color

    def set_orientation(self, view_matrix: np.ndarray) -> None:
        """Feed the camera's 4x4 view matrix; only its rotation is used."""
        rot = np.asarray(view_matrix, dtype=np.float64)[:3, :3]
        if not np.allclose(rot, self._rot):
            self._rot = rot
            self.update()

    def place_in(self, parent_height: int) -> None:
        """Park in the parent's lower-left corner."""
        self.move(self.MARGIN, max(0, parent_height - self.SIZE - self.MARGIN))
        self.raise_()

    def screen_axes(self) -> list[tuple[int, float, float, float]]:
        """(axis, x, y, depth) of each axis tip relative to the centre, in
        logical pixels with y down, farthest first. Orthographic: the view
        rotation alone, as OpenSCAD's corner axes are."""
        tips = np.eye(3) @ self._rot.T
        out = [(i, tips[i, 0] * self.LENGTH, -tips[i, 1] * self.LENGTH, tips[i, 2])
               for i in range(3)]
        return sorted(out, key=lambda t: t[3])

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.SIZE / 2, self.SIZE / 2)
        label = QColor.fromRgbF(*self._label_color()[:3])
        for i, x, y, _ in self.screen_axes():
            p.setPen(QPen(QColor.fromRgbF(*_AXIS_RGB[i]), 2.0,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(c, c + QPointF(x, y))
            at = c + QPointF(x, y) * self.LABEL_AT
            p.setPen(QPen(label, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            g = self.GLYPH
            for (x0, y0), (x1, y1) in _GLYPHS[i]:
                p.drawLine(at + QPointF(x0 * g, -y0 * g), at + QPointF(x1 * g, -y1 * g))
        p.end()
