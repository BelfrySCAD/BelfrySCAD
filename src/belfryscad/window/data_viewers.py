"""
Finding viewable/editable data literals in source text, and the menus
that open a data viewer on them -- from the code editor's right-click menu
and the debugger's variable context menu. Each viewer lives in its own
data_viewer_*.py module.
"""
from __future__ import annotations

import ast
import copy
import re

from PySide6.QtWidgets import QMenu

from belfryscad.turtle import is_turtle as _is_turtle
from belfryscad.turtle3d import is_turtle3d as _is_turtle3d, uses_3d_only as _uses_3d_only

from belfryscad.window.data_viewer_affine import AffineMatrixViewer, _is_affine_matrix
from belfryscad.window.data_viewer_common import (_is_list, _is_numeric_point,
                                                  _is_oscobject,
                                                  _scad_literal_to_python)
from belfryscad.window.data_viewer_grid import GridViewer, _is_grid
from belfryscad.window.data_viewer_heightfield import HeightfieldViewer, _is_heightfield
from belfryscad.window.data_viewer_list import ListViewer
from belfryscad.window.data_viewer_matrix import MatrixViewer, _is_matrix
from belfryscad.window.data_viewer_object import ObjectViewer
from belfryscad.window.data_viewer_path import PathViewer, _is_path
from belfryscad.window.data_viewer_region import RegionViewer, _is_region
from belfryscad.window.data_viewer_turtle import Turtle3DViewer, TurtleViewer, _session_starts
from belfryscad.window.data_viewer_vnf import VNFViewer, _is_vnf
from belfryscad.window.data_viewer_vnf_tile import VNFTileViewer, _is_vnf_tile


def _object_field(v, *names):
    """First present key among `names` on an OscObject, else None.

    Geometry objects name their points `vertices`; `points` is accepted as
    an alias because that is what polyhedron()/polygon() call the same
    argument, and a hand-built object may follow either.
    """
    if not _is_oscobject(v):
        return None
    data = v.data
    for n in names:
        if n in data:
            return data[n]
    return None


def _geometry_object_vnf(v):
    """`[vertices, faces]` for a 3D geometry object, else None.

    A `render()` expression yields an object() carrying the mesh as separate
    `vertices` and `faces` keys, so `_is_vnf` -- which only ever matches a
    bare 2-list -- fires for `obj.vnf` but not for `obj` itself. This is the
    unwrapping that lets the object go straight to the VNF viewer.

    Deliberately not folded into `_is_vnf`: that predicate is also used to
    spot VNF *literals* in source text (find_viewable_literals), where an
    object is not a candidate at all.
    """
    verts = _object_field(v, "vertices", "points")
    faces = _object_field(v, "faces")
    if verts is None or faces is None:
        return None
    candidate = [verts, faces]
    return candidate if _is_vnf(candidate) else None


def _geometry_object_region(v):
    """A 2D geometry object's contours as real point lists, else None.

    The 2D counterpart of `_geometry_object_vnf`. Unlike the 3D case this is
    not a straight unwrap: `paths` holds *indices* into `vertices` (matching
    polygon(points=, paths=)), so the contours have to be resolved before
    any viewer can use them. The result is a region -- a list of closed 2D
    paths -- which is exactly what a 2D shape with holes is.

    Indices arrive as floats: OpenSCAD has no integer type.
    """
    verts = _object_field(v, "vertices", "points")
    paths = _object_field(v, "paths")
    if not (_is_list(verts) and _is_list(paths)) or not paths:
        return None
    if not all(_is_numeric_point(p) and len(p) == 2 for p in verts):
        return None
    out = []
    for path in paths:
        if not (_is_list(path) and len(path) >= 3):
            return None
        contour = []
        for idx in path:
            if not isinstance(idx, (int, float)):
                return None
            i = int(idx)
            if not (0 <= i < len(verts)):
                return None
            contour.append(list(verts[i]))
        out.append(contour)
    return out if _is_region(out) else None


# ---------------------------------------------------------------------------
# Lexical literal detection — for the code editor's "View as..."/"Edit as..."
# context-menu items, which work on a plain numeric-only bracketed literal
# under the cursor with no debug session (and no AST/root_scope) involved.
# ---------------------------------------------------------------------------

_ASSIGNMENT_NAME_RE = re.compile(r'([A-Za-z_]\w*)\s*=\s*$')


def _literal_display_name(text: str, start: int) -> str:
    """If the literal at `start` is the RHS of a simple `name = <literal>`
    assignment, return `name`; otherwise "" (e.g. a literal used inline,
    like a `translate([...])` argument). Callers should omit any ": {name}"
    title/menu-label suffix entirely when this is empty, rather than fall
    back to a raw snippet of the literal's own source text — for a
    multi-line literal that snippet is often just "[" (window/menu titles
    can't show the embedded newline that follows), which isn't useful."""
    m = _ASSIGNMENT_NAME_RE.search(text[max(0, start - 200):start])
    return m.group(1) if m else ""


_COMMENT_OR_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _blank_comments(text: str) -> str:
    """`text` with every comment turned to spaces, newlines kept, so offsets
    still line up. Strings are skipped, so a "//" in one stays (#818)."""
    return _COMMENT_OR_STRING_RE.sub(
        lambda m: m.group() if m.group().startswith('"') else re.sub(r'[^\n]', ' ', m.group()), text)


