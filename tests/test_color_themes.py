"""Tests for viewport color theme data, plus the custom "color theme"
layer built on top (all_themes/load_custom_themes/save_custom_themes/
is_builtin/unique_theme_name). The persistence helpers round-trip through
real `QSettings` (via `preferences.load_preference`/`save_preferences`),
so any test that touches them uses the `isolated_settings` fixture below
to point `QSettings` at a temp INI file instead of the developer's actual
saved app preferences -- confirmed via search that no other test in this
suite touches QSettings/preferences.py, so this fixture is new territory,
not an existing convention being reused."""
import pytest
from PySide6.QtCore import QSettings

from belfryscad.window.color_themes import (
    COLOR_THEMES, DEFAULT_COLOR_THEME, THEME_COLOR_KEYS, all_themes,
    is_builtin, load_custom_themes, save_custom_themes, unique_theme_name,
)


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect every `QSettings("BelfrySCAD", "BelfrySCAD")` constructed
    inside `preferences.py` to a temp INI file for the duration of the
    test, so custom-theme round-trip tests never read/write the real
    developer machine's saved preferences."""
    ini_path = str(tmp_path / "test_settings.ini")

    def _fake_qsettings(*args, **kwargs):
        return QSettings(ini_path, QSettings.Format.IniFormat)

    monkeypatch.setattr("belfryscad.window.preferences.QSettings", _fake_qsettings)


def test_default_theme_is_in_table():
    assert DEFAULT_COLOR_THEME in COLOR_THEMES


def test_every_theme_has_all_four_color_keys_as_valid_rgba():
    for name, theme in COLOR_THEMES.items():
        for key in THEME_COLOR_KEYS:
            assert key in theme, f"{name} missing {key}"
            r, g, b, a = theme[key]
            assert all(0.0 <= c <= 1.0 for c in (r, g, b, a)), f"{name}.{key} out of range"


def test_cornfield_axes_color_is_black():
    assert COLOR_THEMES["Cornfield"]["axes"] == (0.0, 0.0, 0.0, 1.0)


class TestIsBuiltin:
    def test_builtin_name_is_true(self):
        assert is_builtin(DEFAULT_COLOR_THEME) is True

    def test_unknown_name_is_false(self):
        assert is_builtin("Definitely Not A Real Theme") is False


class TestUniqueThemeName:
    def test_returns_base_when_free(self):
        assert unique_theme_name("Foo", {"Bar": {}}) == "Foo"

    def test_appends_incrementing_suffix_on_collision(self):
        existing = {"Foo": {}, "Foo 2": {}}
        assert unique_theme_name("Foo", existing) == "Foo 3"

    def test_first_collision_gets_suffix_2(self):
        assert unique_theme_name("Foo", {"Foo": {}}) == "Foo 2"


class TestCustomThemePersistence:
    def test_no_custom_themes_by_default(self, isolated_settings):
        assert load_custom_themes() == {}

    def test_round_trips_through_save_and_load(self, isolated_settings):
        theme = {
            "background": (0.1, 0.2, 0.3, 1.0),
            "object": (0.4, 0.5, 0.6, 1.0),
            "axes": (0.7, 0.8, 0.9, 1.0),
            "unselected_vertex": (1.0, 0.0, 0.0, 1.0),
        }
        save_custom_themes({"My Theme": theme})
        loaded = load_custom_themes()
        assert loaded == {"My Theme": theme}

    def test_overwrites_previous_save(self, isolated_settings):
        save_custom_themes({"A": {k: (0.0, 0.0, 0.0, 1.0) for k in THEME_COLOR_KEYS}})
        save_custom_themes({"B": {k: (1.0, 1.0, 1.0, 1.0) for k in THEME_COLOR_KEYS}})
        assert load_custom_themes() == {"B": {k: (1.0, 1.0, 1.0, 1.0) for k in THEME_COLOR_KEYS}}


class TestAllThemes:
    def test_includes_every_builtin(self, isolated_settings):
        merged = all_themes()
        for name in COLOR_THEMES:
            assert name in merged
            assert merged[name] == COLOR_THEMES[name]

    def test_includes_custom_themes(self, isolated_settings):
        custom = {"My Theme": {k: (0.2, 0.2, 0.2, 1.0) for k in THEME_COLOR_KEYS}}
        save_custom_themes(custom)
        merged = all_themes()
        assert merged["My Theme"] == custom["My Theme"]

    def test_builtin_wins_on_name_collision(self, isolated_settings):
        # Custom-theme creation is expected to enforce uniqueness against
        # all_themes() before ever reaching save_custom_themes, but this
        # confirms the merge itself is defensive if that were bypassed.
        colliding = {DEFAULT_COLOR_THEME: {k: (0.0, 0.0, 0.0, 1.0) for k in THEME_COLOR_KEYS}}
        save_custom_themes(colliding)
        assert all_themes()[DEFAULT_COLOR_THEME] == COLOR_THEMES[DEFAULT_COLOR_THEME]
