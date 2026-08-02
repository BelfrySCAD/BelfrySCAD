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
