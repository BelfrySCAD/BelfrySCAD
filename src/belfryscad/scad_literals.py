"""OpenSCAD literal (de)serialization and Customizer parameter-set ("preset")
file I/O -- split out from window.customizer so the CLI's -p/-P handling
(belfryscad.headless, Qt-free by design) doesn't need to import PySide6.
window.customizer re-exports these under their original names for its own
internal use and for existing tests."""

import json
import re
from pathlib import Path
from typing import Any, Optional


def parse_literal(s: str) -> Optional[Any]:
    s = s.strip()
    if s == 'true':
        return True
    if s == 'false':
        return False
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    try:
        if '.' in s or ('e' in s.lower() and re.match(r'^-?[\d]', s)):
            return float(s)
        return int(s)
    except ValueError:
        pass
    m = re.match(r'^\[([^\[\]]*)\]$', s)
    if m:
        inner = m.group(1).strip()
        if inner:
            parts = [p.strip() for p in inner.split(',')]
            if 1 <= len(parts) <= 4:
                try:
                    return [float(p) if '.' in p else int(p) for p in parts]
                except ValueError:
                    pass
    return None


def format_value(v: Any) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        return '[' + ', '.join(format_value(x) for x in v) + ']'
    if isinstance(v, float):
        s = f'{v:g}'
        if '.' not in s and 'e' not in s.lower():
            s += '.0'
        return s
    return str(v)


def scad_literal(v) -> str:
    """Render a Python value back as OpenSCAD source, losing nothing: a
    float is written at full round-trip precision, unlike format_value's
    `:g` (6 significant digits)."""
    if v is None:
        return "undef"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, float):
        if v != v:
            return "0/0"            # OpenSCAD has no literal for nan or inf
        if v in (float("inf"), float("-inf")):
            return "1/0" if v > 0 else "-1/0"
        # Trim the ".0" a whole number would otherwise carry: OpenSCAD has
        # one number type, so `42` and `42.0` are the same value and the
        # shorter form is what a human would have written.
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(scad_literal(x) for x in v) + "]"
    return str(v)


def is_literal_value(v) -> bool:
    """True when scad_literal(v) is a plain literal: no function, object
    or range anywhere inside it."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, (list, tuple)):
        return all(is_literal_value(x) for x in v)
    return False


def rounded(v, digits: int = 6):
    """`v` with every number rounded to `digits` significant digits --
    what echo() shows and what Edit as... writes back."""
    if isinstance(v, float):
        return float(f"{v:.{digits}g}")
    if isinstance(v, (list, tuple)):
        return [rounded(x, digits) for x in v]
    return v


def scad_literal_block(v, indent: str = "") -> str:
    """scad_literal(v), with a list of lists of lists -- a grid of points,
    a list of paths -- broken one element per line, so each row reads on
    a line of its own (#932). Anything shallower stays on one line."""
    def deep(x):
        return isinstance(x, (list, tuple)) and any(
            isinstance(y, (list, tuple)) and any(isinstance(z, (list, tuple)) for z in y)
            for y in x)
    if not deep(v):
        return scad_literal(v)
    inner = indent + "    "
    return ("[\n" + ",\n".join(inner + scad_literal_block(x, inner) for x in v)
            + "\n" + indent + "]")


def preset_path_for(scad_path: str) -> str:
    """The sidecar JSON file OpenSCAD-style presets for *scad_path* live in."""
    return str(Path(scad_path).with_suffix('.json'))


def load_presets(path: str) -> dict[str, dict[str, Any]]:
    """Returns {preset_name: {param_name: value}}. Missing/unreadable/malformed
    files just mean no presets -- never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    sets = data.get('parameterSets', {})
    if not isinstance(sets, dict):
        return {}
    result = {}
    for name, params in sets.items():
        if not isinstance(params, dict):
            continue
        parsed = {}
        for k, v in params.items():
            val = parse_literal(str(v))
            if val is not None:
                parsed[k] = val
        result[name] = parsed
    return result


def save_presets(path: str, presets: dict[str, dict[str, Any]]) -> None:
    data = {
        'fileFormatVersion': '1',
        'parameterSets': {
            name: {k: format_value(v) for k, v in params.items()}
            for name, params in presets.items()
        },
    }
    Path(path).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