def _iter_enclosing_literals(text: str, offset: int, max_levels: int = 8):
    """Yield `(start, end, value)` innermost-to-outermost for each enclosing
    `[...]` literal around `offset` that `ast.literal_eval`s to a list
    (`end` is exclusive). Levels that fail to parse — identifiers, calls,
    OpenSCAD-only syntax like ranges or `true`/`false`/`undef` — are skipped
    but the walk continues outward past them. Stops after `max_levels` or
    once there's no further enclosing `[`. Comments are ignored, both in
    the bracket count and in the value."""
    text = _blank_comments(text)
    pos = offset
    for _ in range(max_levels):
        depth = 0
        start = None
        i = pos - 1
        while i >= 0:
            c = text[i]
            if c == ']':
                depth += 1
            elif c == '[':
                if depth == 0:
                    start = i
                    break
                depth -= 1
            i -= 1
        if start is None:
            return
        depth = 0
        end = None
        j = start
        while j < len(text):
            c = text[j]
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end is None:
            return
        try:
            value = ast.literal_eval(text[start:end + 1])
        except (ValueError, SyntaxError):
            value = None
        if isinstance(value, list):
            yield start, end + 1, value
        pos = start


def _split_top_level(src: str, sep: str = ",") -> list[str]:
    """Split on `sep`, ignoring separators nested in brackets or quotes."""
    parts, depth, i, start = [], 0, 0, 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            quote = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    break
                i += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(src[start:i])
            start = i + 1
        i += 1
    parts.append(src[start:])
    return parts


def _enclosing_call(text: str, start: int, end: int) -> dict | None:
    """The call `text[start:end]` is a direct argument of, or None.

    `name` is the function's; `args` lists each argument as a dict with its
    span (`a`, `b`, whitespace trimmed), its `name` (None if positional),
    its value's span (`value`) and, if positional, its `pos`ition among the
    positional ones; `index` is which of them holds `text[start:end]`.
    Lexical, with comments ignored, like `find_editable_literals`."""
    blank = _blank_comments(text)
    depth, i = 0, start - 1
    while i >= 0:
        c = blank[i]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                break
            depth -= 1
        i -= 1
    m = re.search(r"\b([A-Za-z_]\w*)\s*$", blank[:max(i, 0)])
    if i < 0 or blank[i] != "(" or not m:
        return None
    depth, j = 0, i
    while j < len(blank):
        if blank[j] in "([":
            depth += 1
        elif blank[j] in ")]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    call = {"name": m.group(1), "open": i, "close": j, "args": [], "index": None}
    pos, positional = i + 1, 0
    for part in _split_top_level(blank[i + 1:j]):
        if part.strip():
            a = pos + len(part) - len(part.lstrip())
            b = pos + len(part.rstrip())
            named = re.match(r"\s*([A-Za-z_$]\w*)\s*=(?!=)\s*", part)
            arg = {"a": a, "b": b, "name": named.group(1) if named else None,
                   "value": (pos + named.end(), b) if named else (a, b),
                   "pos": None if named else positional}
            if not named:
                positional += 1
            if arg["value"][0] <= start and end <= arg["value"][1]:
                call["index"] = len(call["args"])
            call["args"].append(arg)
        pos += len(part) + 1
    return call if call["index"] is not None else None


def _call_arg(call: dict, name: str, position: int | None = None) -> dict | None:
    """The argument given as `name=`, or at positional `position`."""
    for arg in call["args"]:
        if arg["name"] == name or (position is not None and arg["pos"] == position):
            return arg
    return None


def _literal(text: str, span):
    try:
        return ast.literal_eval(_blank_comments(text)[span[0]:span[1]])
    except (ValueError, SyntaxError):
        return None


def turtle_call_context(text: str, start: int, end: int) -> dict | None:
    """Where the turtle list at `text[start:end]` sits in a `turtle(...)`
    call, or None when it is not one of that call's arguments.

    `arg_start` is where the list's argument begins (before a `commands=`),
    which is where a new `state=` goes. `state` is the `(start, end)` span
    of the state argument's value -- `state=` or turtle()'s second
    positional argument -- or None, and `start` is its value when that is
    a literal `[x, y]`, else None: a state worked out by the script (a
    variable, an earlier turtle's full state) can be read by nothing short
    of running it."""
    call = _enclosing_call(text, start, end)
    if call is None or call["name"] != "turtle":
        return None
    mine = call["args"][call["index"]]
    state = _call_arg(call, "state", 1 if mine["pos"] == 0 else None)
    ctx = {"arg_start": mine["a"], "state": state["value"] if state else None, "start": None}
    if state is not None:
        v = _literal(text, state["value"])
        if isinstance(v, list) and len(v) == 2 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            ctx["start"] = [float(v[0]), float(v[1])]
    return ctx


#: BOSL2's named directions, as `turtle3d(state=RIGHT)` may give one.
_NAMED_DIRECTIONS = {
    "RIGHT": [1, 0, 0], "LEFT": [-1, 0, 0], "BACK": [0, 1, 0], "FRONT": [0, -1, 0],
    "FWD": [0, -1, 0], "FORWARD": [0, -1, 0], "UP": [0, 0, 1], "TOP": [0, 0, 1],
    "DOWN": [0, 0, -1], "BOTTOM": [0, 0, -1], "BOT": [0, 0, -1],
}


