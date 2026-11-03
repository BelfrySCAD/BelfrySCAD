"""Preferences ▸ Viewport ▸ Calibrate Anaglyph Colors: tune Grayscale
Anaglyph's red and cyan to a pair of glasses, one setting per screen, in the
manner of a display calibrator (Xbox's HDR calibration, Windows' dccw).

Each eye's colour is the pure colour (red, cyan) with a Tint, degrees of hue
either way, and a Brightness, which past 100% mixes in white for a lens that
leaks a little of the other colours. Each screen moves one of them until a
pattern just disappears:

- through its own lens, a colour that lens passes as fully as it passes
  white vanishes against white -- the most light that eye can get;
- through the other lens it should vanish against black -- no ghosting.

The white is plain white rather than the viewport's background: matching a
dark background would just dim the colour. Non-modal and saving on every
change, so the viewport behind it updates live (see PreferencesDialog's note
on modality); Cancel puts back the colours it opened with.
"""
import colorsys

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget)

LEFT_KEY, RIGHT_KEY = "viewport/anaglyphLeftColor", "viewport/anaglyphRightColor"
DEFAULTS = {LEFT_KEY: "#ff0000", RIGHT_KEY: "#00ffff"}
#: key -> (name, base hue in degrees, tint slider's ends, eye to close)
EYES = {
    LEFT_KEY: ("red", 0.0, ("Magenta", "Orange"), "right"),
    RIGHT_KEY: ("cyan", 180.0, ("Green", "Blue"), "left"),
}

#: Tint's reach either side of the pure colour, in degrees of hue. Real
#: lenses sit close to their nominal colour; a wider slider made the small
#: moves that matter too coarse to make.
TINT_RANGE = 20
#: Brightness's range, in percent. Past 100% mixes in white, for a lens that
#: also passes a little of the other colours: pure red then looks darker
#: than white through it. Narrow for the same reason as Tint.
MIN_BRIGHTNESS, MAX_BRIGHTNESS = 80, 120

STEPS = 6


def eye_color(base_hue: float, tint: float, brightness: float) -> tuple[float, float, float]:
    """The colour `tint` degrees of hue from `base_hue` at `brightness`
    percent: up to 100 the full-strength colour scaled down, past it the
    full colour mixed towards white."""
    r, g, b = colorsys.hsv_to_rgb(((base_hue + tint) / 360) % 1, 1.0, min(brightness, 100) / 100)
    white = max(brightness - 100, 0) / 100
    return tuple(c + white * (1 - c) for c in (r, g, b))


def eye_settings(base_hue: float, rgb) -> tuple[float, float]:
    """(tint, brightness) for `rgb`: the inverse of eye_color, clamped to
    the sliders' ranges."""
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    tint = (h * 360 - base_hue + 180) % 360 - 180
    brightness = v * 100 if v < 0.999 else 100 + (1 - s) * 100
    return (max(-TINT_RANGE, min(TINT_RANGE, tint)),
            max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, brightness)))


def _draw_pattern(p: QPainter, rect: QRectF, color: QColor, dx: float = 0.0):
    """A thick ring around a triangle: edges and a filled shape, which make
    "just gone" much easier to judge than a flat block of colour."""
    size = min(rect.width(), rect.height())
    c = rect.center() + QPointF(dx, 0)
    r = size * 0.36
    p.setPen(QPen(color, max(3.0, size * 0.09)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, r, r)
    t = r * 0.55
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPolygon(QPolygonF([c + QPointF(0, -t), c + QPointF(t * 0.95, t * 0.6),
                             c + QPointF(-t * 0.95, t * 0.6)]))


class _Target(QWidget):
    """The pattern in `fg()` on `bg`, with an optional caption under it."""

    def __init__(self, bg, fg, w=260, h=150, caption=""):
        super().__init__()
        self._bg, self._fg, self._caption = QColor(bg), fg, caption
        self.setFixedSize(w, h + (22 if caption else 0))
        self._h = h

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(0, 0, self.width(), self._h)
        p.fillRect(box, self._bg)
        _draw_pattern(p, box, self._fg())
        if self._caption:
            # Outlined, so the white "Just gone" example reads as a picture.
            p.setPen(QPen(QColor(150, 150, 150), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))
            p.setPen(self.palette().windowText().color())
            p.drawText(QRectF(0, self._h, self.width(), 22), Qt.AlignmentFlag.AlignCenter,
                       self._caption)


def stereo_preview(w: int, h: int, left_rgb, right_rgb, shift: float) -> np.ndarray:
    """An h x w x 3 Grayscale Anaglyph of a black pattern on white, drawn as
    the renderer composites it: the left eye's brightness in `left_rgb`, the
    right's in `right_rgb`. The pattern is offset `shift` pixels each way, so
    it floats in front of a flat frame drawn the same for both eyes."""
    def eye(dx):
        img = QImage(w, h, QImage.Format.Format_Grayscale8)
        img.fill(255)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(0, 0, 0), 4))
        p.drawRect(QRectF(8, 8, w - 16, h - 16))
        _draw_pattern(p, QRectF(0, 0, w, h), QColor(0, 0, 0), dx)
        p.end()
        rows = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())
        return rows[:, :w] / 255.0
    lum_l, lum_r = eye(shift), eye(-shift)       # crossed: in front of the screen
    out = lum_l[..., None] * np.asarray(left_rgb) + lum_r[..., None] * np.asarray(right_rgb)
    return np.clip(out, 0.0, 1.0)


