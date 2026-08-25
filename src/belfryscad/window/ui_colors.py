"""Appearance-aware colours for the chrome that paints itself.

Most widgets follow the system palette without help. A few don't, because
they paint or style themselves with fixed colours, and every one of those
was picked against a light appearance -- so on a Mac in Dark Mode they came
out illegible: a light-grey gutter, near-white table headers, and a bright
yellow debugger highlight, all against dark surroundings.

`is_dark()` reads the *application palette* rather than
`QStyleHints.colorScheme()` on purpose: it stays correct if the app (or a
test) sets its own palette, which the colour-scheme hint does not reflect.

Callers that paint should ask at paint time rather than caching the answer.
Anything that *can't* -- a stylesheet, a QIcon already handed to an action
-- registers with `on_appearance_change()` and is rebuilt when the user
switches between light and dark mid-session.
"""
import weakref

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

# Every SVG under resources/icons is drawn in this one ink, for a light
# toolbar. Checked across the whole set: the only rendered colours are
# `#444`/`#444444` and `fill="none"`, so a flat substitution recolours them
# all without touching anything else. (`#ffffff`/`#d1d1d1`/`#111111` also
# appear, but only inside Inkscape's <sodipodi:namedview>, which is
# metadata and never rendered.)
_ICON_INK = ("#444444", "#444")


def is_dark() -> bool:
    """True when the application palette is a dark one."""
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def gutter_colors() -> tuple[str, str]:
    """(background, line-number text) for the editor's line number area."""
    return ("#3A3A3A", "#FFFFFF") if is_dark() else ("#CCCCCC", "#000000")


def fold_arrow_color() -> str:
    """The gutter's fold triangles, which sit on `gutter_colors()[0]`."""
    return "#B0B0B0" if is_dark() else "#606060"


def execution_line_color() -> str:
    """Background of the debugger's "paused here" line in the editor.

    Dark mode gets a dark amber rather than the light theme's pale yellow:
    the syntax colours painted over it are light, and they washed out
    against a bright fill.

    The shade is a measured balance, not a guess. Darker reads the code
    better but sinks the band into the editor background, and a highlight
    nobody can see defeats the point. Contrast against the six syntax
    colours (worst case) and against the editor's own #252526:

        #5A4A00   text 2.61   band 1.76
        #4A3D00   text 3.22   band 1.43   <- chosen
        #3D3200   text 3.81   band 1.21
        #332A00   text 4.28   band 1.07   band invisible

    For reference the light theme's #FFFF88 scores 1.41 on text, so every
    one of these is an improvement on the bar already in use.
    """
    return "#4A3D00" if is_dark() else "#FFFF88"


def guide_colors() -> tuple[str, str]:
    """(indent guides, column guide) for the editor's overlay rules.

    These are meant to be barely there -- a hint of structure, not a set of
    lines competing with the code. Hardcoded near-white values measured
    1.32:1 against a white page but 13.6:1 against the dark editor
    background (#171717), which is where "too bright" came from: ten times
    louder than the same lines are in light mode.

    The dark values are chosen to land at the SAME contrast ratio the light
    ones have, rather than picked by eye.
    """
    return ("#2D2D2D", "#303030") if is_dark() else ("#E0E0E0", "#DDDDDD")


def find_bar_bg() -> str:
    """Background for the editor's floating Find/Replace overlay.

    It has to be opaque and distinct from the code behind it, which is why
    the bar sets a Window colour at all rather than inheriting one -- but
    the value must follow the theme. Hardcoded light grey here meant white
    theme text on an off-white bar in dark mode, i.e. invisible.
    """
    return "#3A3A3A" if is_dark() else "#F3F3F3"


def find_match_colors() -> tuple[str, str, str, str]:
    """(current bg, current fg, other bg, other fg) for Find match highlights.

    Both foregrounds are set explicitly and are theme-INDEPENDENT: these
    backgrounds are pale amber in either theme, so text that follows the
    palette turns white-on-amber the moment the app goes dark. Black on
    amber reads in both (and beats the old white-on-#FF9900, which was
    around 2.3:1 even in light mode).
    """
    return ("#FF9900", "#000000", "#FFE080", "#000000")


def find_no_match_colors() -> tuple[str, str]:
    """(background, foreground) for the Find field when nothing matches.

    A pair, not just a background: the palette's own text is white in dark
    mode and would vanish into the tint. Dark mode inverts the relationship
    rather than reusing the pale light-mode red -- a deep red with pale text
    reads as "error" in a dark UI, where black on a dark red measured only
    3.7:1.
    """
    return ("#7A2626", "#FFD5D5") if is_dark() else ("#FFCCCC", "#000000")


def header_colors() -> tuple[str, str, str]:
    """(background, border, text) for QHeaderView sections."""
    if is_dark():
        return ("#3A3A3A", "#555555", "#E0E0E0")
    return ("#E8E8E8", "#C0C0C0", "#202020")


def console_severity_colors(kind: str) -> tuple[str, str]:
    """(background, foreground) for a console line banded by severity.

    `kind` is "error" or "warning". Both foregrounds are explicit for the
    same reason find_match_colors' are: text that follows the palette turns
    near-white the moment the app goes dark, and near-white on a pale band
    is unreadable.

    The bands are theme-AWARE rather than one colour for both, because a
    saturated band that reads on white glares on near-black and vice versa.

    Chosen by measuring, not by eye. Text contrast lands between 8.7:1 and
    16.1:1, all far past WCAG AA. The number that actually needed tuning is
    the band's separation from the console's own background -- a band you
    cannot see is not a warning. Light-mode yellow started at #FFF0B0 and
    was only 1.14:1 against white, so it went to #FFE066 at 1.30:1, which
    also matches the red band's 1.33:1 so neither severity looks louder
    than the other by accident.
    """
    if kind == "error":
        return ("#5A1F1F", "#FFD5D5") if is_dark() else ("#FFD6D6", "#000000")
    return ("#4F4008", "#FFEDB0") if is_dark() else ("#FFE066", "#000000")


