"""Viewport color themes, derived from OpenSCAD's built-in render color
schemes (`background`, `cgal-face-front`, and `axes-color` colors)."""

COLOR_THEMES = {
    'BeforeDawn': {"background": (0.2, 0.2, 0.2, 1.0), "object": (0.8, 0.8, 0.8, 1.0), "axes": (0.7569, 0.7569, 0.7569, 1.0)},
    'ClearSky': {"background": (0.5294, 0.8078, 0.9216, 1.0), "object": (1.0, 0.9255, 0.3686, 1.0), "axes": (0.0, 0.0, 0.0, 1.0)},
    'Cornfield': {"background": (1.0, 1.0, 0.898, 1.0), "object": (0.9765, 0.8431, 0.1725, 1.0), "axes": (0.0, 0.0, 0.0, 1.0)},
    'Daylight Gem': {"background": (0.9412, 0.9412, 0.9412, 1.0), "object": (0.0078, 0.8549, 0.9686, 1.0), "axes": (0.2157, 0.2235, 0.2784, 1.0)},
    'DeepOcean': {"background": (0.2, 0.2, 0.2, 1.0), "object": (0.9333, 0.9333, 0.9333, 1.0), "axes": (0.7569, 0.7569, 0.7569, 1.0)},
    'Metallic': {"background": (0.6667, 0.6667, 1.0, 1.0), "object": (0.8667, 0.8667, 1.0, 1.0), "axes": (0.1333, 0.1333, 0.2, 1.0)},
    'Nature': {"background": (0.9804, 0.9804, 0.9804, 1.0), "object": (0.0863, 0.6275, 0.5216, 1.0), "axes": (0.1961, 0.1961, 0.1961, 1.0)},
    'Nocturnal Gem': {"background": (0.0471, 0.0471, 0.0471, 1.0), "object": (0.0078, 0.8549, 0.9686, 1.0), "axes": (0.6549, 0.6627, 0.7176, 1.0)},
    'Solarized': {"background": (0.9922, 0.9647, 0.8902, 1.0), "object": (0.7098, 0.5333, 0.0, 1.0), "axes": (0.098, 0.0941, 0.0863, 1.0)},
    'Starnight': {"background": (0.0, 0.0, 0.0, 1.0), "object": (1.0, 1.0, 0.8784, 1.0), "axes": (0.898, 0.898, 0.898, 1.0)},
    'Sunset': {"background": (0.6667, 0.2667, 0.2667, 1.0), "object": (1.0, 0.6667, 0.6667, 1.0), "axes": (0.1333, 0.051, 0.051, 1.0)},
    'Tomorrow Night': {"background": (0.1137, 0.1216, 0.1294, 1.0), "object": (0.5412, 0.7451, 0.7176, 1.0), "axes": (0.9098, 0.9098, 0.9098, 1.0)},
    'Tomorrow': {"background": (0.9725, 0.9725, 0.9725, 1.0), "object": (0.2431, 0.6, 0.6235, 1.0), "axes": (0.0941, 0.0941, 0.0941, 1.0)},
}

DEFAULT_COLOR_THEME = "Cornfield"
