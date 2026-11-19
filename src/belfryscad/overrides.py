"""Top-level assignments appended to a script: `-D` and the Customizer's
values (#1146).

Appended, not substituted: OpenSCAD's last top-level assignment of a name
wins everywhere, so appending overrides the value while every byte offset
of the script itself stays where it was -- which drag-to-edit and the error
marks map back to the editor by. OpenSCAD's own -D does the same, and says
nothing about it; our evaluator reports the appended line as overwriting the
script's, so that one warning is dropped for appended lines only. A
reassignment in the script itself still warns.
"""
import os
import re
from typing import Any

from belfryscad import scad_temp
from belfryscad.scad_literals import format_value

_OVERWRITTEN = re.compile(
    r"^WARNING: \S+ was assigned on line \d+ but was overwritten in file (.+), line (\d+)$")
#: realpath of a temp holding appended assignments -> the first such line.
_appended_from: dict[str, int] = {}


def assignments(values: dict[str, Any]) -> str:
    """`name = literal;` lines for Customizer values."""
    return "".join(f"{name} = {format_value(v)};\n" for name, v in values.items())


def write_temp(source: str, appended: str, near=None) -> str:
    """`source` with `appended` after it, in a new temp .scad (see
    `scad_temp.write_temp_scad`, and its `remove`)."""
    if not appended:
        return scad_temp.write_temp_scad(source, near=near)
    text = source + "\n"
    path = scad_temp.write_temp_scad(text + appended, near=near)
    _appended_from[os.path.realpath(path)] = text.count("\n") + 1
    return path


def is_override_warning(msg: str) -> bool:
    """The evaluator's "x was overwritten" warning for an appended line."""
    m = _OVERWRITTEN.match(msg)
    if not m:
        return False
    first = _appended_from.get(os.path.realpath(m.group(1)))
    return first is not None and int(m.group(2)) >= first


def forget(path) -> None:
    _appended_from.pop(os.path.realpath(path), None)
