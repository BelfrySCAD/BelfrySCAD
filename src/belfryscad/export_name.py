"""`$export_name` — the script-controllable default filename for Export.

Seeded from the input file's basename before the script runs, readable and
assignable by the script, and used (sanitised) as the default name in the
Export dialog.

Pure and Qt-free so the GUI, the debugger and the headless CLI can all seed
it the same way -- a script that reads `$export_name` should not see it
defined in one and undefined in another.

Nothing here needs an evaluator change: `viewport_params` seeds arbitrary
`$`-names (the name is historical, not a restriction) and `Evaluator.dyn`
hands every `$`-variable back after evaluate().
"""

import os

#: Characters kept as-is in an export filename. Everything else becomes "_".
#: Letters, digits, underscore, dash, plus, period -- deliberately no space
#: and no path separator, so the result is safe to drop straight into a save
#: dialog on any platform.
_VALID = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "_-+."
)

#: What an unsaved buffer is called, matching the tab title.
UNTITLED = "Untitled"


def default_export_name(file_path) -> str:
    """The value `$export_name` is seeded with: the input file's basename
    without its extension, or "Untitled" for a buffer never saved.

    Seeded from the ORIGINAL path, never the temp file a live unsaved
    buffer is rendered through -- that one is named for the temp dir and
    would leak a random string into the dialog.
    """
    if not file_path:
        return UNTITLED
    stem = os.path.splitext(os.path.basename(str(file_path)))[0]
    return stem or UNTITLED


def sanitize_export_name(value) -> str:
    """`value` reduced to characters legal in an export filename.

    Every other character becomes a single underscore -- one per character,
    not one per run, so `a//b` is `a__b` and the original length is still
    recognisable.

    A non-string (a script assigning `$export_name = 42`) is stringified
    first rather than rejected; the script asked for something and a
    usable name is friendlier than silently falling back.

    Returns "" for an empty or all-empty input, which callers treat as
    "no opinion" and fall back to their own default.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        # Whole floats stringify as "42.0"; OpenSCAD has one number type, so
        # "42" is what the author wrote and what they mean.
        if isinstance(value, float) and value == int(value):
            value = str(int(value))
        else:
            value = str(value)
    return "".join(c if c in _VALID else "_" for c in value)


def seed_params(params: dict, file_path) -> dict:
    """`params` with `$export_name` added, without mutating the original."""
    out = dict(params or {})
    out.setdefault("$export_name", default_export_name(file_path))
    return out


def resolve_export_name(dyn_value, file_path) -> str:
    """The final export basename: the script's `$export_name`, sanitised,
    falling back to the file-derived default if that leaves nothing."""
    return sanitize_export_name(dyn_value) or default_export_name(file_path)
