"""Helpers shared by more than one data viewer: number formatting, table
styling, vertex markers, click selection, the undo mixin, and syncing a
viewer's viewport with the main window's."""
from __future__ import annotations

import ast
import bisect
import copy
import math

import numpy as np

from PySide6.QtWidgets import (QApplication, QHBoxLayout, QTableWidget, QPushButton,
                               QComboBox)
from PySide6.QtCore import Qt, QItemSelectionModel
from PySide6.QtGui import QUndoStack, QUndoCommand, QKeySequence

from belfryscad.window.viewport import Viewport
from belfryscad.window.ui_colors import header_colors, on_appearance_change


def _fmt_short(v) -> str:
    from belfryscad.window.debugger import _fmt
    return _fmt(v)


def _parse_number(text: str):
    """Parse a table-cell edit as int or float, or None if not numeric —
    shared by the editable Matrix/Affine/Path/Grid viewers' itemChanged
    handlers to validate and revert bad input."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _format_value(value: list) -> str:
    """Serialize an edited (nested list of numbers) value back to OpenSCAD
    source, on Save. Plain recursive formatting -- always inline, no
    width-aware line-wrapping (the C++ backend has no pretty-printer to
    reach for that; still valid, still readable for the editable
    Matrix/Affine/Path/Grid viewers' typical small dimensions). Number
    formatting mirrors debugger._fmt's own `f"{v:g}"` convention."""
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(x) for x in value) + "]"
    return f"{float(value):g}"


def _is_list(v) -> bool:
    return isinstance(v, list)


def _is_oscobject(v) -> bool:
    # Duck-typed so both the C++ backend's OscObject and the legacy Python
    # evaluator's OscObject are accepted (both expose .data + .items()).
    return hasattr(v, "data") and hasattr(v, "items") and not isinstance(v, dict)


def _is_numeric_point(v) -> bool:
    return (_is_list(v)
            and len(v) in (2, 3)
            and all(isinstance(x, (int, float)) for x in v))


def _grid_row_offsets(grid_value: list) -> list[int]:
    """Cumulative per-row flat-index offsets for a (possibly ragged) grid:
    `offsets[r]` is the flat index of `grid_value[r][0]`, and `offsets[-1]`
    is the total point count. Row `r`'s valid column range is
    `[0, offsets[r + 1] - offsets[r])`, i.e. `[0, len(grid_value[r]))`."""
    offsets = [0]
    for row in grid_value:
        offsets.append(offsets[-1] + len(row))
    return offsets


def _grid_flat_to_rc(vi: int, row_offsets: list[int]) -> tuple[int, int]:
    """Convert a flat point index back to `(row, col)` using the cumulative
    per-row offsets from `_grid_row_offsets`. Rows are assumed non-empty, so
    every flat index in range maps to exactly one row."""
    r = bisect.bisect_right(row_offsets, vi) - 1
    r = max(0, min(r, len(row_offsets) - 2))
    return r, vi - row_offsets[r]


# ---------------------------------------------------------------------------
# Affine editor transform tools -- compose a new operation onto the current
# matrix (M_new = N . M, i.e. N is applied AFTER the existing transform),
# rather than decomposing the matrix into components (decomposition can't
# recover the original authored order -- a composed matrix has no memory
# of the operations that built it). Rotate/Scale support an explicit pivot
# via _pivot_about (T(c) . N . T(-c)); Translate needs none (translation
# commutes with any pivot wrap); Skew is applied about the origin only,
# matching OpenSCAD convention.
# ---------------------------------------------------------------------------

def _identity_matrix(n: int) -> list:
    return [[1.0 if r == c else 0.0 for c in range(n)] for r in range(n)]


def _header_style() -> str:
    """QHeaderView section styling for the current appearance.

    The colours used to be a fixed near-white, which on a Mac in Dark Mode
    left bright header bars sitting on top of dark tables. The text colour
    is now stated explicitly in both appearances rather than left to
    inherit, since a stylesheet that sets only a background hands you
    whatever foreground the style felt like.
    """
    bg, border, fg = header_colors()
    return (
        "QHeaderView::section {"
        f"  background-color: {bg};"
        f"  border: 1px solid {border};"
        f"  color: {fg};"
        "  padding: 2px 4px;"
        "}"
    )


