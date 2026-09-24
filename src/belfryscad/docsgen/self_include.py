"""Examples see the file they document (#560).

openscad_docsgen builds an example's script from the file's `Includes:`
lines, its `CommonCode`, and the example -- never the documented file
itself. Inside BOSL2 that goes unnoticed, because `std.scad` includes every
core file, and the files it does not include list themselves
(`gears.scad` says `include <BOSL2/gears.scad>`). A library of your own,
outside any such arrangement, had every example fail with `Ignoring unknown
function` for the very functions it documents.

So after the `Includes:` lines, `include <the file>` is added -- but only
when those lines do not already reach it. Including a file twice re-runs its
top-level assignments, and OpenSCAD warns about every one it overwrites;
BOSL2's own examples would all break. Reach is decided statically, the way
`-d` finds dependencies (`scad_deps`), with the libshim redirect applied, so a
checkout previewed from outside the libraries folder resolves its own name to
itself exactly as the run will.

The Docs pane documents the live editor buffer, not the saved file, so it
sets `live_copy` to a temporary copy of the buffer beside the source, and that
is what gets included.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_INCLUDE_RE = re.compile(r'\b(?:use|include)\s*<([^>]+)>')

#: (documented file's basename, path of a copy of its live text) while the
#: Docs pane is building a preview; None otherwise. See module docstring.
live_copy: tuple[str, str] | None = None


def self_include_lines(src_file: str, includes) -> list[str]:
    """`["include <file>"]` for an example of `src_file`, or `[]` when its
    `includes` already reach it."""
    from .runner import runner

    # The Docs pane passes a bare basename and says where it lives through
    # the runner's override; the CLI passes a path.
    base = runner.src_dir_override
    path = os.path.join(base, os.path.basename(src_file)) if base else os.path.abspath(src_file)
    if _reached(os.path.realpath(path), os.path.dirname(path), tuple(includes)):
        return []
    name = os.path.basename(path)
    if live_copy is not None and live_copy[0] == name:
        name = os.path.basename(live_copy[1])
    return [f"include <{name}>"]


@lru_cache(maxsize=256)
def _reached(target: str, src_dir: str, includes: tuple) -> bool:
    from belfryscad.libshim import detect
    from belfryscad.scad_deps import _resolve, scan_dependencies

    shim = detect(src_dir, includes)
    for line in includes:
        for name in _INCLUDE_RE.findall(line):
            lib, _, rest = name.partition("/")
            if shim and lib == shim[0] and rest:
                found = Path(shim[1]) / rest
                found = found if found.is_file() else None
            else:
                found = _resolve(name, Path(src_dir))
            if found is None:
                continue
            if any(os.path.realpath(d) == target for d in scan_dependencies(str(found))):
                return True
    return False
