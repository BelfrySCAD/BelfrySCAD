"""Your own colour palettes (#766): named lists of named colours, such as the
filament on your shelf, offered by the colour picker and the Color List
beside the CSS and xkcd names.

A palette is edited as a table of names and colours (`color_list.PaletteEditor`);
pasted text is read one colour per line -- a name, then the colour as
`#rrggbb` / `#rgb` or as 0-255 `r, g, b` -- so a list can come straight from
filamentcolors.xyz or a spreadsheet:

    Sunlu Cyan      #0074c3
    Sunlu Magenta   196, 65, 136

A palette colour's name is yours, not one `color()` knows, so choosing it
writes its hex into the script.

`parse_palette`/`format_palette` and the storage are plain functions with no
widget in them, so `tests/test_color_palettes.py` tests them directly.
"""
from __future__ import annotations

import json
import re

_KEY = "colors/palettes"
_VIEW_KEY = "colors/paletteView"

_HEX_TAIL = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\s*$")
_RGB_TAIL = re.compile(r"[\[(]?\s*(\d{1,3})\s*[,;\s]\s*(\d{1,3})\s*[,;\s]\s*(\d{1,3})\s*[\])]?\s*$")


def _value_at_end(line: str):
    """The match for a colour value ending `line`, and its "#rrggbb"."""
    m = _HEX_TAIL.search(line)
    if m:
        h = m.group(1).lower()
        return m, "#" + (h if len(h) == 6 else "".join(ch * 2 for ch in h))
    m = _RGB_TAIL.search(line)
    if not m or any(int(v) > 255 for v in m.groups()):
        return None, None
    return m, "#" + "".join(f"{int(v):02x}" for v in m.groups())


def parse_color_value(text: str) -> str | None:
    """"#rrggbb" for a colour typed on its own -- `#rrggbb`, `#rgb` or
    0-255 `r, g, b` -- or None."""
    m, hexa = _value_at_end(text.strip())
    return hexa if m is not None and m.start() == 0 else None


def _parse_line(line: str) -> tuple[str, str] | None:
    """(name, "#rrggbb") for one line, or None if it names no colour."""
    m, hexa = _value_at_end(line)
    if m is None:
        return None
    name = line[:m.start()].strip().rstrip(",;:=\t ").strip()
    return name or hexa, hexa


def parse_palette(text: str) -> tuple[list[tuple[str, str]], list[int]]:
    """The (name, "#rrggbb") entries `text` lists, and the 1-based numbers
    of the lines that list none. Blank lines and `//` comments are skipped."""
    entries, bad = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        entry = _parse_line(line)
        if entry is None:
            bad.append(n)
        else:
            entries.append(entry)
    return entries, bad


def format_palette(entries: list[tuple[str, str]]) -> str:
    width = max((len(n) for n, _ in entries), default=0)
    return "".join(f"{n.ljust(width)}  {h}\n" for n, h in entries)


def load_palettes() -> dict[str, list[tuple[str, str]]]:
    """Palette name -> entries, in the order they were made."""
    from belfryscad.settings import app_settings
    try:
        raw = json.loads(app_settings().value(_KEY, "{}") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, entries in raw.items():
        if isinstance(name, str) and isinstance(entries, list):
            out[name] = [(e[0], e[1]) for e in entries
                         if isinstance(e, list) and len(e) == 2 and _HEX_TAIL.fullmatch(str(e[1]))]
    return out


def save_palettes(palettes: dict[str, list[tuple[str, str]]]):
    # JSON, not a QSettings map: macOS plists mangle one-element lists.
    from belfryscad.settings import app_settings
    app_settings().setValue(_KEY, json.dumps({k: [list(e) for e in v] for k, v in palettes.items()}))


def last_view() -> str | None:
    """The palette last listed, or None for the named colours."""
    from belfryscad.settings import app_settings
    v = app_settings().value(_VIEW_KEY, "")
    return v if isinstance(v, str) and v else None


def set_last_view(name: str | None):
    from belfryscad.settings import app_settings
    app_settings().setValue(_VIEW_KEY, name or "")
