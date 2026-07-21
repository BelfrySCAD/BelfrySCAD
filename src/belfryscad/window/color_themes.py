"""Viewport color themes, derived from OpenSCAD's built-in render color
schemes (`background`, `cgal-face-front`, and `axes-color` colors), plus
user-creatable custom schemes layered on top (see `all_schemes`)."""
import json

# "unselected_vertex" (color for a viewer/editor's not-currently-selected
# vertex markers, e.g. data_viewers.py's PathViewer/GridViewer/VNFViewer/
# RegionViewer) has no OpenSCAD equivalent to derive from, so every
# built-in theme defaults to the same cyan rather than a hand-picked
# per-theme value.
_DEFAULT_VERTEX_COLOR = (0.0, 0.9, 0.9, 1.0)

COLOR_THEMES = {
    'BeforeDawn': {"background": (0.2, 0.2, 0.2, 1.0), "object": (0.8, 0.8, 0.8, 1.0), "axes": (0.7569, 0.7569, 0.7569, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'ClearSky': {"background": (0.5294, 0.8078, 0.9216, 1.0), "object": (1.0, 0.9255, 0.3686, 1.0), "axes": (0.0, 0.0, 0.0, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Cornfield': {"background": (1.0, 1.0, 0.898, 1.0), "object": (0.9765, 0.8431, 0.1725, 1.0), "axes": (0.0, 0.0, 0.0, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Daylight Gem': {"background": (0.9412, 0.9412, 0.9412, 1.0), "object": (0.0078, 0.8549, 0.9686, 1.0), "axes": (0.2157, 0.2235, 0.2784, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'DeepOcean': {"background": (0.2, 0.2, 0.2, 1.0), "object": (0.9333, 0.9333, 0.9333, 1.0), "axes": (0.7569, 0.7569, 0.7569, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Metallic': {"background": (0.6667, 0.6667, 1.0, 1.0), "object": (0.8667, 0.8667, 1.0, 1.0), "axes": (0.1333, 0.1333, 0.2, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Nature': {"background": (0.9804, 0.9804, 0.9804, 1.0), "object": (0.0863, 0.6275, 0.5216, 1.0), "axes": (0.1961, 0.1961, 0.1961, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Nocturnal Gem': {"background": (0.0471, 0.0471, 0.0471, 1.0), "object": (0.0078, 0.8549, 0.9686, 1.0), "axes": (0.6549, 0.6627, 0.7176, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Solarized': {"background": (0.9922, 0.9647, 0.8902, 1.0), "object": (0.7098, 0.5333, 0.0, 1.0), "axes": (0.098, 0.0941, 0.0863, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Starnight': {"background": (0.0, 0.0, 0.0, 1.0), "object": (1.0, 1.0, 0.8784, 1.0), "axes": (0.898, 0.898, 0.898, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Sunset': {"background": (0.6667, 0.2667, 0.2667, 1.0), "object": (1.0, 0.6667, 0.6667, 1.0), "axes": (0.1333, 0.051, 0.051, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Tomorrow Night': {"background": (0.1137, 0.1216, 0.1294, 1.0), "object": (0.5412, 0.7451, 0.7176, 1.0), "axes": (0.9098, 0.9098, 0.9098, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
    'Tomorrow': {"background": (0.9725, 0.9725, 0.9725, 1.0), "object": (0.2431, 0.6, 0.6235, 1.0), "axes": (0.0941, 0.0941, 0.0941, 1.0), "unselected_vertex": _DEFAULT_VERTEX_COLOR},
}

DEFAULT_COLOR_THEME = "Cornfield"

SCHEME_COLOR_KEYS = ("background", "object", "axes", "unselected_vertex")


def load_custom_schemes() -> dict:
    """User-created color schemes, persisted as one JSON blob under the
    `colorSchemes/custom` preference key (deferred import of `preferences`
    to avoid a circular import -- `preferences.py` already imports
    `COLOR_THEMES`/`DEFAULT_COLOR_THEME` from this module at load time)."""
    from belfryscad.window.preferences import load_preference
    raw = load_preference("colorSchemes/custom")
    try:
        schemes = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return {
        name: {k: tuple(v[k]) for k in SCHEME_COLOR_KEYS}
        for name, v in schemes.items()
    }


def save_custom_schemes(schemes: dict) -> None:
    """Persist `schemes` (same shape as `load_custom_schemes` returns) back
    to the `colorSchemes/custom` preference key."""
    from belfryscad.window.preferences import save_preferences
    encoded = {
        name: {k: list(v[k]) for k in SCHEME_COLOR_KEYS}
        for name, v in schemes.items()
    }
    save_preferences({"colorSchemes/custom": json.dumps(encoded)})


def all_schemes() -> dict:
    """Built-in themes merged with custom ones, for display/lookup. Custom
    scheme names are kept unique against this merged set at creation time
    (see `unique_scheme_name`), so a name collision here should never
    actually happen -- if it somehow did, the built-in wins, since it's
    the one thing a caller can always rely on existing."""
    return {**load_custom_schemes(), **COLOR_THEMES}


def is_builtin(name: str) -> bool:
    return name in COLOR_THEMES


def unique_scheme_name(base: str, existing: dict) -> str:
    """`base` if it's not already taken in `existing`, else `base` with an
    incrementing ` 2`, ` 3`, ... suffix until it is."""
    if base not in existing:
        return base
    n = 2
    while f"{base} {n}" in existing:
        n += 1
    return f"{base} {n}"