def turtle3d_call_context(text: str, start: int, end: int) -> dict | None:
    """`turtle_call_context` for `turtle3d(...)`, whose `state=` is a
    direction or a transform rather than a point. `start` is the position
    when the state is `move([x, y, z])`, or `[0, 0, 0]` when there is none;
    `dir` is the direction when the state is one (a literal vector or a
    BOSL2 name such as `RIGHT`). Anything else leaves both None."""
    call = _enclosing_call(text, start, end)
    if call is None or call["name"] != "turtle3d":
        return None
    mine = call["args"][call["index"]]
    state = _call_arg(call, "state", 1 if mine["pos"] == 0 else None)
    ctx = {"arg_start": mine["a"], "state": state["value"] if state else None,
           "start": None if state else [0.0, 0.0, 0.0], "dir": None}
    if state is None:
        return ctx
    word = _blank_comments(text)[state["value"][0]:state["value"][1]].strip()

    def vec3(v):
        return (isinstance(v, list) and len(v) in (2, 3)
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v))
    m = re.fullmatch(r"move\s*\((.*)\)", word, re.S)
    if m:
        try:
            v = ast.literal_eval(m.group(1).strip())
        except (ValueError, SyntaxError):
            v = None
        if vec3(v):
            ctx["start"] = [float(x) for x in v] + [0.0] * (3 - len(v))
    elif word in _NAMED_DIRECTIONS:
        ctx["dir"] = list(_NAMED_DIRECTIONS[word])
    else:
        v = _literal(text, state["value"])
        if vec3(v) and len(v) == 3 and any(v):
            ctx["dir"] = [float(x) for x in v]
    return ctx


def nurbs_interp_context(text: str, start: int, end: int) -> dict | None:
    """When the points at `text[start:end]` are `nurbs_interp()`'s (its
    first argument, or `points=`), what the call says, for the path editor
    to open on: `degree`, `closed` and `corners` where each is a literal
    (else None), and the call itself, for `nurbs_write_back`. None when the
    list is anything else."""
    call = _enclosing_call(text, start, end)
    if call is None or call["name"] != "nurbs_interp":
        return None
    mine = call["args"][call["index"]]
    if not (mine["pos"] == 0 or mine["name"] == "points"):
        return None
    ctx = {"call": call, "degree": None, "closed": None, "corners": None}
    deg = _call_arg(call, "degree", 1)
    if deg is not None:
        v = _literal(text, deg["value"])
        ctx["degree"] = v if isinstance(v, int) and not isinstance(v, bool) else None
    closed = _call_arg(call, "closed")
    if closed is not None:
        word = text[closed["value"][0]:closed["value"][1]].strip()
        ctx["closed"] = {"true": True, "false": False}.get(word)
    corners = _call_arg(call, "corners")
    if corners is not None:
        v = _literal(text, corners["value"])
        if isinstance(v, list) and all(isinstance(c, int) and not isinstance(c, bool) for c in v):
            ctx["corners"] = v
    return ctx


def nurbs_write_back(text: str, start: int, end: int, list_text: str,
                     ctx: dict | None, corners: list) -> tuple[str, int, int]:
    """`(replacement, s, e)` writing the path editor's points and corners
    back into a `nurbs_interp()` call in one edit: `corners=` replaced,
    added after the last argument, or removed when there are none. A
    `corners=` the script computes is left alone -- the editor opened with
    no corners it could read, so it has none to write."""
    if ctx is None:
        return list_text, start, end
    call = ctx["call"]
    arg = _call_arg(call, "corners")
    value = "[" + ", ".join(str(c) for c in sorted(corners)) + "]"
    if arg is not None and ctx["corners"] is None:
        return list_text, start, end
    if arg is None:
        if not corners:
            return list_text, start, end
        last = call["args"][-1]["b"]
        return list_text + text[end:last] + f", corners={value}", start, last
    if corners:
        a, b = arg["value"]
        return list_text + text[end:a] + value, start, b
    # Removed with its comma: the one before it, or, written ahead of the
    # points, the one after.
    k = call["args"].index(arg)
    if arg["a"] > start:
        prev_b = call["args"][k - 1]["b"]
        return list_text + text[end:prev_b], start, arg["b"]
    next_a = call["args"][k + 1]["a"]
    return text[next_a:start] + list_text, arg["a"], end


def turtle_write_back(text: str, start: int, end: int, list_text: str,
                      ctx: dict | None, new_start, wrap: str = "{}") -> tuple[str, int, int]:
    """`(replacement, s, e)` writing a turtle editor's result back: the list
    at `text[start:end]` becomes `list_text`, and when the start was moved,
    the call's `state=` becomes `new_start` -- inserted before the list when
    the call has none. One edit, so it is one undo. `wrap` formats the
    point as the state: turtle3d's is `move([x, y, z])`."""
    pt = wrap.format("[" + ", ".join(f"{round(v, 2):g}" for v in new_start) + "]") \
        if new_start else None
    if ctx is None or pt is None:
        return list_text, start, end
    zero = [0.0] * len(new_start)
    old = ctx["start"] or zero
    if ctx["state"] is None:
        if list(new_start) == zero:
            return list_text, start, end
        a = ctx["arg_start"]
        return f"state={pt}, " + text[a:start] + list_text, a, end
    s, e = ctx["state"]
    if ctx["start"] is None or [round(v, 2) for v in new_start] == [round(v, 2) for v in old]:
        return list_text, start, end
    if s < start:
        return pt + text[e:start] + list_text, s, end
    return list_text + text[end:s] + pt, start, e


