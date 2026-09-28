"""Every colour string `color()` accepts, and the orders to list them in.

The GUI's copy of OpenSCAD's `parse_color` (ColorUtil.cc), which
openscad_cpp_evaluator >= 1.29.0 follows too, so the editor's swatches, the
colour picker and the Color List agree with what a render draws:

- the CSS names: Qt's list plus `rebeccapurple`, which Qt never adopted, and
  `transparent` (black at alpha 0); case-insensitive;
- `xkcd:<name>` over the 949 xkcd survey names (`xkcd_colors.py`);
- hex as `#rgb`, `#rgba`, `#rrggbb` or `#rrggbbaa` -- alpha LAST, as CSS
  and OpenSCAD write it. Qt's own `#aarrggbb` reads alpha first, so hex is
  parsed here rather than by `QColor.fromString`.

`SORT_KEYS` are the orders the picker and the Color List both offer.
"""
from __future__ import annotations

import re
from functools import lru_cache

from PySide6.QtGui import QColor

from belfryscad.window.xkcd_colors import XKCD_COLORS

_HEX_ANY = re.compile(r"#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")


def _hue_order(name: str, color: QColor | None = None):
    """Twelve 30-degree hue bands around the wheel (red centred on 0), dark
    to light within each -- bands rather than raw hue, since sorted by exact
    hue, lightness only broke ties, so dark and light alternated along any
    stretch of similar hues. No separate grey or pastel group: a colour with
    no hue (Qt's -1) counts as hue 0, and the Saturation or Chroma sort is
    the way to gather the neutrals. `color` is given when `name` is not one
    Qt knows (an xkcd name)."""
    c = color if color is not None else QColor(name)
    return ((max(c.hsvHue(), 0) + 15) % 360 // 30, c.lightnessF())


#: The CSS names, in hue order, without `transparent` (its swatch would be a
#: lie). Qt's list plus `rebeccapurple`.
_PICKABLE_NAMES = sorted([n for n in QColor.colorNames() if n != "transparent"] + ["rebeccapurple"],
                         key=lambda n: _hue_order(n, QColor("#663399") if n == "rebeccapurple" else None))


@lru_cache(maxsize=1)
def all_colors() -> dict:
    """Every listable name -> QColor: the CSS names, then `xkcd:<name>`."""
    out = {n: QColor(n) for n in _PICKABLE_NAMES}
    out["rebeccapurple"] = QColor("#663399")
    out.update((f"xkcd:{n}", QColor(h)) for n, h in XKCD_COLORS.items())
    return out


def parse_color(text: str) -> QColor | None:
    """The colour `color(text)` draws, or None where it would warn
    "Unable to parse color"."""
    t = text.strip()
    m = _HEX_ANY.match(t)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) if len(h) == 8 else 255
        return QColor(r, g, b, a)
    name = t.lower()
    if name == "transparent":
        return QColor(0, 0, 0, 0)
    c = all_colors().get(name)
    return QColor(c) if c is not None else None


def _luminance(c: QColor) -> float:
    """Relative luminance (WCAG / Rec. 709, on linearised sRGB): how light
    the colour looks, not the HSL `lightness` Qt reports, which rates pure
    yellow and pure blue alike."""
    def lin(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b, _ = c.getRgbF()
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _chroma(c: QColor) -> float:
    """max - min of r, g, b: how colourful, whatever the lightness --
    saturation divides this by value (HSV) or by a lightness term (HSL)."""
    r, g, b, _ = c.getRgbF()
    return max(r, g, b) - min(r, g, b)


def _hue(n: str, c: QColor):
    return _hue_order(n.removeprefix("xkcd:"), c)


#: Sort choice -> key over (name, QColor). All but Name run low to high, with
#: ties falling back to hue so near-equal rows still group by colour.
#: Name sorts on the bare name, so `red` and `xkcd:red` sit together rather
#: than every xkcd name filing under "x". Saturation is HSV's (distance from
#: grey): HSL's rates a near-white like `lavenderblush` fully saturated.
#: Lightness is HSL's (max + min) / 2, Value HSV's max, Luminance how light it
#: looks.
SORT_KEYS = {
    "Name": lambda n, c: (n.removeprefix("xkcd:"), n.startswith("xkcd:")),
    "Hue": _hue,
    "Saturation": lambda n, c: (round(c.hsvSaturationF(), 3), _hue(n, c)),
    "Chroma": lambda n, c: (round(_chroma(c), 3), _hue(n, c)),
    "Lightness": lambda n, c: (round(c.lightnessF(), 3), _hue(n, c)),
    "Value": lambda n, c: (round(c.valueF(), 3), _hue(n, c)),
    "Luminance": lambda n, c: (round(_luminance(c), 4), _hue(n, c)),
}


def sorted_colors(sort: str = "Hue", include_xkcd: bool = True) -> list:
    """(name, QColor) pairs in `sort` order."""
    key = SORT_KEYS[sort]
    items = all_colors().items()
    if not include_xkcd:
        items = [kv for kv in items if not kv[0].startswith("xkcd:")]
    return sorted(items, key=lambda kv: key(*kv))
