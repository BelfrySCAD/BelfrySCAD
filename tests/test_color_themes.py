"""Tests for viewport color theme data."""
from belfryscad.window.color_themes import COLOR_THEMES, DEFAULT_COLOR_THEME


def test_default_theme_is_in_table():
    assert DEFAULT_COLOR_THEME in COLOR_THEMES


def test_every_theme_has_background_object_and_axes_rgba():
    for name, theme in COLOR_THEMES.items():
        for key in ("background", "object", "axes"):
            assert key in theme, f"{name} missing {key}"
            r, g, b, a = theme[key]
            assert all(0.0 <= c <= 1.0 for c in (r, g, b, a)), f"{name}.{key} out of range"


def test_cornfield_axes_color_is_black():
    assert COLOR_THEMES["Cornfield"]["axes"] == (0.0, 0.0, 0.0, 1.0)