def _parse_object_call_args(argtext: str):
    """`[(key, value), ...]` for an all-literal `object(...)` argument list,
    or None if any argument is not `name = <literal>`.

    None is the honest answer for `object(other, [["b"]])`: `other` is a
    reference whose contents are not knowable from the text, so an editor
    that rewrote the call would silently destroy it. Such calls stay
    view-only.
    """
    if not argtext.strip():
        return []
    entries: list[tuple[str, object]] = []
    seen: set[str] = set()
    for raw in _split_top_level(argtext):
        part = raw.strip()
        if not part:
            return None
        eq = _split_top_level(part, "=")
        if len(eq) != 2:
            return None
        key = eq[0].strip()
        if not re.match(r"^\$?[A-Za-z_][A-Za-z0-9_]*$", key):
            return None
        ok, value = _scad_literal_to_python(eq[1].strip())
        if not ok:
            return None
        # A later duplicate overwrites IN PLACE, keeping the key's original
        # position -- object(a=42, b=1, a=99) is { a = 99; b = 1; }, checked
        # against the reference. Removing and re-appending would be DELETE
        # semantics, and the difference is observable because an object is
        # insertion-ordered.
        if key in seen:
            for i, (k, _v) in enumerate(entries):
                if k == key:
                    entries[i] = (key, value)
                    break
        else:
            seen.add(key)
            entries.append((key, value))
    return entries


def _find_object_call(text: str, offset: int):
    """`(start, end, entries)` for the innermost enclosing `object(...)` call
    around `offset`, or None. `end` is exclusive; `entries` is None when the
    call is not all-literal (viewable but not editable)."""
    pos = offset
    for _ in range(8):
        depth = 0
        open_paren = None
        i = pos - 1
        while i >= 0:
            c = text[i]
            if c == ")":
                depth += 1
            elif c == "(":
                if depth == 0:
                    open_paren = i
                    break
                depth -= 1
            i -= 1
        if open_paren is None:
            return None
        name_end = open_paren
        while name_end > 0 and text[name_end - 1].isspace():
            name_end -= 1
        name_start = name_end
        while name_start > 0 and (text[name_start - 1].isalnum() or text[name_start - 1] == "_"):
            name_start -= 1
        if text[name_start:name_end] == "object":
            depth = 0
            j = open_paren
            while j < len(text):
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        argtext = text[open_paren + 1:j]
                        return name_start, j + 1, _parse_object_call_args(argtext)
                j += 1
            return None
        pos = open_paren
    return None


def _pick_turtle_kind(text: str, found: dict):
    """Keep the turtle editor that fits: inside `turtle3d(...)` the 3D one,
    inside `turtle(...)` the 2D one, and outside either call the 2D one
    only for a list with nothing 3D-only in it -- a list both read, such as
    `["move", 10]`, is offered as both."""
    for key, other in (("turtle", "turtle3d"), ("turtle3d", "turtle")):
        if key in found:
            call = _enclosing_call(text, found[key][0], found[key][1])
            name = call["name"] if call else None
            if name == key:
                found.pop(other, None)
    if "turtle" in found and "turtle3d" in found and _uses_3d_only(found["turtle3d"][2]):
        found.pop("turtle")


def find_editable_literals(text: str, offset: int, max_levels: int = 8) -> dict:
    """Find the innermost enclosing literal matching each of Path/Grid/
    Matrix/Affine/VNF/Region *independently*, as `{shape: (start, end,
    value)}` for whichever shapes match (mirrors `find_viewable_literals`).
    A single shared "first match wins" walk doesn't work here either: a
    grid's own row is itself a valid Path (a list of numeric points), so a
    shared walk would resolve "path" (the row) before ever reaching "grid"
    (the whole structure) for *any* click inside a row, not just when
    clicking exactly between rows."""
    found: dict = {}
    for start, end, value in _iter_enclosing_literals(text, offset, max_levels):
        if "path" not in found and _is_path(value):
            found["path"] = (start, end, value)
        if "grid" not in found and _is_grid(value):
            found["grid"] = (start, end, value)
        if "heightfield" not in found and _is_heightfield(value):
            found["heightfield"] = (start, end, value)
        if "matrix" not in found and _is_matrix(value):
            found["matrix"] = (start, end, value)
        if "affine" not in found and _is_affine_matrix(value):
            found["affine"] = (start, end, value)
        if "vnf" not in found and _is_vnf(value):
            found["vnf"] = (start, end, value)
        if "vnf_tile" not in found and _is_vnf_tile(value):
            found["vnf_tile"] = (start, end, value)
        if "region" not in found and _is_region(value):
            found["region"] = (start, end, value)
        if "turtle" not in found and _is_turtle(value):
            found["turtle"] = (start, end, value)
        if "turtle3d" not in found and _is_turtle3d(value):
            found["turtle3d"] = (start, end, value)
    # An object is a CALL, not a bracket literal, so it has its own
    # finder. entries is None when the call references variables, which
    # cannot be round-tripped through text -- view-only, never editable.
    call = _find_object_call(text, offset)
    if call is not None and call[2] is not None:
        found["object"] = call
    _pick_turtle_kind(text, found)
    return found


