"""Viewport color themes: built-in themes named for OpenSCAD's colour
schemes (the names are interface: --colorscheme, docsgen ColorScheme), plus
user-creatable custom themes layered on top (see `all_themes`)."""
import json

# CLEAN-ROOM: every background/object/axes value below is a placeholder;
# choose real colours from spec section 2.
_PLACEHOLDER = (0.0, 0.0, 0.0, 1.0)


COLOR_THEMES = {
    'BeforeDawn': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'ClearSky': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (1.0, 0.5, 0.0, 1.0)
    },
    'Cornfield': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Daylight Gem': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0),
    },
    'DeepOcean': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Metallic': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Nature': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
    'Nocturnal Gem': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
    'Solarized': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Starnight': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Sunset': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (0.0, 0.9, 0.9, 1.0)
    },
    'Tomorrow Night': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
        "unselected_vertex": (1.0, 0.75, 0.0, 1.0)
    },
    'Tomorrow': {
        "background": _PLACEHOLDER,  # CLEAN-ROOM
        "object": _PLACEHOLDER,  # CLEAN-ROOM
        "axes": _PLACEHOLDER,  # CLEAN-ROOM
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
