"""Holds the main viewport at a fixed aspect ratio (View ▸ Aspect Ratio).

The viewport itself is resized to the ratio and centred, with bars in the
space left over, rather than drawn letterboxed inside its own widget. So
picking, the camera's projection, the size label and every capture
(Save Image, Copy Image, animation frames) see a viewport of exactly that
shape, with no change to any of them.
"""

from PySide6.QtWidgets import QWidget

# (menu label, width / height); None is the default, filling the space.
RATIOS = [("&Fill Window", None), ("&1:1", 1.0), ("&4:3", 4 / 3), ("&3:2", 3 / 2),
          ("16:1&0", 16 / 10), ("16:&9", 16 / 9), ("&2:1", 2.0)]


def fit(w: int, h: int, ratio: float | None) -> tuple[int, int, int, int]:
    """(x, y, w, h): the largest rectangle of `ratio` centred in w x h."""
    if not ratio or w <= 0 or h <= 0:
        return 0, 0, w, h
    fw, fh = (w, round(w / ratio)) if w / h <= ratio else (round(h * ratio), h)
    return (w - fw) // 2, (h - fh) // 2, fw, fh


class AspectFrame(QWidget):
    def __init__(self, child: QWidget, parent=None):
        super().__init__(parent)
        self._child = child
        self._ratio = None
        child.setParent(self)
        self.setMinimumSize(child.minimumSize())
        child.setMinimumSize(1, 1)

    def ratio(self) -> float | None:
        return self._ratio

    def set_ratio(self, ratio: float | None):
        self._ratio = ratio
        self._place()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def _place(self):
        self._child.setGeometry(*fit(self.width(), self.height(), self._ratio))