def find_viewable_literals(text: str, offset: int, max_levels: int = 8) -> dict:
    """Find the innermost enclosing literal matching each of List/VNF/Grid/
    Path *independently*, as `{shape: (start, end, value)}` for whichever
    shapes match (missing key if none). A single shared "first match wins"
    walk (as `find_editable_literal` uses for its four shapes) doesn't work
    here: `_is_list` is trivially true for any list, so it would almost
    always win at the very innermost bracket — e.g. clicking anywhere in an
    outer path `[[0,0],[1,0]]` usually lands inside one inner point's own
    brackets, which is already "a list", starving "View as Path..." of any
    reachable click position. Each shape instead gets its own walk-outward
    search, so e.g. "list" resolves to the innermost point while "path"
    keeps walking out to the enclosing path — matching what a user actually
    wants from each menu item."""
    found: dict = {}
    for start, end, value in _iter_enclosing_literals(text, offset, max_levels):
        if "list" not in found and _is_list(value):
            found["list"] = (start, end, value)
        if "vnf" not in found and _is_vnf(value):
            found["vnf"] = (start, end, value)
        if "vnf_tile" not in found and _is_vnf_tile(value):
            found["vnf_tile"] = (start, end, value)
        if "grid" not in found and _is_grid(value):
            found["grid"] = (start, end, value)
        if "path" not in found and _is_path(value):
            found["path"] = (start, end, value)
        if "heightfield" not in found and _is_heightfield(value):
            found["heightfield"] = (start, end, value)
        if "region" not in found and _is_region(value):
            found["region"] = (start, end, value)
        if "turtle" not in found and _is_turtle(value):
            found["turtle"] = (start, end, value)
        if "turtle3d" not in found and _is_turtle3d(value):
            found["turtle3d"] = (start, end, value)
    # Only when the call is all-literal. `object(other, [["k"]])` depends on
    # `other`, whose contents are not knowable from source text, so there is
    # nothing truthful to display -- offering it produced an EMPTY viewer.
    # Such a call is still fully inspectable at runtime, via the debugger's
    # own "View as Object..." on the variable, which sees resolved values.
    call = _find_object_call(text, offset)
    if call is not None and call[2] is not None:
        found["object"] = call
    _pick_turtle_kind(text, found)
    return found


# ---------------------------------------------------------------------------
# Factory helpers (used by debugger context menu)
# ---------------------------------------------------------------------------

def _open_list_viewer(title: str, value, parent=None):
    dlg = ListViewer(title, value, parent)
    dlg.show()


def _open_vnf_viewer(title: str, value, parent=None):
    dlg = VNFViewer(title, value, parent)
    dlg.show()


def _open_vnf_tile_viewer(title: str, value, parent=None):
    dlg = VNFTileViewer(title, value, parent)
    dlg.show()


def _open_path_viewer(title: str, value, parent=None):
    dlg = PathViewer(title, value, parent)
    dlg.show()


def _open_grid_viewer(title: str, value, parent=None):
    dlg = GridViewer(title, value, parent)
    dlg.show()


def _open_heightfield_viewer(title: str, value, parent=None):
    dlg = HeightfieldViewer(title, value, parent)
    dlg.show()


def _open_region_viewer(title: str, value, parent=None):
    dlg = RegionViewer(title, value, parent)
    dlg.show()


def _open_turtle_viewer(title: str, value, parent=None):
    TurtleViewer(title, value, parent).show()


def _open_turtle3d_viewer(title: str, value, parent=None):
    Turtle3DViewer(title, value, parent).show()


def _open_object_viewer(title: str, value, parent=None, entries=None):
    # Refuse rather than show an empty table. Nothing to display means the
    # caller had nothing to display -- an object() call that references a
    # variable, say -- and a viewer with no rows just looks broken.
    if entries is None and not _is_oscobject(value):
        return None
    dlg = ObjectViewer(title, value, parent, editable=False, entries=entries)
    dlg.show()
    return dlg


def _open_object_editor(title: str, entries, on_commit, parent=None):
    dlg = ObjectViewer(title, None, parent, editable=True, entries=entries)
    dlg.committed.connect(on_commit)
    dlg.show()


def _open_matrix_viewer(title: str, value, parent=None):
    dlg = MatrixViewer(title, value, parent)
    dlg.show()


def _open_affine_matrix_viewer(title: str, value, parent=None):
    dlg = AffineMatrixViewer(title, value, parent)
    dlg.show()


def build_viewer_menu(menu: QMenu, name: str, value, parent=None):
    """Add viewer actions to a QMenu based on the value's type."""
    if _is_oscobject(value):
        menu.addAction("View as Object...", lambda: _open_object_viewer(name, value, parent))
    if _is_list(value) or _is_oscobject(value):
        menu.addAction("View as List...", lambda: _open_list_viewer(name, value, parent))
    if _is_vnf(value):
        menu.addAction("View as VNF...", lambda: _open_vnf_viewer(name, value, parent))
        if _is_vnf_tile(value):
            menu.addAction("View as VNF Tile...", lambda: _open_vnf_tile_viewer(name, value, parent))
    else:
        # A geometry object from a render() expression carries the mesh as
        # separate keys rather than a 2-list, so it needs unwrapping first.
        geom_vnf = _geometry_object_vnf(value)
        if geom_vnf is not None:
            menu.addAction("View as VNF...",
                            lambda v=geom_vnf: _open_vnf_viewer(name, v, parent))
    geom_region = _geometry_object_region(value)
    if geom_region is not None:
        # `paths` are indices, so these contours are resolved, not unwrapped.
        menu.addAction("View as Region...",
                        lambda v=geom_region: _open_region_viewer(name, v, parent))
        if len(geom_region) == 1:
            # A single contour is also a valid path. Offering both follows
            # the same convention as a grid row that also reads as a path --
            # let the reader pick the interpretation they meant.
            menu.addAction("View as Path...",
                            lambda v=geom_region[0]: _open_path_viewer(name, v, parent))
    if _is_grid(value):
        menu.addAction("View as Grid...", lambda: _open_grid_viewer(name, value, parent))
    if _is_path(value):
        menu.addAction("View as Path...", lambda: _open_path_viewer(name, value, parent))
    if _is_region(value):
        menu.addAction("View as Region...", lambda: _open_region_viewer(name, value, parent))
    three_d = _is_turtle3d(value)
    if _is_turtle(value) and not (three_d and _uses_3d_only(value)):
        menu.addAction("View as Turtle Path...", lambda: _open_turtle_viewer(name, value, parent))
    if three_d:
        menu.addAction("View as 3D Turtle Path...", lambda: _open_turtle3d_viewer(name, value, parent))
    if _is_matrix(value):
        menu.addAction("View as Matrix...", lambda: _open_matrix_viewer(name, value, parent))
    if _is_affine_matrix(value):
        menu.addAction("View as Affine Transform...", lambda: _open_affine_matrix_viewer(name, value, parent))


