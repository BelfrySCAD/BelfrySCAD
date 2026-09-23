"""HeightfieldViewer: a rectangular array of heights, as BOSL2's
heightfield() and texture= take it, with import from an image."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QDialogButtonBox, QDoubleSpinBox,
                               QSpinBox, QFormLayout, QHeaderView, QAbstractItemView,
                               QCheckBox, QMenu, QLabel, QPushButton, QSplitter,
                               QWidget, QComboBox, QFileDialog, QMessageBox)
from PySide6.QtCore import Qt, Signal, QItemSelectionModel
from PySide6.QtGui import QFont

from belfryscad.window.ui_colors import apply_themed_icon
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin, _is_list,
                                                  _parse_number,
                                                  _size_combo_to_widest_item,
                                                  _style_table_headers,
                                                  _sync_viewport_to_main_window)
from belfryscad.window.data_viewer_grid import _GridViewport


#: Same directory the toolbar and debugger icons come from.
_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"


#: How BOSL2's `vnf_vertex_array` may split each quad into triangles --
#: its own documented list, which is what a texture or a heightfield is
#: built with. "max_edge" and "quad" exist in BOSL2's source but are not in
#: that list, and "random" is deliberately absent here: a preview that
#: re-triangulates differently on every redraw is not a preview.
QUAD_STYLES = ["default", "alt", "flip1", "flip2", "min_edge", "min_area",
               "quincunx", "convex", "concave"]


def _format_heightfield(value: list, indent: str = "    ") -> str:
    """A heightfield as multi-line OpenSCAD source, one row per line.

    `_format_value` puts everything on one line, which is fine for a 4x4
    matrix and unreadable for a 30x40 field -- the whole point of the array
    is that its shape on the page matches the surface, and a single line
    throws that away. Columns are right-aligned to a common width for the
    same reason.

    Three decimals, fixed: a heightfield is a field of proportions rather
    than measurements, and a column of them stays readable at a common
    width. One more digit than the table shows, so a value nudged to
    something the display rounds is not silently flattened on the way to
    the file. This is the one writeback that does NOT use `_format_value`'s
    `%g` -- and it does round the saved value, not just its display.
    """
    cells = [[f"{float(h):.3f}" for h in row] for row in value]
    width = max((len(t) for row in cells for t in row), default=1)
    rows = [indent + "[" + ", ".join(t.rjust(width) for t in row) + "]"
            for row in cells]
    return "[\n" + ",\n".join(rows) + "\n]"


def _image_luminance_grid(image, rows: int, cols: int) -> list:
    """`image` sampled down to a `rows` x `cols` grid of 0..1 luminance.

    Qt does both hard parts: Format_Grayscale8 applies a proper luma
    weighting rather than averaging the channels (which would read a
    saturated blue as bright as a saturated green), and a smooth scale
    AREA-AVERAGES on the way down. Point-sampling a photo instead would
    alias badly -- a 3000px image sampled at 50 columns would take one
    pixel in sixty and miss whatever lies between.

    Row 0 is the image's TOP row, which is also the first row of the
    heightfield -- the texture convention -- and `_as_grid` places that at
    the far edge of the surface, so the field reads the same way up as the
    picture it came from.
    """
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QImage

    small = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        cols, rows, _Qt.AspectRatioMode.IgnoreAspectRatio,
        _Qt.TransformationMode.SmoothTransformation)
    return [[small.pixelColor(c, r).value() / 255.0 for c in range(cols)]
            for r in range(rows)]


def _apply_levels(grid: list, black: float, white: float,
                   lo: float, hi: float, invert: bool = False) -> list:
    """Map `grid`'s 0..1 luminance onto heights in `lo`..`hi`.

    `black`/`white` are the input levels: at or below `black` becomes the
    low end, at or above `white` the high end, and everything between
    stretches linearly across. That is what makes an image usable as a
    heightfield -- a photo rarely spans the full range, and without levels
    the interesting part is squeezed into the middle.

    `white <= black` would divide by zero; the span collapses to a step at
    that value instead, which is the sensible reading of "everything below
    is low, everything above is high".
    """
    span = white - black
    out = []
    for row in grid:
        new = []
        for v in row:
            if span <= 0:
                t = 0.0 if v < black else 1.0
            else:
                t = min(1.0, max(0.0, (v - black) / span))
            if invert:
                t = 1.0 - t
            new.append(round(lo + t * (hi - lo), 6))
        out.append(new)
    return out


def _normalize_heights(value: list, lo: float, hi: float) -> list:
    """`value` rescaled linearly so its lowest height becomes `lo` and its
    highest `hi`, which leaves the field's shape untouched.

    A field that is already flat has no range to map from, so every cell
    becomes `lo` -- the alternative is dividing by zero, and there is no
    reading of "spread this constant across a range" that says otherwise.
    """
    flat = [h for row in value for h in row]
    lowest, highest = min(flat), max(flat)
    span = highest - lowest
    if span == 0:
        return [[lo for _ in row] for row in value]
    scale = (hi - lo) / span
    return [[round(lo + (h - lowest) * scale, 6) for h in row] for row in value]


def _ask_range(parent, cur_lo: float, cur_hi: float):
    """Prompt for a low/high pair, or (None, None) if cancelled. Defaults to
    0..1, the range a heightfield is normally consumed in, with the field's
    own current range shown so the user can see what is being replaced."""
    from PySide6.QtWidgets import QDialogButtonBox, QDoubleSpinBox, QFormLayout

    dlg = QDialog(parent)
    dlg.setWindowTitle("Normalize Heights")
    form = QFormLayout(dlg)
    form.addRow(QLabel(f"Current range: {cur_lo:g} to {cur_hi:g}"))

    def spin(val):
        box = QDoubleSpinBox()
        box.setRange(-1e6, 1e6)
        box.setDecimals(4)
        box.setValue(val)
        return box

    lo_box, hi_box = spin(0.0), spin(1.0)
    form.addRow("Lowest:", lo_box)
    form.addRow("Highest:", hi_box)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None, None
    return lo_box.value(), hi_box.value()


def _is_heightfield(v) -> bool:
    """A heightfield is a rectangular 2D list of plain numbers -- what
    BOSL2's `heightfield()` takes as `data`, one scalar height per grid
    cell.

    Deliberately distinct from both neighbours: `_is_grid` wants rows of
    *points*, one nesting level deeper, and `_is_matrix` wants a SQUARE
    2x2..5x5. A heightfield is neither, so nothing offered it an editor
    before. Rows must all be the same length -- a ragged one is not a
    height map, and the surface it would build has no meaning.
    """
    if not (_is_list(v) and len(v) >= 2):
        return False
    if not all(_is_list(row) and len(row) >= 2
               and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                       for x in row)
               for row in v):
        return False
    return len({len(row) for row in v}) == 1


# ---------------------------------------------------------------------------
# Region Viewer
# ---------------------------------------------------------------------------

def _lower_left_tip(image) -> tuple:
    """The (x, y) of the lowest-left opaque pixel in `image`.

    For an eyedropper that is the point that does the picking, which is
    where a cursor has to be anchored. Measured from the rendered artwork
    rather than written down, so editing the icon moves the hotspot with
    it instead of leaving it pointing at where the tip used to be.

    "Lowest-left" is the largest (y - x): the furthest along the
    down-and-left diagonal the eyedropper is drawn on.
    """
    width, height = image.width(), image.height()
    best = None
    best_score = None
    for y in range(height):
        for x in range(width):
            if image.pixelColor(x, y).alpha() <= 40:
                continue
            score = y - x
            if best_score is None or score > best_score:
                best_score, best = score, (x, y)
    return best or (0, height - 1)


def _eyedropper_cursor(size: int = 32):
    """The `eyedropper.svg` icon as a cursor, anchored at its tip.

    The same artwork as the button, rather than a second drawn copy: it is
    already white-filled with a grey outline, so it reads over a light or a
    dark image without needing a special high-contrast variant.

    Rendered at twice `size` and marked as such, so it stays sharp on a
    Retina display; the hotspot is in logical pixels either way.
    """
    from PySide6.QtGui import QCursor, QImage, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtCore import QRectF

    path = _ICONS_DIR / "eyedropper.svg"
    if not path.exists():
        return QCursor(Qt.CursorShape.CrossCursor)

    ratio = 2
    px = size * ratio
    image = QImage(px, px, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(path)).render(painter, QRectF(0, 0, px, px))
    painter.end()

    tip_x, tip_y = _lower_left_tip(image)
    pm = QPixmap.fromImage(image)
    pm.setDevicePixelRatio(ratio)
    # The hotspot is in logical pixels, so scale the measurement back down.
    return QCursor(pm, int(round(tip_x / ratio)), int(round(tip_y / ratio)))


class _CropLabel(QLabel):
    """The source image with a draggable crop rectangle over it.

    Drag to select a region; a click without a drag clears it back to the
    whole image. The rectangle is kept in IMAGE coordinates, not widget
    ones, so it survives the label being resized and means the same thing
    whatever the preview is scaled to.
    """

    crop_changed = Signal()
    #: 0..1 luminance sampled from a click while a picker is armed.
    luminance_picked = Signal(float)

    #: A drag shorter than this is a click, and clears the crop. Without a
    #: floor, a click with a pixel of shake selects a 2px region and the
    #: preview goes blank with no obvious cause.
    _MIN_DRAG_PX = 5

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self._image = image
        self._crop = None          # QRect in image coords, or None for all
        self._drag_from = None
        self._drag_to = None
        #: While set, a click samples brightness instead of cropping.
        self._picking = False
        #: Grayscale copy, made once: a picker samples it repeatedly, and
        #: converting per click would re-walk the whole image each time.
        self._grey = None
        self.setMinimumSize(220, 180)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Drag to crop; click to use the whole image.")

    # -- geometry ------------------------------------------------------

    def _draw_rect(self):
        """Where the image is actually painted, in widget coordinates."""
        from PySide6.QtCore import QRect
        w, h = self.width(), self.height()
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return QRect(0, 0, w, h)
        scale = min(w / iw, h / ih)
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
        return QRect((w - dw) // 2, (h - dh) // 2, dw, dh)

    def _to_image(self, pos):
        """A widget point as image coordinates, clamped to the image."""
        r = self._draw_rect()
        if r.width() <= 0 or r.height() <= 0:
            return 0, 0
        fx = (pos.x() - r.x()) / r.width()
        fy = (pos.y() - r.y()) / r.height()
        x = int(round(min(1.0, max(0.0, fx)) * self._image.width()))
        y = int(round(min(1.0, max(0.0, fy)) * self._image.height()))
        return (min(x, self._image.width() - 1),
                min(y, self._image.height() - 1))

    def crop_rect(self):
        """The selected region in image coordinates, or None for all of it."""
        return self._crop

    def cropped_image(self):
        return self._image if self._crop is None else self._image.copy(self._crop)

    def set_crop(self, rect):
        self._crop = rect
        self.update()
        self.crop_changed.emit()

    # -- picking -------------------------------------------------------

    def set_picking(self, on: bool):
        """Arm or disarm brightness picking. While armed a click samples
        rather than starting a crop."""
        self._picking = on
        # The eyedropper itself, anchored at its tip, rather than a generic
        # pointing hand: the cursor is the only thing showing WHERE the
        # sample will be taken from, and a hand points at nothing in
        # particular.
        self.setCursor(_eyedropper_cursor() if on else Qt.CursorShape.CrossCursor)

    def luminance_at(self, x: int, y: int) -> float:
        """0..1 brightness around (x, y), averaged over a 3x3 neighbourhood.

        A single pixel is a poor sample of a photograph -- sensor noise and
        JPEG artefacts move one pixel far more than the tone the user is
        actually pointing at.
        """
        from PySide6.QtGui import QImage

        if self._grey is None:
            self._grey = self._image.convertToFormat(
                QImage.Format.Format_Grayscale8)
        w, h = self._grey.width(), self._grey.height()
        total = count = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                px, py = x + dx, y + dy
                if 0 <= px < w and 0 <= py < h:
                    total += self._grey.pixelColor(px, py).value()
                    count += 1
        return (total / count / 255.0) if count else 0.0

    # -- interaction ---------------------------------------------------

    def mousePressEvent(self, event):
        if self._picking:
            x, y = self._to_image(event.position().toPoint())
            self.luminance_picked.emit(self.luminance_at(x, y))
            return
        self._drag_from = event.position().toPoint()
        self._drag_to = self._drag_from
        self.update()

    def mouseMoveEvent(self, event):
        if self._picking:
            return
        if self._drag_from is not None:
            self._drag_to = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        from PySide6.QtCore import QRect
        if self._picking or self._drag_from is None:
            return
        start, end = self._drag_from, event.position().toPoint()
        self._drag_from = self._drag_to = None
        if (abs(end.x() - start.x()) < self._MIN_DRAG_PX
                and abs(end.y() - start.y()) < self._MIN_DRAG_PX):
            self.set_crop(None)          # a click means "all of it"
            return
        x0, y0 = self._to_image(start)
        x1, y1 = self._to_image(end)
        rect = QRect(min(x0, x1), min(y0, y1),
                     abs(x1 - x0) or 1, abs(y1 - y0) or 1)
        self.set_crop(rect)

    # -- painting ------------------------------------------------------

    def paintEvent(self, event):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

        painter = QPainter(self)
        area = self._draw_rect()
        painter.drawPixmap(area, QPixmap.fromImage(self._image))

        # The live drag wins over the committed crop, so the rectangle
        # tracks the cursor rather than only appearing on release.
        if self._drag_from is not None and self._drag_to is not None:
            box = QRect(self._drag_from, self._drag_to).normalized()
        elif self._crop is not None:
            iw, ih = self._image.width(), self._image.height()
            box = QRect(
                area.x() + round(self._crop.x() / iw * area.width()),
                area.y() + round(self._crop.y() / ih * area.height()),
                max(1, round(self._crop.width() / iw * area.width())),
                max(1, round(self._crop.height() / ih * area.height())))
        else:
            painter.end()
            return

        # Dim everything outside the selection rather than outlining it
        # alone: on a busy image a thin rectangle is easy to lose.
        shade = QColor(0, 0, 0, 110)
        for part in (QRect(area.x(), area.y(), area.width(), box.y() - area.y()),
                      QRect(area.x(), box.bottom() + 1, area.width(),
                            area.bottom() - box.bottom()),
                      QRect(area.x(), box.y(), box.x() - area.x(), box.height()),
                      QRect(box.right() + 1, box.y(),
                            area.right() - box.right(), box.height())):
            if part.width() > 0 and part.height() > 0:
                painter.fillRect(part.intersected(area), shade)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawRect(box.intersected(area))
        painter.end()


class HeightfieldImportDialog(QDialog):
    """Turn an image into a heightfield: sampling, levels and output range,
    over a live preview of the result.

    The preview shows what the FIELD will be, not what the file looks like
    -- sampled to the chosen grid and with the levels applied -- because
    that is the thing being decided. Seeing the original instead would hide
    exactly the choices this dialog exists to make.
    """

    #: Preview size. Big enough to judge the result, small enough that
    #: re-sampling on every slider tick stays instant.
    _PREVIEW_PX = 260

    def __init__(self, image, parent=None, rows: int = 32, cols: int = 32):
        super().__init__(parent)
        self.setWindowTitle("Import Heightfield from Image")
        self._image = image
        self._grid: list | None = None
        #: Guards the rows <-> columns round trip under "Uniform", which
        #: would otherwise each keep correcting the other. Set before any
        #: widget exists, since building one can fire its own handler.
        self._matching = False

        layout = QVBoxLayout(self)

        # Source on the left to crop on, result on the right. Cropping
        # needs the picture; judging levels and sampling needs the field.
        panes = QHBoxLayout()
        self._crop_label = _CropLabel(image)
        self._crop_label.setStyleSheet("QLabel { border: 1px solid palette(mid); }")
        self._crop_label.crop_changed.connect(self._on_crop_changed)
        panes.addWidget(self._crop_label, 1)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(self._PREVIEW_PX, self._PREVIEW_PX)
        self._preview.setStyleSheet("QLabel { border: 1px solid palette(mid); }")
        panes.addWidget(self._preview, 1)
        layout.addLayout(panes, 1)

        self._source_label = QLabel()
        layout.addWidget(self._source_label)

        form = QFormLayout()

        def spin(lo, hi, val, step=1, decimals=0, on_change=None):
            box = QDoubleSpinBox() if decimals else QSpinBox()
            box.setRange(lo, hi)
            if decimals:
                box.setDecimals(decimals)
                box.setSingleStep(step)
            box.setValue(val)
            box.valueChanged.connect(on_change or self._refresh)
            return box

        # Sampling. An image is normally far larger than a useful field --
        # a 3000x2000 photo would be six million heights.
        # Rows and columns go through one handler that matches the aspect
        # BEFORE refreshing. Refreshing first and matching after left the
        # preview built from the pair as typed while the boxes showed the
        # matched pair -- the guard that stops the two boxes correcting
        # each other forever also swallowed the refresh that would have
        # caught up. Only committing the field with Return fixed it, which
        # is what made this look like "typing does not work".
        self._rows = spin(2, 512, rows,
                          on_change=lambda _v: self._on_sample_size(True))
        self._cols = spin(2, 512, cols,
                          on_change=lambda _v: self._on_sample_size(False))
        self._uniform = QCheckBox("Uniform")
        self._uniform.setToolTip(
            "Keep the sample grid in the crop's proportions, so a cell is\n"
            "square and the field is not stretched.")
        self._uniform.toggled.connect(self._on_uniform_toggled)
        size_row = QHBoxLayout()
        size_row.addWidget(self._rows)
        size_row.addWidget(QLabel("rows x"))
        size_row.addWidget(self._cols)
        size_row.addWidget(QLabel("columns"))
        size_row.addWidget(self._uniform)
        size_row.addStretch()
        form.addRow("Sample to:", size_row)

        # Input levels, each with an eyedropper that reads its value off
        # the image -- guessing a photo's black and white points by typing
        # numbers is far harder than pointing at them.
        self._black = spin(0.0, 1.0, 0.0, 0.05, 3)
        self._white = spin(0.0, 1.0, 1.0, 0.05, 3)
        self._pick_black = self._picker_button("darkest")
        self._pick_white = self._picker_button("lightest")
        lv = QHBoxLayout()
        lv.addWidget(QLabel("black"))
        lv.addWidget(self._black)
        lv.addWidget(self._pick_black)
        lv.addSpacing(12)
        lv.addWidget(QLabel("white"))
        lv.addWidget(self._white)
        lv.addWidget(self._pick_white)
        lv.addStretch()
        form.addRow("Levels:", lv)
        self._crop_label.luminance_picked.connect(self._on_picked)

        # Output range.
        self._lo = spin(-1e4, 1e4, 0.0, 0.1, 3)
        self._hi = spin(-1e4, 1e4, 1.0, 0.1, 3)
        rr = QHBoxLayout()
        rr.addWidget(QLabel("low"))
        rr.addWidget(self._lo)
        rr.addWidget(QLabel("high"))
        rr.addWidget(self._hi)
        rr.addStretch()
        form.addRow("Heights:", rr)

        self._invert = QCheckBox("Invert (dark is high)")
        self._invert.toggled.connect(self._refresh)
        form.addRow("", self._invert)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()

    # -- picking levels off the image ------------------------------------

    def _picker_button(self, which: str) -> QPushButton:
        """A checkable eyedropper. Checkable because picking is a mode: the
        button stays lit while the next click on the image will sample,
        which is the only thing telling the user why their click is not
        cropping."""
        btn = QPushButton()
        # apply_themed_icon rather than a one-off QIcon: it re-applies on
        # every appearance change, where a snapshot taken here would keep
        # the old ink after a light/dark switch.
        icon_path = _ICONS_DIR / "eyedropper.svg"
        if icon_path.exists():
            apply_themed_icon(btn, icon_path)
        btn.setCheckable(True)
        btn.setFixedWidth(30)
        btn.setToolTip(f"Click the {which} part of the image to set this level.")
        btn.toggled.connect(self._on_picker_toggled)
        return btn

    def _on_picker_toggled(self, on: bool):
        # Only one at a time: with both armed there would be no telling
        # which a click was meant for.
        if on:
            other = (self._pick_white if self.sender() is self._pick_black
                     else self._pick_black)
            if other.isChecked():
                other.setChecked(False)
        self._crop_label.set_picking(
            self._pick_black.isChecked() or self._pick_white.isChecked())

    def _on_picked(self, value: float):
        """A sampled brightness lands in whichever level is armed.

        The picker disarms itself afterwards. Staying armed would make the
        next click on the image silently overwrite the level just set,
        rather than crop as expected.
        """
        target = self._black if self._pick_black.isChecked() else self._white
        if not (self._pick_black.isChecked() or self._pick_white.isChecked()):
            return
        target.setValue(round(value, 3))
        self._pick_black.setChecked(False)
        self._pick_white.setChecked(False)

    # -- sampling shape --------------------------------------------------

    def _on_sample_size(self, from_rows: bool):
        """Rows or columns changed: match the aspect, then rebuild once."""
        if self._matching:
            return          # the other box being corrected; its own call follows
        self._match_aspect(from_rows=from_rows)
        self._refresh()

    def _on_crop_changed(self):
        if self._uniform.isChecked():
            self._match_aspect(from_rows=True)
        self._refresh()

    def _on_uniform_toggled(self, on: bool):
        if on:
            self._match_aspect(from_rows=True)
        self._refresh()

    def _match_aspect(self, from_rows: bool):
        """Hold the sample grid to the crop's proportions.

        Driven from whichever box the user just changed, so either can lead
        -- with a flag to stop the two correcting each other forever.
        """
        if not self._uniform.isChecked() or self._matching:
            return
        img = self._crop_label.cropped_image()
        if img.width() <= 0 or img.height() <= 0:
            return
        self._matching = True
        try:
            if from_rows:
                want = max(2, min(512, round(self._rows.value()
                                              * img.width() / img.height())))
                self._cols.setValue(want)
            else:
                want = max(2, min(512, round(self._cols.value()
                                              * img.height() / img.width())))
                self._rows.setValue(want)
        finally:
            self._matching = False

    def _refresh(self):
        if self._matching:
            return          # mid-correction; the second edit will refresh
        img = self._crop_label.cropped_image()
        self._grid = _apply_levels(
            _image_luminance_grid(img, self._rows.value(), self._cols.value()),
            self._black.value(), self._white.value(),
            self._lo.value(), self._hi.value(), self._invert.isChecked())
        crop = self._crop_label.crop_rect()
        where = "whole image" if crop is None else \
            f"crop {crop.width()} x {crop.height()} at {crop.x()},{crop.y()}"
        self._source_label.setText(
            f"Source: {self._image.width()} x {self._image.height()} pixels — {where}")
        self._preview.setPixmap(self._preview_pixmap())

    def _preview_pixmap(self):
        """The sampled field as a grey image, scaled up with no smoothing
        so each height reads as one visible cell -- the sampling grid is
        one of the things being chosen here."""
        from PySide6.QtGui import QImage, QPixmap, qRgb

        rows, cols = len(self._grid), len(self._grid[0])
        flat = [v for row in self._grid for v in row]
        lo, hi = min(flat), max(flat)
        span = (hi - lo) or 1.0
        img = QImage(cols, rows, QImage.Format.Format_RGB32)
        for r, row in enumerate(self._grid):
            for c, v in enumerate(row):
                g = int(round(255 * (v - lo) / span))
                img.setPixel(c, r, qRgb(g, g, g))
        return QPixmap.fromImage(img).scaled(
            self._PREVIEW_PX, self._PREVIEW_PX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)

    def heightfield(self) -> list:
        """The field as configured. Only meaningful after `exec()` returned
        Accepted."""
        return self._grid


class HeightfieldViewer(QDialog, _UndoableViewerMixin):
    """A rectangular 2D list of scalar heights, as an editable table beside
    a 3D surface of the same data.

    The surface reuses `_GridViewport` rather than growing another one: a
    heightfield IS a grid of points once the implicit coordinates are
    filled in (x = column, y = row, z = the height), so the mesh, the
    markers, the framing and the picking all come for free.

    The viewport is never handed `editable=True`, even in editing mode.
    Its editing gesture drags a vertex in three dimensions, and two of
    those are the cell's position in the array -- a heightfield has no way
    to express a moved x or y. Heights are edited in the table, where the
    one number per cell that CAN change is the only thing on offer.
    """

    committed = Signal(str)

    #: Heights are usually a 0..1 field. Three decimals is what the finest
    #: nudge steps by, so a cell always shows the change a keypress made --
    #: at two it looked like nothing had happened. Matches the writeback.
    #: Display only: the stored value keeps every digit it had, and only a
    #: cell the user actually types into changes.
    _DECIMALS = 3

    #: Display-only exaggeration for the preview, as a percentage. A height
    #: map's values are often a small fraction of its width, which renders
    #: as a flat sheet; this scales the surface without touching the data.
    #: Presets only -- the box is editable, so any percentage can be typed.
    _Z_PRESETS = [15, 25, 33, 50, 75, 100, 150, 200, 300, 400]

    def __init__(self, title: str, value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "Heightfield Editor" if editable else "Heightfield Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1040, 520)

        self._editable = editable
        self._value = value
        self._zscale = 1.0
        #: Guards the table <-> viewport selection round trip.
        self._syncing = False
        #: Show the tile's neighbours as it would repeat -- see _as_grid.
        self._tiled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self._vp = _GridViewport(self._as_grid(), False, self, editable=editable)
        self._vp.z_only = True
        # The surface is the subject here, not a lattice of control points.
        # The skeleton only repeats what Show Edges draws from the real
        # triangulation, and lying coplanar with the surface it speckles
        # through as a coloured grid over what should be a clean shape.
        self._vp.show_skeleton = False
        self._vp.z_nudge_scale = self._zscale
        _sync_viewport_to_main_window(self._vp)
        self._vp.vertex_clicked.connect(self._on_vertex_clicked)
        if editable:
            self._vp.vertex_moved.connect(self._on_vertex_moved)
            self._vp.vertex_drag_started.connect(self._begin_live_edit)
            self._vp.vertex_drag_finished.connect(
                lambda: self._end_live_edit("Change Height"))
        splitter.addWidget(self._vp)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            if editable else QAbstractItemView.EditTrigger.NoEditTriggers)
        self._build_table()
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        if editable:
            for header, is_row in ((self._table.verticalHeader(), True),
                                    (self._table.horizontalHeader(), False)):
                header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                header.customContextMenuRequested.connect(
                    lambda pos, h=header, rw=is_row: self._header_menu(h, rw, pos))
        if editable:
            self._table.itemChanged.connect(self._on_item_changed)
        right_layout.addWidget(self._table)

        self._size_label = QLabel()
        right_layout.addWidget(self._size_label)

        # Their own row under the label: side by side, the two dropdowns and
        # their captions left the label no room to say anything.
        zrow = QHBoxLayout()
        zrow.setContentsMargins(0, 0, 0, 0)
        zrow.addWidget(QLabel("Style:"))
        self._style_combo = QComboBox()
        self._style_combo.addItems(QUAD_STYLES)
        self._style_combo.setToolTip(
            "How each quad is split into triangles -- BOSL2's own\n"
            "vnf_vertex_array styles, which is what heightfield() uses.")
        self._style_combo.currentTextChanged.connect(self._on_style_changed)
        _size_combo_to_widest_item(self._style_combo)
        zrow.addWidget(self._style_combo)
        zrow.addStretch()
        # An image-derived field is routinely 50x50 or more, where a marker
        # on every point buries the surface it sits on. Selected points keep
        # their markers regardless, so turning this off does not lose track
        # of what is being edited.
        self._verts_cb = QCheckBox("Show points")
        self._verts_cb.setChecked(True)
        self._verts_cb.setToolTip(
            "Marker on every point. Worth turning off for a large field;\n"
            "selected points stay marked either way.")
        self._verts_cb.toggled.connect(self._vp.set_show_unselected)
        zrow.addWidget(self._verts_cb)
        zrow.addStretch()

        self._tile_cb = QCheckBox("Show tiling")
        self._tile_cb.setToolTip(
            "Draw the tile's neighbours as the texture would repeat, so the\n"
            "seam between one copy and the next is visible. Only the middle\n"
            "copy is editable.")
        self._tile_cb.toggled.connect(self._on_tiling_toggled)
        zrow.addWidget(self._tile_cb)
        # A stretch either side, so the checkbox sits midway between the
        # Style and Z scale controls rather than crowding one of them.
        zrow.addStretch()
        zrow.addWidget(QLabel("Z scale:"))
        self._zcombo = QComboBox()
        self._zcombo.setEditable(True)
        self._zcombo.addItems([f"{pct}%" for pct in self._Z_PRESETS])
        self._zcombo.setCurrentText("100%")
        self._zcombo.setToolTip("Vertical exaggeration of the preview only; "
                                 "the stored heights never change.\n"
                                 "Any percentage can be typed, not just these.")
        # `activated` for a pick and `editingFinished` for something typed,
        # rather than currentTextChanged: that fires on every keystroke, so
        # typing "200" would rebuild the surface at 2% and then 20% on the
        # way. _size_combo_to_widest_item before the box is populated with
        # anything longer than its presets.
        self._zcombo.activated.connect(lambda _i: self._apply_zscale_text())
        self._zcombo.lineEdit().editingFinished.connect(self._apply_zscale_text)
        _size_combo_to_widest_item(self._zcombo)
        zrow.addWidget(self._zcombo)
        right_layout.addLayout(zrow)

        splitter.addWidget(right)
        # An even split: the table's columns have a width they cannot go
        # below without eliding, so starving this half just means scrolling
        # past most of the field.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        # The mesh cannot be built until there is a GL context to build it
        # in; the constructor's grid argument only records the data.
        self._vp.schedule_load(self._do_initial_load)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        if editable:
            self._setup_undo(value)
            import_btn = QPushButton("Import Image…")
            import_btn.setToolTip("Build the field from an image's brightness.")
            import_btn.clicked.connect(self._on_import_image)
            btn_row.addWidget(import_btn)
            normalize = QPushButton("Normalize…")
            normalize.setToolTip("Rescale every height into a given range, "
                                  "keeping the shape of the field.")
            normalize.clicked.connect(self._on_normalize)
            btn_row.addWidget(normalize)
            btn_row.addLayout(self._make_reset_button_row())
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._update_size_label()

    # -- data <-> surface ----------------------------------------------

    def _as_grid(self) -> list:
        """The heights as a grid of points, filling in the coordinates the
        array only implies: x = column, y = row -- but counted DOWN from
        the top.

        A heightfield texture's first row is the top of the image, the way
        every raster format orders its rows, so row 0 has to be the far
        edge of the surface rather than the near one. Mapping the row index
        straight onto y put it at the near edge and drew every field
        upside-down against its own source image.

        With tiling on, the field is laid out 3x3 with the editable copy in
        the middle, so the seam between one tile and the next is visible --
        which is the thing a texture tile actually has to get right, and
        the thing an isolated tile cannot show. The period is the array's
        own size, so column 0 of the next tile sits immediately after the
        last column of this one.
        """
        z = self._zscale
        rows, cols = len(self._value), len(self._value[0])
        if self._tiled:
            reps = (-1, 0, 1)
            return [[[float(tc * cols + c), float(tr * rows + (rows - 1 - r)),
                      float(h) * z]
                     for tc in reps for c, h in enumerate(row)]
                    for tr in reps for r, row in enumerate(self._value)]
        # One row and column past the end, wrapped from the start. An
        # N x M field tiles into N x M cells of surface, but N x M points
        # only span (N-1) x (M-1) of them -- so an untiled tile was drawn
        # a row and a column short of what it actually covers, and the
        # edge where it meets its own next copy was the part not shown.
        # The extra row falls BELOW the last one once y counts down, which
        # is where the next tile's row 0 genuinely continues.
        return [[[float(c), float(rows - 1 - r),
                  float(self._value[r % rows][c % cols]) * z]
                 for c in range(cols + 1)]
                for r in range(rows + 1)]

    def _flat_index(self, row: int, col: int) -> int:
        """The viewport's index for cell (row, col) of the editable tile."""
        rows, cols = len(self._value), len(self._value[0])
        if not self._tiled:
            return row * (cols + 1) + col      # the grid is one wider
        return (rows + row) * (3 * cols) + (cols + col)

    def _cell_of_flat(self, flat: int):
        """(row, col) within the EDITABLE tile, or None for a vertex in one
        of the surrounding copies -- those are there to be looked at, not
        edited, and every path that acts on a vertex has to say so."""
        rows, cols = len(self._value), len(self._value[0])
        if not self._tiled:
            r, c = divmod(flat, cols + 1)
            return (r, c) if 0 <= r < rows and 0 <= c < cols else None
        r, c = divmod(flat, 3 * cols)
        r -= rows
        c -= cols
        return (r, c) if 0 <= r < rows and 0 <= c < cols else None

    # -- table ----------------------------------------------------------

    def _fmt(self, v) -> str:
        return f"{float(v):.{self._DECIMALS}f}"

    def _build_table(self):
        rows, cols = len(self._value), len(self._value[0])
        self._table.blockSignals(True)
        self._table.setRowCount(rows)
        self._table.setColumnCount(cols)
        self._table.setHorizontalHeaderLabels([str(c) for c in range(cols)])
        self._table.setVerticalHeaderLabels([str(r) for r in range(rows)])
        _style_table_headers(self._table)
        for r in range(rows):
            for c in range(cols):
                item = QTableWidgetItem(self._fmt(self._value[r][c]))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                       | Qt.AlignmentFlag.AlignVCenter)
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)
        # Even columns filling the width, rather than each sized to its own
        # contents: every cell holds one number of roughly the same length,
        # so content-sizing only produced a ragged right edge and left the
        # table not filling its half of the splitter.
        #
        # With a floor, though. Stretch alone divides whatever width there
        # is by the column count, and a wide field in a narrow pane shrank
        # the columns until every value was elided to "4.…" -- evenly sized
        # and unreadable. Below the floor the table scrolls instead.
        header = self._table.horizontalHeader()
        header.setMinimumSectionSize(
            self._table.fontMetrics().horizontalAdvance("-000.000") + 12)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.blockSignals(False)

    def _update_size_label(self):
        rows, cols = len(self._value), len(self._value[0])
        flat = [h for row in self._value for h in row]
        self._size_label.setText(
            f"{rows} x {cols} — min {self._fmt(min(flat))}, max {self._fmt(max(flat))}")

    # -- signals ---------------------------------------------------------

    def _do_initial_load(self):
        self._vp.marker_indices = {
            self._flat_index(r, c)
            for r in range(len(self._value))
            for c in range(len(self._value[0]))}
        self._vp.load_grid(self._as_grid())      # never tiled at startup

    def _reload_surface(self):
        """Rebuild the surface after the data or the Z scale changed.

        makeCurrent/doneCurrent around it because this runs from a table
        edit or a combo box, not from the viewport's own paint path -- a GL
        call made while another widget's context is current corrupts that
        widget instead.
        """
        if self._vp._ctx is None:
            return
        # Only the editable copy gets grab handles; the neighbours are
        # there to show the seam, not to be dragged.
        self._vp.marker_indices = {
            self._flat_index(r, c)
            for r in range(len(self._value))
            for c in range(len(self._value[0]))}
        self._vp.makeCurrent()
        self._vp.load_grid(self._as_grid())
        self._vp.doneCurrent()
        self._vp.update()

    def _on_style_changed(self, name: str):
        self._vp.quad_style = name
        self._reload_surface()

    def _on_vertex_moved(self, flat: int, _x: float, _y: float, z: float):
        """A dragged or nudged vertex writes back its height alone. The
        viewport is in z_only mode, so x and y arrive unchanged anyway --
        ignoring them here says why they can be ignored."""
        cell = self._cell_of_flat(flat)
        if cell is None:
            return          # a neighbouring tile's copy: not editable
        r, c = cell
        # In place, deliberately: a nudge of a whole selection calls this
        # once per point, and _commit_value would push an undo step for
        # each. The drag_started/drag_finished bracket around the gesture
        # pushes exactly one for all of them (see _end_live_edit).
        self._value[r][c] = round(z / self._zscale, 6) if self._zscale else z
        self._table.blockSignals(True)
        self._table.item(r, c).setText(self._fmt(self._value[r][c]))
        self._table.blockSignals(False)
        self._reload_surface()
        self._update_size_label()

    def _on_tiling_toggled(self, on: bool):
        self._tiled = on
        self._reload_surface()
        self._on_selection_changed()      # indices shift when tiling changes

    def _header_menu(self, header, is_row: bool, pos):
        """Duplicate/Delete on a row or column header.

        Delete is disabled at 2 rows or 2 columns: below that the value is
        no longer a heightfield (see `_is_heightfield`), and the dialog
        would be editing something it could not save back.
        """
        idx = header.logicalIndexAt(pos)
        if idx < 0:
            return
        what = "Row" if is_row else "Column"
        count = len(self._value) if is_row else len(self._value[0])

        menu = QMenu(self)
        menu.addAction(f"Duplicate {what} {idx}",
                       lambda: self._duplicate_line(idx, is_row))
        delete = menu.addAction(f"Delete {what} {idx}",
                                 lambda: self._confirm_delete_line(idx, is_row))
        if count <= 2:
            delete.setEnabled(False)
            delete.setToolTip(f"A heightfield needs at least two {what.lower()}s.")
        menu.exec(header.mapToGlobal(pos))

    def _duplicate_line(self, idx: int, is_row: bool):
        value = copy.deepcopy(self._value)
        if is_row:
            value.insert(idx + 1, list(value[idx]))
        else:
            for row in value:
                row.insert(idx + 1, row[idx])
        self._commit_value(value, f"Duplicate {'Row' if is_row else 'Column'}")

    def _confirm_delete_line(self, idx: int, is_row: bool):
        """Ask first. Deleting a line throws away a whole row or column of
        hand-placed heights, and the menu item sits one pixel from
        Duplicate -- undo is there, but not before the surface has already
        changed under the user.

        The question is separate from `_delete_line` so the operation
        itself stays callable without a dialog in the way.
        """
        what = "row" if is_row else "column"
        count = len(self._value[0]) if is_row else len(self._value)
        answer = QMessageBox.question(
            self, f"Delete {what.capitalize()}",
            f"Delete {what} {idx}? Its {count} height"
            f"{'' if count == 1 else 's'} will be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._delete_line(idx, is_row)

    def _delete_line(self, idx: int, is_row: bool):
        value = copy.deepcopy(self._value)
        if is_row:
            if len(value) <= 2:
                return
            del value[idx]
        else:
            if len(value[0]) <= 2:
                return
            for row in value:
                del row[idx]
        self._commit_value(value, f"Delete {'Row' if is_row else 'Column'}")

    def _apply_zscale_text(self):
        """Read the box as a percentage, or put back the last good value.

        Lenient about the "%" and surrounding space, since a typed entry is
        as likely to be "150" as "150%". Anything that is not a positive
        number is refused outright rather than guessed at -- a zero or
        negative scale would flatten or mirror the preview, and neither is
        something the box can be asked for by accident.
        """
        text = self._zcombo.currentText().strip().rstrip("%").strip()
        try:
            pct = float(text)
        except ValueError:
            pct = 0.0
        if pct <= 0:
            self._zcombo.setCurrentText(self._fmt_pct(self._zscale))
            return
        self._zscale = pct / 100.0
        self._vp.z_nudge_scale = self._zscale
        self._zcombo.setCurrentText(self._fmt_pct(self._zscale))
        self._reload_surface()

    @staticmethod
    def _fmt_pct(scale: float) -> str:
        pct = scale * 100.0
        return f"{pct:g}%"

    def _on_selection_changed(self):
        """Every selected cell lights up its vertex -- and the viewport
        nudges whatever is selected, so this is also what makes a group of
        points move together."""
        if self._syncing:
            return
        self._syncing = True
        try:
            self._vp.set_selected([self._flat_index(i.row(), i.column())
                                   for i in self._table.selectedItems()])
        finally:
            self._syncing = False

    def _on_vertex_clicked(self, flat: int, mode: str):
        """A click in the viewport selects the matching cell, honouring the
        same replace/add/toggle modifiers the table itself uses."""
        if self._syncing:
            return
        cell = None if flat < 0 else self._cell_of_flat(flat)
        self._syncing = True
        try:
            if cell is None:
                if mode == "replace" and flat < 0:
                    self._table.clearSelection()
                return
            index = self._table.model().index(cell[0], cell[1])
            flag = (QItemSelectionModel.SelectionFlag.Toggle if mode == "toggle"
                    else QItemSelectionModel.SelectionFlag.Select)
            if mode == "replace":
                self._table.clearSelection()
            self._table.selectionModel().select(index, flag)
            self._table.setCurrentIndex(index)
        finally:
            self._syncing = False
        self._on_selection_changed_from_viewport()

    def _on_selection_changed_from_viewport(self):
        self._vp.set_selected([self._flat_index(i.row(), i.column())
                               for i in self._table.selectedItems()])

    def _on_item_changed(self, item: QTableWidgetItem):
        parsed = _parse_number(item.text())
        if parsed is None:
            self._table.blockSignals(True)
            item.setText(self._fmt(self._value[item.row()][item.column()]))
            self._table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._value)
        new_value[item.row()][item.column()] = parsed
        self._commit_value(new_value, "Edit Height")

    # -- undo mixin ------------------------------------------------------

    def _get_value(self):
        return self._value

    def _apply_value(self, value):
        # Rebuild rather than re-label when the shape changed: undoing a
        # Duplicate Row leaves fewer rows than there are table rows, and
        # writing into cells that no longer exist is how that crashes.
        resized = (len(value) != len(self._value)
                   or len(value[0]) != len(self._value[0]))
        self._value = value
        if resized:
            self._build_table()
        else:
            self._table.blockSignals(True)
            for r, row in enumerate(value):
                for c, h in enumerate(row):
                    self._table.item(r, c).setText(self._fmt(h))
            self._table.blockSignals(False)
        self._reload_surface()
        self._update_size_label()

    def _on_import_image(self):
        """Replace the field with one built from an image's brightness.

        Undoable like any other edit, so an import that turns out wrong is
        one Cmd+Z away rather than something to be careful about.
        """
        from PySide6.QtGui import QImage, QImageReader

        formats = " ".join(f"*.{bytes(f).decode()}"
                           for f in QImageReader.supportedImageFormats())
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Heightfield from Image", "",
            f"Images ({formats});;All files (*)")
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "Import Image",
                                 f"Could not read an image from:\n{path}")
            return

        dlg = HeightfieldImportDialog(image, self,
                                       rows=len(self._value), cols=len(self._value[0]))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        grid = dlg.heightfield()
        if grid:
            self._commit_value(grid, "Import Image")

    def _on_normalize(self):
        """Rescale every height into a range, keeping the field's shape.

        A heightfield is usually consumed as a 0..1 field, and one built by
        hand or lifted from data rarely arrives that way.
        """
        flat = [h for row in self._value for h in row]
        lo, hi = _ask_range(self, min(flat), max(flat))
        if lo is None:
            return
        self._commit_value(_normalize_heights(self._value, lo, hi), "Normalize")

    def _on_save(self):
        self.committed.emit(_format_heightfield(self._value))
        self.accept()