def _style_table_headers(table: QTableWidget):
    """Style `table`'s headers, and restyle them on every appearance change.

    Registered here rather than at each call site, so every table in the
    app -- viewers, debugger panes, anything added later -- follows a
    light/dark switch just by calling this the way it always did.
    """
    def apply():
        style = _header_style()
        table.horizontalHeader().setStyleSheet(style)
        table.verticalHeader().setStyleSheet(style)

    apply()
    on_appearance_change(table, apply)


def _size_combo_to_widest_item(combo: QComboBox, extra: int = 40):
    """`QComboBox.setSizeAdjustPolicy(AdjustToContents)` alone still clips
    the widest item's text on macOS's native (Aqua) style -- the style's
    dropdown-arrow/margin metrics eat into the content box that
    `AdjustToContents` sized from. Force a minimum width from the widest
    item's actual text metrics plus a fixed buffer for the arrow instead of
    trusting the style's own size hint."""
    fm = combo.fontMetrics()
    widest = max((fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
    combo.setMinimumWidth(widest + extra)


_DODECA_PHI = (1 + 5 ** 0.5) / 2


# 20 vertices of a regular dodecahedron, unit circumradius (sqrt(3) before
# normalization -- divided out in _dodecahedron_faces): 8 (+-1,+-1,+-1)
# cube corners, plus 3 groups of 4 forming golden-ratio rectangles in each
# coordinate plane. Standard construction.
_DODECA_VERTS = [
    (s1, s2, s3) for s1 in (1, -1) for s2 in (1, -1) for s3 in (1, -1)
] + [
    (0, s1 / _DODECA_PHI, s2 * _DODECA_PHI) for s1 in (1, -1) for s2 in (1, -1)
] + [
    (s1 / _DODECA_PHI, s2 * _DODECA_PHI, 0) for s1 in (1, -1) for s2 in (1, -1)
] + [
    (s1 * _DODECA_PHI, 0, s2 / _DODECA_PHI) for s1 in (1, -1) for s2 in (1, -1)
]


# 12 pentagonal faces, each a 5-tuple of _DODECA_VERTS indices in winding
# order. Verified offline (see PR): all vertices 3-regular, exactly 30
# unique edges each shared by exactly 2 faces, every face planar to float
# precision -- see tests/test_data_viewers.py::TestDodecahedronFaces.
_DODECA_FACES = [
    (0, 8, 10, 2, 16), (0, 16, 17, 1, 12), (0, 12, 14, 4, 8),
    (8, 4, 18, 6, 10), (10, 6, 15, 13, 2), (2, 13, 3, 17, 16),
    (1, 17, 3, 11, 9), (1, 9, 5, 14, 12), (4, 14, 5, 19, 18),
    (6, 18, 19, 7, 15), (13, 15, 7, 11, 3), (5, 9, 11, 7, 19),
]


def _dodecahedron_faces(r: float, is_2d: bool) -> list:
    """Triangle triples (as vertex-offset vectors) forming a dodecahedron
    point marker of circumradius `r` — a flat regular 12-gon (dodecagon) in
    the XY plane when `is_2d`, a full 12-pentagon dodecahedron (fan-
    triangulated to 36 triangles) otherwise. Shared by every viewport that
    draws corner/vertex markers (`_GridViewport`, `_PathViewport`,
    `_AffineViewport`, `_VNFViewport`, `_RegionViewport`). The `is_2d` case
    is safe to light (see `_lit_marker_triangles`) because those viewers
    are permanently locked to a top-down camera, so the marker's single
    +Z-facing normal always faces the eye."""
    if is_2d:
        n = 12
        pts = [np.array([r * math.cos(2 * math.pi * i / n),
                          r * math.sin(2 * math.pi * i / n), 0.0]) for i in range(n)]
        center = np.zeros(3)
        return [(center, pts[i], pts[(i + 1) % n]) for i in range(n)]

    verts = [np.array(v) / math.sqrt(3.0) * r for v in _DODECA_VERTS]
    tris = []
    for face in _DODECA_FACES:
        v0 = verts[face[0]]
        for i in range(1, 4):
            tris.append((v0, verts[face[i]], verts[face[i + 1]]))
    return tris


def _marker_radius_for_point(vp: "Viewport", world_point) -> float:
    """Screen-space-constant marker radius (in world units) for a marker
    positioned AT `world_point`, so it stays ~12px on screen regardless of
    zoom -- in *both* projection modes and regardless of which vertex is
    being sized. Shared by every viewport that rebuilds marker geometry
    on zoom (`_GridViewport`, `_PathViewport`, `_AffineViewport`,
    `_VNFViewport`, `_RegionViewport`).

    In orthographic mode, apparent size never depends on a point's
    distance from the eye at all (that's the definition of orthographic),
    so using the camera's target distance (`cam.distance`) for every
    marker is exactly correct — and this is also why `Camera.
    projection_matrix`'s orthographic half-height is itself defined as
    `distance * tan(fov/2)`, matching perspective's apparent size at the
    target depth for a seamless toggle between modes.

    In perspective mode, apparent size genuinely *does* scale with each
    point's own eye-distance, not the target's — a fixed world-space
    radius computed from `cam.distance` alone renders too big for any
    vertex closer to the eye than the target, and too small for any
    vertex farther (most noticeable when a 3D path/grid/mesh is orbited
    or zoomed toward one end, rather than viewed dead-on from directly
    above like the always-2D-locked `PathViewer`/`GridViewer` cases,
    where every vertex is coplanar with — and thus equidistant from —
    the camera). Using each point's actual distance from `cam.
    eye_position()` here fixes that."""
    cam = vp._renderer.camera
    vh = vp.height()
    depth = cam.distance if cam.orthographic else float(np.linalg.norm(np.asarray(world_point) - cam.eye_position()))
    if vh > 0:
        world_per_px = 2.0 * depth * math.tan(math.radians(cam.fov / 2)) / vh
    else:
        world_per_px = depth * 0.003
    return world_per_px * 6


def _lit_marker_triangles(pt, r: float, unit_faces: list, color: np.ndarray) -> list:
    """Build (position, normal, color) interleaved rows for one marker,
    phong-lit via a flat per-triangle normal (cross product of two edges,
    same convention as `_VNFViewport._rebuild_highlight`). `unit_faces` is
    the (v0, v1, v2) offset-vector triangle list from `_dodecahedron_faces`/
    `_diamond_faces`/`_triangle_faces`; `color` is that marker's RGB.
    Shared by every marker-building call site so the cross-product-normal
    step isn't repeated at each one -- feeds `SceneRenderer.upload_points`'
    `[x,y,z, nx,ny,nz, r,g,b]` row format."""
    rows = []
    for v0, v1, v2 in unit_faces:
        p0, p1, p2 = pt + v0 * r, pt + v1 * r, pt + v2 * r
        n = np.cross(p1 - p0, p2 - p0)
        ln = np.linalg.norm(n)
        if ln > 0:
            n = n / ln
        rows.append(np.concatenate([p0, n, color]))
        rows.append(np.concatenate([p1, n, color]))
        rows.append(np.concatenate([p2, n, color]))
    return rows


def _marker_radii_for_points(vp: "Viewport", points) -> np.ndarray:
    """`_marker_radius_for_point` for many points at once.

    Same arithmetic, done as array operations. Orthographic depth does not
    vary per point at all (that is the definition of it), so that case is a
    single value broadcast; perspective needs each point's own eye-distance.
    """
    cam = vp._renderer.camera
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    if cam.orthographic:
        depth = np.full(len(pts), float(cam.distance), dtype=np.float32)
    else:
        eye = np.asarray(cam.eye_position(), dtype=np.float32)
        depth = np.linalg.norm(pts - eye, axis=1)
    vh = vp.height()
    if vh > 0:
        world_per_px = 2.0 * depth * math.tan(math.radians(cam.fov / 2)) / vh
    else:
        world_per_px = depth * 0.003
    return (world_per_px * 6).astype(np.float32)


def _lit_marker_field(points, radii, unit_faces: list, colors) -> np.ndarray:
    """Every marker in one array, built without a Python loop over points.

    `_lit_marker_triangles` is fine for the handful of selected markers,
    but the unselected ones are the whole field: an image-derived
    heightfield is routinely tens of thousands of points, and building
    those a triangle at a time cost SECONDS -- 6.3s for 10,000 markers,
    which is the entire interaction budget spent before anything is drawn.

    The saving comes from what every marker has in common. They are the
    same unit shape, so the offsets are shared; and a normal is unchanged
    by uniform positive scaling, so each triangle's normal is computed once
    for the shape rather than once per marker. Only the positions actually
    differ, and those are one broadcast add.

    `colors` is either one RGB row for all of them or one per point.
    Returns the same `[x,y,z, nx,ny,nz, r,g,b]` rows `upload_points` wants,
    in the same order `_lit_marker_triangles` would have produced.
    """
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    if len(pts) == 0:
        return np.zeros((0, 9), dtype=np.float32)

    unit = np.asarray(unit_faces, dtype=np.float32).reshape(-1, 3, 3)
    normals = np.cross(unit[:, 1] - unit[:, 0], unit[:, 2] - unit[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals),
                        where=lengths > 0)
    offsets = unit.reshape(-1, 3)                       # (V, 3)
    normals = np.repeat(normals, 3, axis=0)             # (V, 3)

    radii = np.asarray(radii, dtype=np.float32).reshape(-1, 1, 1)
    out = np.empty((len(pts), len(offsets), 9), dtype=np.float32)
    out[:, :, 0:3] = pts[:, None, :] + offsets[None, :, :] * radii
    out[:, :, 3:6] = normals
    out[:, :, 6:9] = np.asarray(colors, dtype=np.float32).reshape(len(pts), 1, 3) \
        if np.ndim(colors) == 2 and len(np.asarray(colors)) == len(pts) else \
        np.asarray(colors, dtype=np.float32)
    return out.reshape(-1, 9)


def _ray_plane_axis_locked(ray_origin: np.ndarray, ray_dir: np.ndarray,
                            plane_point: np.ndarray, lock_axis: int) -> np.ndarray | None:
    """Intersect a world-space ray with the axis-aligned plane through
    `plane_point` whose normal is world axis `lock_axis` (0=X, 1=Y, 2=Z), or
    None if the ray is parallel to it. Used to reproject a dragged screen
    position back to world space for vertex-dragging: 2D data always locks
    Z (embedded at Z=0 by `load_path`/`load_grid`, camera locked top-down);
    3D data locks whichever axis `_view_locked_axis` picks for the current
    camera angle."""
    denom = ray_dir[lock_axis]
    if abs(denom) < 1e-9:
        return None
    t = (plane_point[lock_axis] - ray_origin[lock_axis]) / denom
    hit = ray_origin + t * ray_dir
    hit[lock_axis] = plane_point[lock_axis]  # avoid float drift on the locked axis
    return hit


def _unlocked_plane_name(lock_axis: int) -> str:
    """Name of the drag plane spanned by the two axes *other* than
    `lock_axis` (e.g. lock_axis=2 (Z) -> "XY") — shown as a `_show_delta`
    overlay while a vertex drag is in progress, so it's clear which plane
    a 3D drag is currently constrained to."""
    return "".join("XYZ"[i] for i in range(3) if i != lock_axis)


def _vertex_click_mode(modifiers: Qt.KeyboardModifier) -> str:
    """Standardized vertex-click selection mode, shared by every editable
    viewport with a selectable vertex list (Path/Grid/VNF/Region): no
    modifier replaces the selection with just the clicked vertex, Shift
    adds it, Cmd (Control in the Qt enum) toggles it. Only ever consulted
    for a plain click (no drag) -- Cmd is intercepted earlier, at
    mousePressEvent, to arm a vertex-move drag instead; if that drag never
    actually moves the vertex, the release handler hardcodes "toggle"
    directly rather than calling this (Cmd's intent there is unambiguous)."""
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return "add"
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        return "toggle"
    return "replace"


def _apply_click_selection(table: QTableWidget, row: int, mode: str):
    """Apply a standardized vertex-click selection mode to `table`'s row
    selection -- shared by every editable viewport's
    `_on_viewport_vertex_clicked` handler. "replace": deselect everything
    else, select just `row`. "add" (Shift-click): add `row` to the
    current selection. "toggle" (Cmd-click): flip `row`'s own membership
    in the selection. `row < 0` (clicked blank space) only has an effect
    for "replace", clearing the selection -- "add"/"toggle" on blank
    space is a no-op, nothing to add or toggle."""
    if row < 0:
        if mode == "replace":
            table.clearSelection()
        return
    if mode == "replace":
        table.clearSelection()
        table.selectRow(row)
        return
    sel = table.selectionModel()
    index = table.model().index(row, 0)
    flag = (QItemSelectionModel.SelectionFlag.Toggle if mode == "toggle"
            else QItemSelectionModel.SelectionFlag.Select)
    sel.select(index, flag | QItemSelectionModel.SelectionFlag.Rows)


def _sync_viewport_to_main_window(vp):
    """Match a freshly-constructed data-viewer viewport's colors and FOV
    to the user's current settings. Without this, a newly-opened dialog
    is stuck on `SceneRenderer`/`Camera`'s bare constructor defaults (a
    different color theme, a different FOV) until the user happens to
    open Preferences while the dialog is open -- the only other place
    these get applied is `MainWindow._apply_preferences`, whose own sync
    loop (`hasattr(w, '_vp')`) only ever reaches *already-open* dialogs,
    never one that doesn't exist yet at the moment preferences last
    changed. Color theme is read directly from the persisted preference
    (works even with no `MainWindow` instance around, e.g. future
    standalone/test usage). FOV has no persisted preference of its own
    -- it's only ever changed live, via Shift+wheel or a script's `$vpf`
    -- so it's copied from whichever `MainWindow` viewport is currently
    open; if none is, the viewport's own already-set default is left
    alone."""
    from belfryscad.window.color_themes import COLOR_THEMES, DEFAULT_COLOR_THEME, all_themes
    from belfryscad.window.preferences import load_preference
    theme = all_themes().get(load_preference("viewport/colorTheme"), COLOR_THEMES[DEFAULT_COLOR_THEME])
    vp._renderer.bg_color = theme["background"]
    vp._renderer._default_color = theme["object"]
    vp._renderer.axes_color = theme["axes"]
    vp._renderer.unselected_vertex_color = theme["unselected_vertex"]
    for w in QApplication.topLevelWidgets():
        if hasattr(w, '_viewport'):
            vp._renderer.camera.fov = w._viewport._renderer.camera.fov
            break


# ---------------------------------------------------------------------------
# Shared undo/redo for the editable literal viewers (Matrix/Affine/VNF/
# Path/Grid/Region) -- every mutation (table edit, viewport vertex drag,
# add/delete, keyboard nudge) gets its own dialog-scoped undo/redo stack,
# independent of the main editor's document-level QUndoStack (see
# main_window.py's _TextEditCmd/_GizmoCmd -- same QUndoStack/QUndoCommand
# idiom, just scoped to this dialog instead of the main window).
# ---------------------------------------------------------------------------

class _ViewerEditCmd(QUndoCommand):
    """Generic snapshot undo command shared by all six editable viewers.
    These values are small (a handful to a few hundred floats), so storing
    full before/after copies is simpler than inverse-operation commands
    and the cost is irrelevant. Unlike main_window.py's _TextEditCmd, no
    _first_redo guard is needed: at push time the dialog's value is still
    "before" (the caller passes the proposed "after" as a plain argument,
    it isn't applied until push() triggers this command's redo()), so
    redo() unconditionally applying `_after` is correct."""

    def __init__(self, dialog, before, after, label):
        super().__init__(label)
        self._dialog = dialog
        self._before = before
        self._after = after

    def undo(self):
        self._dialog._apply_value(copy.deepcopy(self._before))

    def redo(self):
        self._dialog._apply_value(copy.deepcopy(self._after))


class _UndoableViewerMixin:
    """Mixed into the six editable literal-viewer dialogs. Subclasses must
    implement:
        _get_value(self) -> the current value (whatever attribute holds it)
        _apply_value(self, value) -> replace that attribute + refresh the
            table/viewport display to match

    Call _setup_undo(value) once, in __init__ after the value attribute
    and display widgets exist, to wire everything up."""

    def _setup_undo(self, value):
        self._original_value = copy.deepcopy(value)
        self._undo_stack = QUndoStack(self)
        self._undo_action = self._undo_stack.createUndoAction(self, "Undo")
        self._redo_action = self._undo_stack.createRedoAction(self, "Redo")
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self._redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self._undo_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._redo_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.addAction(self._undo_action)
        self.addAction(self._redo_action)
        self._live_before = None   # snapshot while a drag/nudge is in progress
        self._suspend_main_window_undo_shortcuts()
        self.finished.connect(lambda _r=0: self._restore_main_window_undo_shortcuts())

    def _suspend_main_window_undo_shortcuts(self):
        """Cmd+Z/Cmd+Shift+Z on this dialog's own QUndoStack would otherwise
        be genuinely ambiguous with the main window's own ApplicationShortcut-
        context Undo/Redo actions (confirmed live: Qt fires *neither* action
        and logs "Ambiguous shortcut overload" -- ShortcutContext specificity
        does NOT arbitrate this, contrary to what you might expect). Clearing
        the main window's shortcuts (not disabling the actions -- their
        enabled state is auto-managed by its own QUndoStack and shouldn't be
        touched) for the dialog's lifetime resolves the ambiguity; restored
        on close. Duck-typed (`getattr`) rather than importing MainWindow, to
        avoid a circular import from this module. Walks `parentWidget()`
        rather than using `self.window()` -- a QDialog is its own
        top-level window (has its own OS window frame) even with a
        `parent` set, so `.window()` returns the dialog itself, not the
        actual MainWindow ancestor `parent` was constructed with."""
        win = self.parentWidget()
        while win is not None and not hasattr(win, '_act_undo'):
            win = win.parentWidget()
        self._suspended_main_shortcuts = []
        for name in ('_act_undo', '_act_redo'):
            action = getattr(win, name, None)
            if action is not None:
                self._suspended_main_shortcuts.append((action, action.shortcut()))
                action.setShortcut(QKeySequence())

    def _restore_main_window_undo_shortcuts(self):
        for action, shortcut in getattr(self, '_suspended_main_shortcuts', []):
            action.setShortcut(shortcut)

    def _commit_value(self, new_value, label="Edit"):
        """Push one undo step. All discrete mutation paths (table edits,
        tool applies, resets, one-shot add/delete) funnel through here."""
        before = copy.deepcopy(self._get_value())
        after = copy.deepcopy(new_value)
        if before == after:
            return
        self._undo_stack.push(_ViewerEditCmd(self, before, after, label))

    def _begin_live_edit(self):
        """Call once when a continuous edit starts (Cmd+drag press, or
        immediately before applying a keyboard nudge)."""
        self._live_before = copy.deepcopy(self._get_value())

    def _end_live_edit(self, label="Edit"):
        """Call once when a continuous edit ends (Cmd+drag release, or
        immediately after applying a keyboard nudge) -- pushes exactly
        ONE undo step for the whole gesture, not one per live frame."""
        if self._live_before is None:
            return
        before, self._live_before = self._live_before, None
        after = copy.deepcopy(self._get_value())
        if before == after:
            return
        self._undo_stack.push(_ViewerEditCmd(self, before, after, label))

    def _on_reset_original(self):
        self._commit_value(self._original_value, "Reset to Original")

    def _make_reset_button_row(self) -> QHBoxLayout:
        """Reset to Original, meant to be inserted into the dialog's
        existing button row. No dedicated Undo/Redo buttons -- Cmd+Z/
        Cmd+Shift+Z (`_undo_action`/`_redo_action`, still wired up in
        `_setup_undo` via `self.addAction(...)`) are always expected to
        be available regardless of any visible button. Reset-to-Identity
        (Matrix/Affine only, where "identity" is a meaningful concept) is
        added separately by those two classes."""
        row = QHBoxLayout()
        reset = QPushButton("Reset to Original")
        reset.clicked.connect(self._on_reset_original)
        row.addWidget(reset)
        return row


# ---------------------------------------------------------------------------
# object() call parsing, for the Object viewer/editor
# ---------------------------------------------------------------------------
#
# Unlike every other editable shape here, an object is not written as a
# bracket literal -- it is a CALL, `object(a=42, b=53)`. So it needs its own
# span finder and its own text round-trip rather than riding on
# `_iter_enclosing_literals`/`ast.literal_eval`.

_SCAD_WORD_LITERALS = {"true": "True", "false": "False", "undef": "None"}


def _scad_literal_to_python(src: str):
    """`ast.literal_eval` for an OpenSCAD literal, or None if it isn't one.

    OpenSCAD spells three literals differently from Python. Substituting
    them has to skip string contents -- a plain `str.replace` would turn
    `"undefined"` into `"Noneined"` -- so this walks the text and only
    rewrites whole words found outside quotes.

    Returns `(True, value)` on success so that a legitimately-parsed None
    (OpenSCAD `undef`) is distinguishable from a parse failure.
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                out.append(src[i])
                if src[i] == "\\" and i + 1 < n:   # keep escapes intact
                    out.append(src[i + 1])
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            out.append(_SCAD_WORD_LITERALS.get(word, word))
            i = j
            continue
        out.append(c)
        i += 1
    try:
        return True, ast.literal_eval("".join(out))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return False, None