def build_lexical_view_menu(menu: QMenu, text: str, literals: dict, parent=None):
    """Like `build_viewer_menu`, but for the per-shape results of
    `find_viewable_literals` — each shape (List/VNF/Grid/Path/Region) may
    come from a different span of `text`, since they're found
    independently. `text` is the full source text the (start, end) spans
    index into, used to look up each action's variable-name title via
    `_literal_display_name` (empty if the literal isn't a simple
    `name = <literal>` assignment). Deliberately excludes Matrix/Affine —
    those are Edit-only via `build_editor_menu`."""
    def _preview(start, end):
        return _literal_display_name(text, start)

    if "object" in literals:
        start, end, entries = literals["object"]
        menu.addAction("View as Object...", lambda start=start, end=end, entries=entries:
                       _open_object_viewer(_preview(start, end), None, parent, entries=entries))
    if "list" in literals:
        start, end, value = literals["list"]
        menu.addAction("View as List...", lambda start=start, end=end, value=value:
                       _open_list_viewer(_preview(start, end), value, parent))
    if "vnf" in literals:
        start, end, value = literals["vnf"]
        menu.addAction("View as VNF...", lambda start=start, end=end, value=value:
                       _open_vnf_viewer(_preview(start, end), value, parent))
    if "vnf_tile" in literals:
        start, end, value = literals["vnf_tile"]
        menu.addAction("View as VNF Tile...", lambda start=start, end=end, value=value:
                       _open_vnf_tile_viewer(_preview(start, end), value, parent))
    if "grid" in literals:
        start, end, value = literals["grid"]
        menu.addAction("View as Grid...", lambda start=start, end=end, value=value:
                       _open_grid_viewer(_preview(start, end), value, parent))
    if "heightfield" in literals:
        start, end, value = literals["heightfield"]
        menu.addAction("View as Heightfield...", lambda start=start, end=end, value=value:
                       _open_heightfield_viewer(_preview(start, end), value, parent))
    if "path" in literals:
        start, end, value = literals["path"]
        menu.addAction("View as Path...", lambda start=start, end=end, value=value:
                       _open_path_viewer(_preview(start, end), value, parent))
    if "region" in literals:
        start, end, value = literals["region"]
        menu.addAction("View as Region...", lambda start=start, end=end, value=value:
                       _open_region_viewer(_preview(start, end), value, parent))
    if "turtle" in literals:
        start, end, value = literals["turtle"]
        menu.addAction("View as Turtle Path...", lambda start=start, end=end, value=value:
                       _open_turtle_viewer(_preview(start, end), value, parent))
    if "turtle3d" in literals:
        start, end, value = literals["turtle3d"]
        menu.addAction("View as 3D Turtle Path...", lambda start=start, end=end, value=value:
                       _open_turtle3d_viewer(_preview(start, end), value, parent))


def _lock_parent_editor_while_open(dlg, parent):
    """Non-modal editable dialogs still need to prevent the user editing the
    source out from under the literal's tracked span while the dialog is
    open. Any flavor of Qt modal dialog (Application- or Window-modal) turned
    out to suppress the main window's ApplicationShortcut-context View-menu
    actions for its own embedded viewport too (confirmed: switching modality
    types didn't help — Qt suppresses non-active-window shortcuts whenever
    *any* modal widget is active, period). So instead: show non-modally, like
    the read-only viewers (whose shortcuts always worked), and lock the
    parent CodeEditor read-only for the dialog's lifetime instead of relying
    on Qt modality at all."""
    if parent is None:
        return
    parent.setReadOnly(True)
    dlg.finished.connect(lambda _result=0: parent.setReadOnly(False))