class _Preview(QWidget):
    def __init__(self, colors):
        super().__init__()
        self._colors = colors        # () -> (left_rgb, right_rgb)
        self.setFixedSize(360, 220)

    def paintEvent(self, event):
        left, right = self._colors()
        rgb = np.ascontiguousarray(
            (stereo_preview(self.width(), self.height(), left, right, 5) * 255).astype(np.uint8))
        img = QImage(rgb.data, self.width(), self.height(), 3 * self.width(),
                     QImage.Format.Format_RGB888)
        QPainter(self).drawImage(0, 0, img)


def _page(html: str) -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    lay = QVBoxLayout(w)
    text = QLabel(html)
    text.setWordWrap(True)
    text.setAlignment(Qt.AlignmentFlag.AlignTop)
    lay.addWidget(text)
    return w, lay


def _refs(items, bg, w, h) -> QHBoxLayout:
    """A row of example patterns: (colour, caption) on `bg`."""
    row = QHBoxLayout()
    row.addStretch(1)
    for fg, caption in items:
        row.addWidget(_Target(bg, lambda c=QColor(fg): c, w, h, caption))
    row.addStretch(1)
    return row


class AnaglyphCalibrationDialog(QDialog):
    def __init__(self, parent, emit):
        super().__init__(parent)
        self.setWindowTitle("Calibrate Anaglyph Colors")
        self.setMinimumWidth(560)
        self._emit = emit
        self.state = {}                 # key -> [tint, brightness]
        self.sliders = {}               # (key, "tint" | "brightness") -> QSlider

        lay = QVBoxLayout(self)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages)
        self.pages.addWidget(_page(
            "<h3>Calibrate your red/cyan glasses</h3>"
            "<p>A few screens, one setting each, tune the red and cyan Grayscale Anaglyph "
            "draws with, so each eye sees its own picture as brightly as it can and none "
            "of the other's (the faint double image called ghosting).</p>"
            "<ol><li>Put your glasses on, red lens over your <b>left</b> eye.</li>"
            "<li>Turn the room lights down if they are bright, and sit where you usually do.</li>"
            "<li>On each screen, move the slider until the pattern <b>just disappears</b>.</li></ol>"
            "<p>Every change shows at once in the viewport. Cancel puts the colours back.</p>")[0])
        n = 1
        for key, (name, _hue, ends, close) in EYES.items():
            self._slider_page(key, "brightness", (
                f"<h3>Step {n} of {STEPS}: {name.capitalize()} brightness</h3>"
                f"<p>Close your <b>{close}</b> eye and look through the <b>{name}</b> lens only.</p>"
                f"<p>Raise <b>Brightness</b> until the ring and triangle <b>just disappear</b> "
                f"into the white, then stop: going further only adds ghosting.</p>"))
            self._slider_page(key, "tint", (
                f"<h3>Step {n + 1} of {STEPS}: {name.capitalize()} tint</h3>"
                f"<p>Still only the <b>{name}</b> lens. Slide <b>Tint</b> to wherever the pattern "
                f"is <b>hardest to see</b>."
                + (" Red/blue glasses usually want it well towards Blue." if name == "cyan" else "")
                + "</p><p>If it shows again at the best tint, go Back and raise Brightness "
                  "until it is gone.</p>"), ends)
            n += 2
        self._ghost_page(n)
        self._final_page(n + 1)

        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.back = QPushButton("Back")
        self.back.setAutoDefault(False)
        self.back.clicked.connect(lambda: self._go(-1))
        self.next = QPushButton("Next")
        self.next.setDefault(True)
        self.next.clicked.connect(lambda: self._go(1))
        factory = QPushButton("Reset to Factory Defaults")
        factory.setAutoDefault(False)
        factory.setToolTip("Back to pure red and cyan, for both eyes.")
        factory.clicked.connect(self.reset_all)
        row.addWidget(cancel)
        row.addWidget(factory)
        row.addStretch(1)
        row.addWidget(self.back)
        row.addWidget(self.next)
        lay.addLayout(row)
        self._load()

    # -- pages -------------------------------------------------------------

    def _slider_page(self, key, which, html, ends=("", "")):
        page, lay = _page(html)
        lay.addWidget(_Target("white", lambda: self.color(key)),
                      alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(QLabel("Through the lens, roughly:"))
        lay.addLayout(_refs((("#d8d8d8", "Still visible"), ("white", "Just gone")),
                            "white", 110, 64))
        slider = QSlider(Qt.Orientation.Horizontal)
        if which == "tint":
            slider.setRange(-TINT_RANGE, TINT_RANGE)
        else:
            slider.setRange(MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        value = QLabel()
        value.setMinimumWidth(44)
        grid = QGridLayout()
        grid.addWidget(QLabel(which.capitalize()), 0, 0)
        grid.addWidget(QLabel(ends[0]), 0, 1)
        grid.addWidget(slider, 0, 2)
        grid.addWidget(QLabel(ends[1]), 0, 3)
        grid.addWidget(value, 0, 4)
        lay.addLayout(grid)
        idx = 0 if which == "tint" else 1

        def changed(v):
            value.setText((f"{v:+d}°" if v else "0°") if which == "tint" else f"{v}%")
            if self.state:
                self.state[key][idx] = v
                self._emit(key, self.color(key).name())
                self.update_targets()
        slider.valueChanged.connect(changed)
        self.sliders[key, which] = slider
        reset = QPushButton("Reset to Default")
        reset.setAutoDefault(False)
        reset.clicked.connect(lambda: slider.setValue(
            round(eye_settings(EYES[key][1], QColor(DEFAULTS[key]).getRgbF()[:3])[idx])))
        lay.addWidget(reset, alignment=Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        self.pages.addWidget(page)

    def _ghost_page(self, step):
        page, lay = _page(
            f"<h3>Step {step} of {STEPS}: Ghosting</h3>"
            "<p>Now each pattern should vanish into the <b>black</b> through the other lens:</p>"
            "<ul><li>Through the <b>red</b> lens only: the cyan pattern, on the right.</li>"
            "<li>Through the <b>cyan</b> lens only: the red pattern, on the left.</li></ul>"
            "<p>Whatever still shows is ghosting, the other eye's picture leaking through. "
            "Go Back and lower that colour's Brightness, or move its Tint, until it fades, "
            "so long as it still disappears on its own screen.</p>")
        row = QHBoxLayout()
        row.addStretch(1)
        for key in EYES:
            row.addWidget(_Target("black", lambda k=key: self.color(k), 200, 120))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel("Through the other lens, ghosting looks like:"))
        lay.addLayout(_refs((("#5a5a5a", "Strong"), ("#262626", "Some"), ("black", "None")),
                            "black", 90, 54))
        lay.addStretch(1)
        self.pages.addWidget(page)

    def _final_page(self, step):
        page, lay = _page(
            f"<h3>Step {step} of {STEPS}: Try it</h3>"
            "<p>Look with <b>both</b> eyes. The ring should float in front of the frame as one "
            "clean shape, with no faint second outline beside it.</p>")
        self.compare = QCheckBox("Compare with plain red and cyan")
        self.preview = _Preview(self._preview_colors)
        self.compare.toggled.connect(self.preview.update)
        lay.addWidget(self.preview, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.compare, alignment=Qt.AlignmentFlag.AlignHCenter)
        note = QLabel("Done keeps these colours for Grayscale Anaglyph (View ▸ Stereo).")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        self.pages.addWidget(page)

    # -- state -------------------------------------------------------------

    def color(self, key) -> QColor:
        tint, bright = self.state.get(key, (0, 100))
        return QColor.fromRgbF(*eye_color(EYES[key][1], tint, bright))

    def _preview_colors(self):
        keys = (LEFT_KEY, RIGHT_KEY)
        if self.compare.isChecked():
            return tuple(QColor(DEFAULTS[k]).getRgbF()[:3] for k in keys)
        return tuple(self.color(k).getRgbF()[:3] for k in keys)

    def reset_all(self):
        """Both eyes back to the theoretical colours, pure red and cyan.
        Saved at once, like any slider; Cancel still undoes it."""
        for (key, which), s in self.sliders.items():
            s.setValue(round(eye_settings(EYES[key][1], QColor(DEFAULTS[key]).getRgbF()[:3])
                             [0 if which == "tint" else 1]))

    def update_targets(self):
        for t in self.findChildren(QWidget):
            if isinstance(t, (_Target, _Preview)):
                t.update()

    def _load(self):
        """Start over from what is saved now, at the first screen."""
        from belfryscad.window.preferences import load_preference
        self._original = {k: load_preference(k) for k in DEFAULTS}
        self.state = {}
        settings = {k: [round(v) for v in eye_settings(EYES[k][1], QColor(c).getRgbF()[:3])]
                    for k, c in self._original.items()}
        for (key, which), s in self.sliders.items():
            s.setValue(settings[key][0 if which == "tint" else 1])
        self.state = settings
        self.compare.setChecked(False)
        self.pages.setCurrentIndex(0)
        self._go(0)
        self.update_targets()

    def showEvent(self, event):
        if not event.spontaneous():      # opened, not un-minimised
            self._load()
        super().showEvent(event)
        self.next.setFocus()

    def _go(self, step):
        i = self.pages.currentIndex() + step
        if i >= self.pages.count():
            self.accept()
            return
        self.pages.setCurrentIndex(i)
        self.back.setEnabled(i > 0)
        self.next.setText("Done" if i == self.pages.count() - 1 else "Next")

    def reject(self):
        for key, value in self._original.items():
            self._emit(key, value)
        super().reject()
