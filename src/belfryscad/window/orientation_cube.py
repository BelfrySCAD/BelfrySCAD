"""Orientation cube: a small beveled cube in the viewport's top-right corner
that mirrors the camera's orientation and snaps it to a named view when
clicked.

Deliberately a plain QWidget painted with QPainter rather than geometry
rendered into the GL scene. The cube is a fixed-size 2D overlay whose faces
are flat colours with text on them, and everything it needs -- projecting 26
polygons, drawing labels in the plane of a face, hit-testing a click -- is
easier and more exact in QPainter than in GL:

  * face labels are drawText, not a texture atlas with one texture per face;
  * clicking is point-in-polygon against the very polygons that were drawn,
    rather than a colour-picking readback or a ray cast against bevel
    geometry;
  * nothing touches the ModernGL context, so there is no GL state to save,
    restore, or get wrong (see the HiDPI sub-viewport comment in
    renderer.paint for how easy that is to trip over).

The projection is orthographic, which is what makes the QPainter approach
work as well as it does: under an orthographic projection a square face
always projects to a parallelogram, so a single affine QTransform maps a
label's text box onto the face exactly.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QWidget


# Which world axis direction each label belongs to. These are the same six
# directions the View menu's named presets use (viewport.VIEW_PRESETS): the
# camera sits along +X for "right", which is the face labelled X+, along -Y
# for "front", which is Y-, and so on.
_FACE_LABELS = {
    (1, 0, 0): "X+",
    (-1, 0, 0): "X-",
    (0, 1, 0): "Y+",
    (0, -1, 0): "Y-",
    (0, 0, 1): "Z+",
    (0, 0, -1): "Z-",
}

# The three positive-axis faces are tinted with the same red/green/blue the
# scene's own X/Y/Z axes use (renderer._render_axes), lightened towards the
# neutral face grey so a label still reads against them. The negative faces
# stay neutral, matching how the axes themselves only colour their positive
# halves and leave the negative ones grey.
_AXIS_RGB = {
    (1, 0, 0): (0.85, 0.15, 0.15),
    (0, 1, 0): (0.15, 0.65, 0.15),
    # Nearer pure blue than the other two axes are to theirs, deliberately.
    # The neutral face grey (150, 155, 165) is itself blue-leaning, so mixing
    # blue into it moves the colour less far than red or green do -- the old
    # (0.25, 0.35, 0.90) left Z sitting 59.6 from neutral where X sat 82.1
    # and Y 74.1, visibly the faintest of the three.
    #
    # This lands at 92.4, past X rather than level with it. That overshoot is
    # the point: the eye is least sensitive to blue (0.07 of luminance
    # against green's 0.72), so matching the other axes by distance still
    # reads weaker than they do. Rendered the alternatives to pick it.
    #
    # Done by changing THIS axis rather than raising _FACE_TINT_MIX, which is
    # shared: a mix high enough to fix Z turns X and Y into the saturated
    # stickers that constant's own note warns about.
    (0, 0, 1): (0.05, 0.20, 1.00),
}
_NEUTRAL_FACE = (150, 155, 165)

# How far a positive face is pulled from the neutral grey towards its axis
# colour. Enough to say "this is X" at 92px; much more and the cube reads as
# three saturated stickers rather than a grey cube with a hint of axis in it.
_FACE_TINT_MIX = 0.28


def _face_color(normal: np.ndarray) -> QColor:
    key = tuple(int(round(float(c))) for c in normal)
    rgb = _AXIS_RGB.get(key)
    if rgb is None:
        return QColor(*_NEUTRAL_FACE)
    m = _FACE_TINT_MIX
    return QColor(*[
        round(n * (1 - m) + (c * 255) * m)
        for n, c in zip(_NEUTRAL_FACE, rgb)
    ])


class _Region:
    """One clickable patch of the cube: a face, an edge bevel or a corner
    bevel. `normal` doubles as the direction the camera moves to when this
    patch is clicked -- clicking a patch turns it to face the viewer, which
    is exactly "put the eye on this patch's outward normal"."""

    __slots__ = ("points", "normal", "label")

    def __init__(self, points: np.ndarray, normal: np.ndarray, label: str | None):
        self.points = points          # (N, 3) model-space, in polygon order
        self.normal = normal          # (3,) unit outward normal
        self.label = label            # face name, or None for a bevel


def _build_regions(bevel: float) -> list[_Region]:
    """A chamfered cube: 6 face squares, 12 edge quads, 8 corner triangles.

    `bevel` is how much of each half-edge the chamfer eats, so the face
    squares span +-s where s = 1 - bevel and the full cube still spans +-1.
    """
    s = 1.0 - bevel
    regions: list[_Region] = []

    def unit(v):
        v = np.asarray(v, dtype=np.float64)
        return v / np.linalg.norm(v)

    def point(axis_vals: dict[int, float]) -> list[float]:
        p = [0.0, 0.0, 0.0]
        for axis, val in axis_vals.items():
            p[axis] = val
        return p

    # Faces: the square at +-1 on one axis, +-s on the other two, walked
    # around the square rather than in raster order so the polygon is simple.
    for a in range(3):
        u, v = [x for x in range(3) if x != a]
        for sa in (1, -1):
            corners = [(+s, +s), (-s, +s), (-s, -s), (+s, -s)]
            pts = [point({a: sa * 1.0, u: cu, v: cv}) for cu, cv in corners]
            n = [0, 0, 0]
            n[a] = sa
            regions.append(_Region(np.array(pts), unit(n), _FACE_LABELS[tuple(n)]))

    # Edge bevels: the quad bridging the two faces that meet at that edge.
    for a in range(3):
        for b in range(a + 1, 3):
            c = 3 - a - b
            for sa in (1, -1):
                for sb in (1, -1):
                    pts = [
                        point({a: sa * 1.0, b: sb * s, c: +s}),
                        point({a: sa * 1.0, b: sb * s, c: -s}),
                        point({a: sa * s, b: sb * 1.0, c: -s}),
                        point({a: sa * s, b: sb * 1.0, c: +s}),
                    ]
                    n = [0, 0, 0]
                    n[a] = sa
                    n[b] = sb
                    regions.append(_Region(np.array(pts), unit(n), None))

    # Corner bevels: the triangle capping each corner.
    for sx in (1, -1):
        for sy in (1, -1):
            for sz in (1, -1):
                pts = [
                    [sx * 1.0, sy * s, sz * s],
                    [sx * s, sy * 1.0, sz * s],
                    [sx * s, sy * s, sz * 1.0],
                ]
                regions.append(_Region(np.array(pts), unit([sx, sy, sz]), None))

    return regions


def azimuth_elevation_for(normal: np.ndarray) -> tuple[float, float]:
    """The camera azimuth/elevation that looks straight down `normal`.

    Straight up and straight down are special-cased to the same values
    set_view_preset uses: at exactly +-90 elevation the look-at basis hits
    gimbal lock and falls back to a hardcoded +X right vector, so the presets
    stop a ten-thousandth of a degree short and pin a matching azimuth. Doing
    anything else here would make the cube's Top disagree with Ctrl+4's.
    """
    nx, ny, nz = (float(c) for c in normal)
    if abs(nz) > 0.9999:
        return (270.0, 89.9999) if nz > 0 else (0.0, -89.9999)
    # Normalized into [0, 360) so a face's answer is the identical number the
    # View menu's own preset table holds -- atan2 would hand back -90 for
    # FRONT where the table says 270. The two are the same direction either
    # way (and animate_camera_to takes the short arc regardless), but keeping
    # them literally equal means one can be checked against the other.
    az = math.degrees(math.atan2(ny, nx)) % 360.0
    return az, math.degrees(math.asin(nz))


class OrientationCube(QWidget):
    """Fixed-size overlay. Parent it to a Viewport, keep `set_orientation`
    fed from that viewport's camera, and connect `view_requested`."""

    view_requested = Signal(float, float)   # azimuth, elevation (degrees)
    orbit_requested = Signal(float, float)  # drag delta in pixels (dx, dy)

    SIZE = 92          # widget is square, in logical pixels
    MARGIN = 10        # gap from the viewport's top-right corner
    BEVEL = 0.16       # fraction of each half-edge taken by the chamfer

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setToolTip("Click a face, edge or corner to look from that direction")

        self._regions = _build_regions(self.BEVEL)
        self._rot = np.eye(3)      # camera view rotation (world -> view)
        self._hover = -1           # index into _regions, or -1
        # Rebuilt every paint, reused by hit-testing so a click can only ever
        # match something actually visible on screen.
        # (index, drawn polygon, hit polygon). The two differ for the
        # bevels: see _grow.
        self._drawn: list[tuple[int, QPolygonF, QPolygonF]] = []
        # Press/drag state. A press alone commits to nothing: it becomes a
        # snap-to-view on release, or an orbit once it has moved far enough
        # to be a drag rather than a shaky click.
        self._press_region = -1
        self._press_pos = None
        self._drag_pos = None
        self._dragging = False

    # -- state ---------------------------------------------------------

    def set_orientation(self, view_matrix: np.ndarray) -> None:
        """Feed the camera's 4x4 view matrix; only its rotation is used."""
        rot = np.asarray(view_matrix, dtype=np.float64)[:3, :3]
        if not np.allclose(rot, self._rot):
            self._rot = rot
            self.update()

    def place_in(self, parent_width: int) -> None:
        """Park in the parent's top-right corner."""
        self.move(max(0, parent_width - self.SIZE - self.MARGIN), self.MARGIN)
        self.raise_()   # above the other viewport overlay children

    # -- projection ----------------------------------------------------

    def _project(self, pts: np.ndarray) -> np.ndarray:
        """Model space -> (x_screen, y_screen, depth). Orthographic: the view
        rotation alone, scaled to the widget. Larger depth is nearer."""
        v = pts @ self._rot.T
        half = self.SIZE / 2.0
        # sqrt(3) is the cube's corner-to-centre distance, so the whole cube
        # fits at any orientation with a little room for the outline.
        scale = (half - 6) / math.sqrt(3.0)
        out = np.empty_like(v)
        out[:, 0] = half + v[:, 0] * scale
        out[:, 1] = half - v[:, 1] * scale      # Qt's y grows downward
        out[:, 2] = v[:, 2]
        return out

    def _visible(self) -> list[tuple[float, int, np.ndarray]]:
        """(depth, region index, projected points) for every front-facing
        region, farthest first so painting in order resolves overlaps."""
        out = []
        for i, reg in enumerate(self._regions):
            # View-space z grows toward the eye (the view matrix's third row
            # is -forward), so a positive z-component means outward-facing.
            facing = float((self._rot @ reg.normal)[2])
            if facing <= 1e-6:
                continue
            proj = self._project(reg.points)
            out.append((float(proj[:, 2].mean()), i, proj))
        out.sort(key=lambda t: t[0])
        return out

    # -- painting ------------------------------------------------------

    @staticmethod
    def _grow(poly: QPolygonF, factor: float) -> QPolygonF:
        """`poly` scaled about its own centroid."""
        if factor == 1.0:
            return poly
        n = poly.count()
        cx = sum(poly.at(i).x() for i in range(n)) / n
        cy = sum(poly.at(i).y() for i in range(n)) / n
        return QPolygonF([
            QPointF(cx + (poly.at(i).x() - cx) * factor,
                    cy + (poly.at(i).y() - cy) * factor)
            for i in range(n)
        ])

    def _hit_grow(self, region: _Region) -> float:
        if region.label is not None:
            return 1.0                       # a face needs no help
        return (self.CORNER_HIT_GROW if len(region.points) == 3
                else self.EDGE_HIT_GROW)     # 3 points = corner, 4 = edge

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        bevel_col = QColor(120, 125, 135)
        hover_col = QColor(90, 150, 235)
        # Outlines every region, so its weight sets how the whole cube reads.
        # It was (45, 48, 55): 4.74:1 against a face, more than three times
        # the 1.48:1 step between a face and a bevel that it is there to
        # delineate -- so the cube read as a black wire cage with grey fill
        # rather than as a shaded solid. At (95, 100, 110) it is 2.13:1 and
        # 1.44:1, about as strong as the shading step it accompanies.
        #
        # Not lighter than this: the bevels are (120, 125, 135), and by
        # (125, 130, 140) the outline is 1.07:1 against them and the
        # chamfers stop being visible at all.
        edge_pen = QPen(QColor(95, 100, 110), 1.0)

        self._drawn = []
        for _depth, idx, proj in self._visible():
            reg = self._regions[idx]
            poly = QPolygonF([QPointF(p[0], p[1]) for p in proj])
            self._drawn.append((idx, poly, self._grow(poly, self._hit_grow(reg))))

            painter.setPen(edge_pen)
            painter.setBrush(hover_col if idx == self._hover
                             else (_face_color(reg.normal) if reg.label else bevel_col))
            painter.drawPolygon(poly)

            if reg.label:
                self._draw_face_label(painter, reg.label, proj, idx == self._hover)

        painter.end()

    @staticmethod
    def _label_frame(proj: np.ndarray):
        """Pick the (origin, x-edge, y-edge) frame to lay a face's label in,
        or None if the face is edge-on and has no usable frame.

        A square under an orthographic projection is a parallelogram, so one
        affine transform maps a text box onto it exactly -- but only some of
        the ways round the quad give text that reads forwards and sits
        upright. Both directions around the quad are tried, not just the four
        rotations of the stored order: the stored order winds outward for
        three of the six faces and inward for the other three (the (axis, u,
        v) triple flips handedness with the face's sign), so filtering on
        det > 0 alone silently dropped the labels on TOP, RIGHT and FRONT.

        Among the candidates that read forwards, take the one whose "down"
        edge points most nearly down the screen, so labels stay upright-ish
        rather than sideways.
        """
        best = None
        for order in (proj, proj[::-1]):
            for r in range(4):
                o = order[r][:2]
                x = order[(r + 1) % 4][:2] - o
                y = order[(r + 3) % 4][:2] - o
                det = x[0] * y[1] - x[1] * y[0]
                if det <= 1e-9:       # mirrored, or degenerate edge-on
                    continue
                if best is None or y[1] > best[3]:
                    best = (o, x, y, y[1])
        return None if best is None else best[:3]

    def _draw_face_label(self, painter: QPainter, text: str, proj: np.ndarray,
                          hovered: bool) -> None:
        """Draw the name in the plane of the face."""
        frame = self._label_frame(proj)
        if frame is None:
            return
        o, x, y = frame

        box = 100.0
        painter.save()
        painter.setTransform(QTransform(x[0] / box, x[1] / box,
                                        y[0] / box, y[1] / box,
                                        o[0], o[1]))
        font = QFont()
        # 48 in the label's own 100-unit box, which the transform then scales
        # to the face. Twice what the old word labels used, which they could
        # not have taken -- "BOTTOM" barely fitted at half this. Two-character
        # axis names leave the room.
        font.setPixelSize(48)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255) if hovered else QColor(35, 38, 44))
        painter.drawText(QRectF(0, 0, box, box), Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    # -- interaction ---------------------------------------------------

    def _region_at(self, pos) -> int:
        """Which patch is under `pos`, or -1.

        Smallest target first, because the hit areas are grown past the
        shapes actually drawn and therefore overlap: corners, then edges,
        then everything at its true size. The order is the whole point --
        an edge bevel grown by a third reaches right over the corner
        triangles at either end of it, so letting edges and corners compete
        on depth alone meant a click aimed dead centre at a corner selected
        the neighbouring edge instead. That made corners HARDER to hit than
        with no growing at all.
        """
        pt = QPointF(pos)

        def scan(want_corner: bool | None, grown: bool) -> int:
            # Nearest first: _drawn is farthest-first, so walk it backwards.
            for idx, poly, hit in reversed(self._drawn):
                reg = self._regions[idx]
                if want_corner is not None:
                    if reg.label is not None or (len(reg.points) == 3) != want_corner:
                        continue
                if (hit if grown else poly).containsPoint(pt, Qt.FillRule.OddEvenFill):
                    return idx
            return -1

        for want_corner, grown in ((True, True), (False, True), (None, False)):
            idx = scan(want_corner, grown)
            if idx >= 0:
                return idx
        return -1

    DRAG_THRESHOLD = 3   # pixels of travel before a press counts as a drag

    # The bevels are deliberately thin slivers, which makes them accurate to
    # look at and fiddly to hit -- a corner triangle is only a few pixels
    # across at BEVEL=0.16. Their hit areas are grown about their own centres
    # so they can be clicked at the size the eye expects, while what gets
    # painted stays exactly the shape of the cube. Corners get the most,
    # being the smallest; faces get none, being enormous already.
    CORNER_HIT_GROW = 2.6
    EDGE_HIT_GROW = 1.35

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._press_pos is not None:
            if not self._dragging:
                moved = (abs(pos.x() - self._press_pos.x())
                         + abs(pos.y() - self._press_pos.y()))
                if moved < self.DRAG_THRESHOLD:
                    return
                self._dragging = True
                if self._hover != -1:
                    self._hover = -1     # no highlight while spinning
                    self.update()
            self.orbit_requested.emit(pos.x() - self._drag_pos.x(),
                                       pos.y() - self._drag_pos.y())
            self._drag_pos = pos
            return
        idx = self._region_at(pos)
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, event):
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        idx = self._region_at(event.position())
        if idx < 0:
            # A press in a corner of the widget that isn't on the cube should
            # fall through to the viewport rather than being swallowed.
            event.ignore()
            return
        # Nothing happens yet: which of the two gestures this is only becomes
        # clear once the pointer either moves (orbit) or lets go (snap).
        self._press_region = idx
        self._press_pos = event.position()
        self._drag_pos = event.position()
        self._dragging = False
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            event.ignore()
            return
        was_drag, idx = self._dragging, self._press_region
        self._press_region = -1
        self._press_pos = None
        self._drag_pos = None
        self._dragging = False
        if not was_drag and idx >= 0:
            az, el = azimuth_elevation_for(self._regions[idx].normal)
            self.view_requested.emit(az, el)
        # A drag leaves the pointer somewhere new; re-evaluate the highlight.
        new_hover = self._region_at(event.position())
        if new_hover != self._hover:
            self._hover = new_hover
            self.update()
        event.accept()