def _open_path_editor(title: str, value: list, on_commit, parent=None, is_2d=None,
                      text: str | None = None, start: int = 0, end: int = 0):
    """With `text`, the points are `text[start:end]` and `on_commit` takes
    `(new_text, s, e)`. Points that are `nurbs_interp()`'s open as that call
    draws them -- NURBS Interpolated, its degree and closed= -- with its
    corners=, and Save writes the corners back there too (#803)."""
    ctx = nurbs_interp_context(text, start, end) if text is not None else None
    preset = {"mode": "nurbs_interp", "degree": ctx["degree"], "closed": ctx["closed"]} if ctx else None
    dlg = PathViewer(title, value, parent, editable=True, is_2d=is_2d,
                     corners=(ctx or {}).get("corners"), preset=preset)
    if text is None:
        dlg.committed.connect(on_commit)
    else:
        dlg.committed.connect(lambda t: on_commit(
            *nurbs_write_back(text, start, end, t, ctx, dlg.corners_list())))
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_grid_editor(title: str, value: list, on_commit, parent=None):
    dlg = GridViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_heightfield_editor(title: str, value: list, on_commit, parent=None):
    dlg = HeightfieldViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_matrix_editor(title: str, value: list, on_commit, parent=None):
    dlg = MatrixViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_affine_matrix_editor(title: str, value: list, on_commit, parent=None):
    dlg = AffineMatrixViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_vnf_editor(title: str, value: list, on_commit, parent=None):
    dlg = VNFViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_vnf_tile_editor(title: str, value: list, on_commit, parent=None):
    dlg = VNFTileViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_region_editor(title: str, value: list, on_commit, parent=None):
    dlg = RegionViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def _open_turtle_editor(title: str, value: list, on_commit, parent=None,
                        text: str | None = None, start: int = 0, end: int = 0,
                        three_d: bool = False):
    """With `text`, the list is `text[start:end]` and `on_commit` takes
    `(new_text, s, e)`: the start point is read from the enclosing
    turtle() call's state= and written back there. Without it (a new
    literal) `on_commit` takes the list alone. `three_d` opens the
    turtle3d() editor instead."""
    context = turtle3d_call_context if three_d else turtle_call_context
    ctx = context(text, start, end) if text is not None else None
    name = "3D Turtle" if three_d else "Turtle"
    key = f"{name} Editor: {title}" if title else f"{name} Editor"
    zero = [0.0] * (3 if three_d else 2)
    movable, extra = True, {}
    if ctx is not None:
        note = ""
        first = ctx["start"] or zero
        if three_d and ctx["dir"] is not None:
            movable = False
            extra["state_dir"] = ctx["dir"]
            note = "state= gives the start direction, so the start stays at the origin."
        elif ctx["state"] is not None and ctx["start"] is None:
            movable = False
            note = "The start position is worked out by the script (state=), so it can't be moved here."
    else:
        first = _session_starts.get(key, zero)
        note = (f"Not in a {'turtle3d' if three_d else 'turtle'}() call, so a moved start position "
                "is kept for this session only. The start direction is saved.") if text is not None else ""
    line_start = text.rfind("\n", 0, start) + 1 if text is not None else 0
    indent = re.match(r"[ \t]*", text[line_start:]).group() if text is not None else ""
    dlg = (Turtle3DViewer if three_d else TurtleViewer)(
        title, value, parent, editable=True, start=first, start_movable=movable,
        start_note=note, indent=indent, **extra)

    def commit(list_text):
        if text is None:
            on_commit(list_text)
            return
        if ctx is None:
            _session_starts[key] = list(dlg.start_point())
        on_commit(*turtle_write_back(text, start, end, list_text, ctx, dlg.start_point(),
                                     wrap="move({})" if three_d else "{}"))
    dlg.committed.connect(commit)
    _lock_parent_editor_while_open(dlg, parent)
    dlg.show()


