"""Colour policy for the chrome that paints itself.

Deliberately Qt-*widget*-free: constructing real widgets under pytest is
what this repo keeps out of the suite (see tests/ conventions), so the
live-switching behaviour is checked with throwaway scripts instead. What is
worth pinning here is the policy -- that light mode is untouched, that dark
mode actually differs, and that both stay legible -- which needs nothing
but `is_dark()` stubbed out.
"""
import pytest

from belfryscad.window import ui_colors


@pytest.fixture
def appearance(monkeypatch):
    def use(dark: bool):
        monkeypatch.setattr(ui_colors, "is_dark", lambda: dark)
    return use


def _contrast(a: str, b: str) -> float:
    """WCAG relative-luminance contrast ratio between two #rrggbb strings."""
    def lum(h):
        h = h.lstrip("#")
        parts = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
        chans = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                 for c in parts]
        return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# --- light mode must not move -----------------------------------------
# These are the values the app shipped with. Dark mode was the bug; light
# mode was fine, and a "fix" that restyles it is a regression.
def test_light_mode_keeps_its_original_colors(appearance):
    appearance(False)
    assert ui_colors.gutter_colors() == ("#CCCCCC", "#000000")
    assert ui_colors.execution_line_color() == "#FFFF88"
    assert ui_colors.header_colors()[0] == "#E8E8E8"
    assert ui_colors.icon_ink() == "#444444"


# --- dark mode actually differs ---------------------------------------
@pytest.mark.parametrize("fn", [
    "gutter_colors", "execution_line_color", "header_colors", "icon_ink",
])
def test_dark_mode_differs_from_light(fn, appearance):
    appearance(False)
    light = getattr(ui_colors, fn)()
    appearance(True)
    assert getattr(ui_colors, fn)() != light


# --- legibility -------------------------------------------------------
def test_gutter_text_is_legible_in_both_appearances(appearance):
    for dark in (True, False):
        appearance(dark)
        bg, fg = ui_colors.gutter_colors()
        assert _contrast(bg, fg) >= 7.0, (dark, bg, fg)


def test_header_text_is_legible_in_both_appearances(appearance):
    for dark in (True, False):
        appearance(dark)
        bg, _border, fg = ui_colors.header_colors()
        assert _contrast(bg, fg) >= 7.0, (dark, bg, fg)


def test_dark_gutter_is_dark_and_light_gutter_is_light(appearance):
    appearance(True)
    dark_bg, dark_fg = ui_colors.gutter_colors()
    appearance(False)
    light_bg, light_fg = ui_colors.gutter_colors()
    # The reported ask: white text on a dark grey background.
    assert _contrast(dark_bg, "#000000") < _contrast(light_bg, "#000000")
    assert dark_fg == "#FFFFFF"


# The execution-line highlight is a genuine trade-off -- darker reads the
# code better but sinks the band into the editor background. Both ends are
# pinned so neither can be tuned away by accident.
_SYNTAX = ("#569CD6", "#4EC9B0", "#CE9178", "#6A9955", "#C586C0", "#D4D4D4")
_EDITOR_DARK_BG = "#252526"


def test_dark_execution_line_beats_the_light_baseline_on_text(appearance):
    appearance(False)
    light_worst = min(_contrast(ui_colors.execution_line_color(), s)
                      for s in _SYNTAX)
    appearance(True)
    dark_worst = min(_contrast(ui_colors.execution_line_color(), s)
                     for s in _SYNTAX)
    assert dark_worst > light_worst


def test_dark_execution_line_is_still_visible_as_a_band(appearance):
    appearance(True)
    band = _contrast(ui_colors.execution_line_color(), _EDITOR_DARK_BG)
    # A highlight nobody can pick out of the background is not a highlight.
    assert band >= 1.35, band


def test_dark_icon_ink_matches_the_light_weight(appearance):
    appearance(True)
    dark = _contrast(ui_colors.icon_ink(), "#2D2D2D")
    appearance(False)
    light = _contrast(ui_colors.icon_ink(), "#ECECEC")
    # Same perceived weight as the assets have on a light toolbar, rather
    # than pure white, which reads as harsh.
    assert abs(dark - light) < 1.5, (dark, light)


def test_no_application_falls_back_to_light(monkeypatch):
    """is_dark() must not explode when there is no QApplication -- the
    headless CLI imports nothing from here, but nothing should depend on
    that staying true."""
    monkeypatch.setattr(ui_colors.QApplication, "instance", staticmethod(lambda: None))
    assert ui_colors.is_dark() is False
    assert ui_colors.text_color() == "#000000"