def text_color() -> str:
    """The palette's normal foreground, as a hex string.

    For QTextDocument content specifically: a document's default character
    format is **black whatever the palette says** -- measured, not assumed,
    with the palette reporting #e0e0e0 text on a #252526 base while every
    block still resolved to #000000. Anything inserting into a document has
    to set the colour itself.
    """
    app = QApplication.instance()
    if app is None:
        return "#000000"
    return app.palette().color(QPalette.ColorRole.Text).name()


def icon_ink() -> str:
    """Ink for the SVG icon set, matched to the appearance.

    #D0D0D0 is not an eyeballed "light grey": it puts the icons at 8.93
    contrast on a #2D2D2D dark toolbar, against the 8.24 that the assets'
    own #444444 scores on a #ECECEC light one. Same perceived weight,
    rather than the harsher pure white.
    """
    return "#D0D0D0" if is_dark() else _ICON_INK[0]


def themed_icon(path, size: int = 128) -> QIcon:
    """An SVG icon recoloured for the current appearance.

    Light mode returns the file untouched -- the assets were drawn for it,
    so there is nothing to correct and no risk taken. Dark mode rewrites
    the ink in the SVG *source* before rendering, which keeps the shapes
    vector-clean and antialiased against the new colour; recolouring the
    rendered pixmap instead (what viewport.py does for its always-white
    overlay icons) fringes the edges with the old ink.

    Rendered once at `size` and left for Qt to scale down to whatever the
    toolbar asks for; 128 downsamples cleanly to the 20pt icons in use,
    Retina included.

    The returned icon is a snapshot of the appearance at call time. Use
    `apply_themed_icon()` instead wherever the icon outlives the call, so
    it gets rebuilt when the appearance changes.
    """
    from PySide6.QtSvg import QSvgRenderer

    path = str(path)
    if not is_dark():
        return QIcon(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            svg = f.read()
    except OSError:
        return QIcon(path)

    ink = icon_ink()
    for old in _ICON_INK:
        svg = svg.replace(f'"{old}"', f'"{ink}"').replace(f":{old}", f":{ink}")

    renderer = QSvgRenderer(svg.encode("utf-8"))
    if not renderer.isValid():
        return QIcon(path)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


# -- live appearance switching -------------------------------------------
#
# Which Qt signal to hang this on was measured, not assumed:
#
#   * An event filter installed on the QApplication DOES receive
#     `ApplicationPaletteChange` addressed to the application object, so a
#     single central hook covers the whole app -- no per-widget plumbing.
#   * A widget's own `changeEvent()` sees `PaletteChange`, NOT
#     `ApplicationPaletteChange`. Overriding changeEvent and testing for the
#     latter, the obvious-looking approach, silently never fires.
#   * `QStyleHints.colorSchemeChanged` exists and is connected below as
#     well, because it is what a real macOS light/dark switch emits --
#     `setPalette()` alone does not trigger it, so it cannot be the only
#     hook either.
#
# Both paths run the same callbacks, and re-theming is idempotent, so
# whichever arrives first (or both) lands on the same result.

class _AppearanceDispatcher(QObject):
    def __init__(self):
        super().__init__()
        self._entries: list[tuple[weakref.ref, object]] = []
        app = QApplication.instance()
        app.installEventFilter(self)
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _scheme: self.dispatch())

    def add(self, owner, callback):
        self._entries.append((weakref.ref(owner), callback))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.ApplicationPaletteChange:
            self.dispatch()
        return False

    def dispatch(self):
        import shiboken6

        live = []
        for ref, callback in self._entries:
            owner = ref()
            # Two different deaths to survive: the Python object being
            # garbage collected (ref() is None), and the C++ widget being
            # destroyed while the Python wrapper lives on, which makes any
            # call into it raise RuntimeError.
            if owner is None or not shiboken6.isValid(owner):
                continue
            live.append((ref, callback))
            try:
                callback()
            except RuntimeError:
                pass
        self._entries = live


_dispatcher: _AppearanceDispatcher | None = None


def on_appearance_change(owner, callback) -> None:
    """Run `callback()` whenever the app switches between light and dark.

    `owner` only decides lifetime -- the registration is dropped once it is
    gone, so callers do not have to unregister. Call the callback yourself
    for the initial paint; this only handles later changes.
    """
    global _dispatcher
    if QApplication.instance() is None:
        return
    if _dispatcher is None:
        _dispatcher = _AppearanceDispatcher()
    _dispatcher.add(owner, callback)


def apply_themed_icon(target, path) -> None:
    """`target.setIcon()` from `path` now, and again on every appearance
    change -- a QIcon is a value with no owner to notify, so whatever holds
    it has to be re-set.

    Safe to call repeatedly on the same target: the path is stored on it and
    the callback re-reads it, so a button that swaps its icon at runtime
    (continue/pause, play/pause) tracks the swap. Registering a fresh
    callback per call would instead pile up stale ones that fight over the
    button, each restoring whichever icon it captured.
    """
    target._themed_icon_path = str(path)

    def apply():
        target.setIcon(themed_icon(target._themed_icon_path))

    apply()
    if not getattr(target, "_themed_icon_hooked", False):
        target._themed_icon_hooked = True
        on_appearance_change(target, apply)