def build_editor_menu(menu: QMenu, text: str, literals: dict, on_commit, parent=None):
    """Add editable-viewer actions to a QMenu for the per-shape results of
    `find_editable_literals` — each shape (Path/Grid/Matrix/Affine/VNF/
    Region) may come from a different span of `text`, since they're found
    independently. `text` is the full source text the (start, end) spans
    index into, used to look up each action's variable-name title via
    `_literal_display_name` (empty if the literal isn't a simple
    `name = <literal>` assignment). `on_commit(new_text, start, end)` fires
    once per dialog, only when its Save button is clicked, with the span
    belonging to *that* shape's match. VNF editing is vertex positions
    only -- face topology is never edited, see `VNFViewer`."""
    def _preview(start, end):
        return _literal_display_name(text, start)

    if "object" in literals:
        start, end, entries = literals["object"]
        menu.addAction("Edit as Object...", lambda start=start, end=end, entries=entries:
                       _open_object_editor(_preview(start, end), entries,
                                            lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "path" in literals:
        start, end, value = literals["path"]
        menu.addAction("Edit as Path...", lambda start=start, end=end, value=value:
                       _open_path_editor(_preview(start, end), value, on_commit, parent,
                                         text=text, start=start, end=end))
    if "grid" in literals:
        start, end, value = literals["grid"]
        menu.addAction("Edit as Grid...", lambda start=start, end=end, value=value:
                       _open_grid_editor(_preview(start, end), value,
                                         lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "heightfield" in literals:
        start, end, value = literals["heightfield"]
        menu.addAction("Edit as Heightfield...", lambda start=start, end=end, value=value:
                       _open_heightfield_editor(_preview(start, end), value,
                                                 lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "matrix" in literals:
        start, end, value = literals["matrix"]
        menu.addAction("Edit as Matrix...", lambda start=start, end=end, value=value:
                       _open_matrix_editor(_preview(start, end), value,
                                           lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "affine" in literals:
        start, end, value = literals["affine"]
        menu.addAction("Edit as Affine Transform...", lambda start=start, end=end, value=value:
                       _open_affine_matrix_editor(_preview(start, end), value,
                                                   lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "vnf" in literals:
        start, end, value = literals["vnf"]
        menu.addAction("Edit as VNF...", lambda start=start, end=end, value=value:
                       _open_vnf_editor(_preview(start, end), value,
                                         lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "vnf_tile" in literals:
        start, end, value = literals["vnf_tile"]
        menu.addAction("Edit as VNF Tile...", lambda start=start, end=end, value=value:
                       _open_vnf_tile_editor(_preview(start, end), value,
                                              lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "region" in literals:
        start, end, value = literals["region"]
        menu.addAction("Edit as Region...", lambda start=start, end=end, value=value:
                       _open_region_editor(_preview(start, end), value,
                                            lambda t, s=start, e=end: on_commit(t, s, e), parent))
    if "turtle" in literals:
        start, end, value = literals["turtle"]
        menu.addAction("Edit as Turtle Path...", lambda start=start, end=end, value=value:
                       _open_turtle_editor(_preview(start, end), value, on_commit, parent,
                                           text, start, end))
    if "turtle3d" in literals:
        start, end, value = literals["turtle3d"]
        menu.addAction("Edit as 3D Turtle Path...", lambda start=start, end=end, value=value:
                       _open_turtle_editor(_preview(start, end), value, on_commit, parent,
                                           text, start, end, three_d=True))


# ---------------------------------------------------------------------------
# "Add data literal..." — creating a literal that isn't there yet
# ---------------------------------------------------------------------------

#: Matches a line that is an assignment with no value yet — `foo =`, which is
#: what the user has typed when they want an editor to *produce* the value.
#: The trailing `;` is optional because the editor's auto-indent/typing may or
#: may not have put one there; either way the whole line is replaced on save.
#: `$`-names are allowed, matching OpenSCAD's own identifier rules.
_EMPTY_ASSIGNMENT_RE = re.compile(
    r"^([ \t]*)(\$?[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*;?[ \t]*$")


#: `(label, shape, opener, seed)` for every editable shape, offered by
#: `build_new_literal_menu`.
#:
#: Paths and regions start empty: their editors build them a point at a time,
#: right-click "Add Vertex" appending where clicked. Every other editor edits
#: an existing value's structure rather than growing one (a VNF's faces, a
#: grid's rows), so its seed is the smallest value its own `_is_*` predicate
#: accepts, which keeps what it writes back re-editable through "Edit as...".
_NEW_LITERAL_SEEDS = [
    ("Object", "object", lambda *a: _open_object_editor(*a), [("key", 0)]),
    ("2D Path", "path", lambda *a: _open_path_editor(*a, is_2d=True), []),
    ("3D Path", "path", lambda *a: _open_path_editor(*a, is_2d=False), []),
    ("Region", "region", lambda *a: _open_region_editor(*a), [[]]),
    ("Turtle Path", "turtle", lambda *a: _open_turtle_editor(*a), ["move", 10]),
    ("3D Turtle Path", "turtle3d", lambda *a: _open_turtle_editor(*a, three_d=True), ["move", 10]),
    ("Grid", "grid", lambda *a: _open_grid_editor(*a),
     [[[0, 0, 0], [10, 0, 0]], [[0, 10, 0], [10, 10, 0]]]),
    ("Heightfield", "heightfield", lambda *a: _open_heightfield_editor(*a),
     [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]]),
    ("Matrix", "matrix", lambda *a: _open_matrix_editor(*a),
     [[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
    ("Affine Transform", "affine", lambda *a: _open_affine_matrix_editor(*a),
     [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]),
    ("VNF", "vnf", lambda *a: _open_vnf_editor(*a),
     [[[0, 0, 0], [10, 0, 0], [0, 10, 0]], [[0, 1, 2]]]),
    # A flat unit square, wound clockwise seen from +Z like every one of
    # BOSL2's own VNF textures.
    ("VNF Tile", "vnf_tile", lambda *a: _open_vnf_tile_editor(*a),
     [[[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], [[0, 3, 2, 1]]]),
]


def find_empty_assignment(text: str, offset: int):
    """`(name, indent, start, end)` when the line containing `offset` is an
    assignment with no value yet (`foo =`), else None.

    `start`/`end` span the whole line, not just the empty right-hand side:
    the caller rewrites the line as a unit, so the name, the `=` spacing and
    the terminating `;` all come out consistent no matter how they were
    typed.

    Deliberately lexical, like `find_editable_literals` — `foo =` is a syntax
    error, so there is no AST to ask, and this has to work on exactly the
    half-typed line the parser rejects.
    """
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    m = _EMPTY_ASSIGNMENT_RE.match(text[line_start:line_end])
    if not m:
        return None
    return m.group(2), m.group(1), line_start, line_end


def build_new_literal_menu(menu: QMenu, name: str, on_commit, parent=None):
    """Add one action per editable shape to `menu`, each opening that shape's
    editor on a minimal seed value (see `_NEW_LITERAL_SEEDS`).

    `on_commit(literal_text)` fires once, only when a dialog's Save button is
    clicked, with just the literal — the caller owns wrapping it back into an
    assignment, since only it knows the span being replaced.
    """
    for label, _shape, opener, seed in _NEW_LITERAL_SEEDS:
        menu.addAction(f"{label}...", lambda opener=opener, seed=seed, label=label:
                       opener(name or label, copy.deepcopy(seed), on_commit, parent))
