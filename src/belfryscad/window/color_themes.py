"""Viewport color themes: built-in themes named for OpenSCAD's colour
schemes (the names are interface: --colorscheme, docsgen ColorScheme), plus
user-creatable custom themes layered on top (see `all_themes`)."""
import json

# Theme names match OpenSCAD's colour schemes so --colorscheme and docsgen's
# ColorScheme= accept the same words; the colours are BelfrySCAD's own,
# except Solarized and Tomorrow/Tomorrow Night, which use their MIT-licensed
# originals (see resources/THIRD_PARTY_NOTICES.md).


COLOR_THEMES = {
    'BeforeDawn': {
        "background": (0.1059, 0.1333, 0.2196, 1.0),  # #1b2238
        "object": (0.9098, 0.6627, 0.6275, 1.0),  # #e8a9a0
        "axes": (0.7216, 0.7529, 0.8471, 1.0),  # #b8c0d8
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'ClearSky': {
        "background": (0.8627, 0.9255, 0.9804, 1.0),  # #dcecfa
        "object": (0.3098, 0.4863, 0.6745, 1.0),  # #4f7cac
        "axes": (0.1216, 0.1765, 0.2392, 1.0),  # #1f2d3d
        "unselected_vertex": (1.0, 0.5, 0.0, 1.0)
    },
    'Cornfield': {
        "background": (0.9686, 0.9529, 0.8902, 1.0),  # #f7f3e3
        "object": (0.8627, 0.698, 0.3059, 1.0),  # #dcb24e
        "axes": (0.0, 0.0, 0.0, 1.0),  # #000000
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Daylight Gem': {
        "background": (0.9569, 0.9647, 0.9725, 1.0),  # #f4f6f8
        "object": (0.1804, 0.6196, 0.3569, 1.0),  # #2e9e5b
        "axes": (0.1686, 0.1843, 0.2118, 1.0),  # #2b2f36
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0),
    },
    'DeepOcean': {
        "background": (0.0431, 0.1333, 0.2118, 1.0),  # #0b2236
        "object": (0.9098, 0.5412, 0.4314, 1.0),  # #e88a6e
        "axes": (0.6627, 0.7686, 0.8392, 1.0),  # #a9c4d6
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Metallic': {
        "background": (0.2275, 0.2471, 0.2745, 1.0),  # #3a3f46
        "object": (0.7059, 0.7373, 0.7765, 1.0),  # #b4bcc6
        "axes": (0.8902, 0.9059, 0.9255, 1.0),  # #e3e7ec
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Nature': {
        "background": (0.9333, 0.9529, 0.8863, 1.0),  # #eef3e2
        "object": (0.3725, 0.5608, 0.2431, 1.0),  # #5f8f3e
        "axes": (0.149, 0.2, 0.1098, 1.0),  # #26331c
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
    'Nocturnal Gem': {
        "background": (0.0784, 0.0863, 0.1216, 1.0),  # #14161f
        "object": (0.1804, 0.6196, 0.3569, 1.0),  # #2e9e5b
        "axes": (0.7882, 0.8, 0.8392, 1.0),  # #c9ccd6
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
    # Solarized dark (Ethan Schoonover, MIT, https://ethanschoonover.com/solarized/):
    # base03 background, base0 body text for axes, cyan accent for objects.
    'Solarized': {
        "background": (0.0, 0.1686, 0.2118, 1.0),  # #002b36
        "object": (0.1647, 0.6314, 0.5961, 1.0),  # #2aa198
        "axes": (0.5137, 0.5804, 0.5882, 1.0),  # #839496
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
    'Starnight': {
        "background": (0.051, 0.0627, 0.1255, 1.0),  # #0d1020
        "object": (0.902, 0.8863, 0.7647, 1.0),  # #e6e2c3
        "axes": (0.7176, 0.7412, 0.8392, 1.0),  # #b7bdd6
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Sunset': {
        "background": (0.9647, 0.7843, 0.6235, 1.0),  # #f6c89f
        "object": (0.3529, 0.2275, 0.4314, 1.0),  # #5a3a6e
        "axes": (0.2275, 0.1216, 0.1686, 1.0),  # #3a1f2b
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    # Tomorrow Night (Chris Kempson, MIT, https://github.com/chriskempson/tomorrow-theme):
    # background, foreground for axes, yellow accent for objects.
    'Tomorrow Night': {
        "background": (0.1137, 0.1216, 0.1294, 1.0),  # #1d1f21
        "object": (0.9412, 0.7765, 0.4549, 1.0),  # #f0c674
        "axes": (0.7725, 0.7843, 0.7765, 1.0),  # #c5c8c6
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    # Tomorrow (Chris Kempson, MIT, https://github.com/chriskempson/tomorrow-theme):
    # background, foreground for axes, aqua accent for objects.
    'Tomorrow': {
        "background": (1.0, 1.0, 1.0, 1.0),  # #ffffff
        "object": (0.2431, 0.6, 0.6235, 1.0),  # #3e999f
        "axes": (0.302, 0.302, 0.298, 1.0),  # #4d4d4c
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
}

DEFAULT_COLOR_THEME = "Cornfield"

THEME_COLOR_KEYS = ("background", "object", "axes", "unselected_vertex")


def load_custom_themes() -> dict:
    """User-created color themes, persisted as one JSON blob under the
    `colorThemes/custom` preference key (deferred import of `preferences`
    to avoid a circular import -- `preferences.py` already imports
    `COLOR_THEMES`/`DEFAULT_COLOR_THEME` from this module at load time)."""
    from belfryscad.window.preferences import load_preference
    raw = load_preference("colorThemes/custom")
    try:
        themes = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return {
        name: {k: tuple(v[k]) for k in THEME_COLOR_KEYS}
        for name, v in themes.items()
    }


def save_custom_themes(themes: dict) -> None:
    """Persist `themes` (same shape as `load_custom_themes` returns) back
    to the `colorThemes/custom` preference key."""
    from belfryscad.window.preferences import save_preferences
    encoded = {
        name: {k: list(v[k]) for k in THEME_COLOR_KEYS}
        for name, v in themes.items()
    }
    save_preferences({"colorThemes/custom": json.dumps(encoded)})


def all_themes() -> dict:
    """Built-in themes merged with custom ones, for display/lookup. Custom
    theme names are kept unique against this merged set at creation time
    (see `unique_theme_name`), so a name collision here should never
    actually happen -- if it somehow did, the built-in wins, since it's
    the one thing a caller can always rely on existing."""
    return {**load_custom_themes(), **COLOR_THEMES}


def is_builtin(name: str) -> bool:
    return name in COLOR_THEMES


def unique_theme_name(base: str, existing: dict) -> str:
    """`base` if it's not already taken in `existing`, else `base` with an
    incrementing ` 2`, ` 3`, ... suffix until it is."""
    if base not in existing:
        return base
    n = 2
    while f"{base} {n}" in existing:
        n += 1
    return f"{base} {n}"
