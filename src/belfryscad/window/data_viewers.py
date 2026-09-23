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


def _iter_enclosing_literals(text: str, offset: int, max_levels: int = 8):
    """Yield `(start, end, value)` innermost-to-outermost for each enclosing
    `[...]` literal around `offset` that `ast.literal_eval`s to a list
    (`end` is exclusive). Levels that fail to parse — identifiers, calls,
    OpenSCAD-only syntax like ranges or `true`/`false`/`undef` — are skipped
    but the walk continues outward past them. Stops after `max_levels` or
    once there's no further enclosing `[`."""
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
    # An object is a CALL, not a bracket literal, so it has its own
    # finder. entries is None when the call references variables, which
    # cannot be round-tripped through text -- view-only, never editable.
    call = _find_object_call(text, offset)
    if call is not None and call[2] is not None:
        found["object"] = call
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
    # Only when the call is all-literal. `object(other, [["k"]])` depends on
    # `other`, whose contents are not knowable from source text, so there is
    # nothing truthful to display -- offering it produced an EMPTY viewer.
    # Such a call is still fully inspectable at runtime, via the debugger's
    # own "View as Object..." on the variable, which sees resolved values.
    call = _find_object_call(text, offset)
    if call is not None and call[2] is not None:
        found["object"] = call
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


def _open_path_editor(title: str, value: list, on_commit, parent=None):
    dlg = PathViewer(title, value, parent, editable=True)
    dlg.committed.connect(on_commit)
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
                       _open_path_editor(_preview(start, end), value,
                                         lambda t, s=start, e=end: on_commit(t, s, e), parent))
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
#: A literal being *created* has no source text to parse, and every editor is
#: built to edit an existing value rather than to start from nothing — so each
#: seed is the smallest value its own `_is_*` predicate already accepts. That
#: is deliberately the acceptance threshold and not something prettier: it
#: guarantees what the editor writes back is immediately re-editable through
#: "Edit as...", which a degenerate seed (an empty path, a 1x1 heightfield, an
#: `object()` with no entries) is not.
_NEW_LITERAL_SEEDS = [
    ("Object", "object", lambda *a: _open_object_editor(*a), [("key", 0)]),
    ("Path", "path", lambda *a: _open_path_editor(*a),
     [[0, 0], [10, 0], [10, 10]]),
    ("Region", "region", lambda *a: _open_region_editor(*a),
     [[[0, 0], [10, 0], [10, 10], [0, 10]]]),
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
