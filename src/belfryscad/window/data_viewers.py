"""
Data viewer windows launched from the debugger's variable context menu.

- ListViewer: scrollable table for lists and OscObject values
- VNFViewer: 3D mesh viewer for [vertices, faces] structures
- PathViewer: 2D/3D path viewer with point markers and connecting lines
- GridViewer: 3D viewer for (possibly ragged) lists of lists of points
- MatrixViewer: table view for square 2x2-5x5 lists of lists of numbers
- AffineMatrixViewer: table + viewport for 3x3/4x4 homogeneous affine
  transform matrices, visualizing their effect on a reference square/cube
- RegionViewer: 2D viewer for a list of closed polygon paths (a "region"),
  with even-odd fill semantics -- nested paths alternate solid/hole
"""
from __future__ import annotations
import ast
import bisect
import math
import re
import numpy as np
import manifold3d as m3d

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QCheckBox, QMenu, QLabel, QPushButton,
    QSplitter, QTabWidget, QWidget, QComboBox, QLineEdit,
)
from PySide6.QtCore import Qt, QPoint, Signal, QTimer
from PySide6.QtGui import QFont, QMouseEvent

from belfryscad.window.viewport import Viewport


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
    source, on Save. Builds a synthetic ListComprehension/NumberLiteral AST
    and renders it with the parser's own pretty-printer (`to_openscad`)
    rather than hand-rolled string joining — same pattern already used by
    `debugger._pretty_assignment` for formatting debug values.

    `to_openscad` only routes a node through the real width-aware formatter
    (which decides inline vs. multi-line list layout) for specific statement
    types like `Assignment` — a bare expression node falls through to a
    naive `str(node)` with no line-wrapping at all. So this wraps the value
    in a throwaway `Assignment` to reach the real formatter, then strips the
    synthetic "name = "/";" back off."""
    from openscad_lalr_parser.nodes import ListComprehension, NumberLiteral, Position, Assignment, Identifier
    from openscad_lalr_parser import to_openscad

    pos = Position(origin="<synthetic>", line=0, column=0)

    def to_ast(v):
        if isinstance(v, list):
            return ListComprehension(pos, [to_ast(x) for x in v])
        return NumberLiteral(pos, float(v))

    name = "_v"
    node = Assignment(pos, Identifier(pos, name), to_ast(value))
    text = to_openscad([node])
    prefix = f"{name} = "
    assert text.startswith(prefix) and text.endswith(";"), text
    return text[len(prefix):-1]


def _is_list(v) -> bool:
    return isinstance(v, list)


def _is_oscobject(v) -> bool:
    from belfryscad.engine.evaluator import OscObject
    return isinstance(v, OscObject)


def _is_numeric_point(v) -> bool:
    return (_is_list(v)
            and len(v) in (2, 3)
            and all(isinstance(x, (int, float)) for x in v))


def _is_path(v) -> bool:
    return (_is_list(v)
            and len(v) >= 2
            and all(_is_numeric_point(p) for p in v))


def _is_grid(v) -> bool:
    """A grid is a list of >= 2 rows, each a non-empty list of points. Rows
    need not all be the same length — GridViewer supports ragged/non-
    rectangular grids (e.g. a cone's single-point apex row next to a
    multi-point base row)."""
    return (_is_list(v)
            and len(v) >= 2
            and all(_is_list(row) and len(row) >= 1
                    and all(_is_numeric_point(p) for p in row) for row in v))


def _is_region(v) -> bool:
    """A region is a list of >= 1 closed 2D polygon paths, each with >= 3
    points, representing non-overlapping perimeters under even-odd fill
    semantics -- a path nested inside another alternates solid/hole (e.g.
    three concentric circles = a central disc surrounded by a ring).
    Points are strictly 2D -- no 3D regions.

    Structurally identical to `_is_grid` whenever there are >= 2 paths (a
    2D-only grid *is* a list of point-lists too) -- unlike `_is_grid`,
    which requires >= 2 rows, a region may have just 1 path (no holes),
    which is the one case that doesn't also read as a grid. For the
    overlapping case, both "Edit as Grid..." and "Edit as Region..." are
    offered and the user picks the intended interpretation -- the same
    precedent as a grid row also being a valid path, or a square matrix
    also being a valid affine transform, elsewhere in this file."""
    return (_is_list(v) and len(v) >= 1
            and all(_is_list(path) and len(path) >= 3
                    and all(_is_numeric_point(p) and len(p) == 2 for p in path)
                    for path in v))


def _region_fill_mesh(paths: list) -> tuple[np.ndarray, np.ndarray] | None:
    """Tessellate a region's paths (even-odd fill -- nested paths
    alternate solid/hole) into a flat triangle mesh for `upload_mesh`, by
    reusing `manifold3d.CrossSection`'s own fill-rule/triangulation
    rather than hand-rolling one: build a `CrossSection` from the raw
    paths under `FillRule.EvenOdd`, extrude a hairline sliver (same
    `evaluator.to_renderable_bodies` trick used to render a lone 2D shape
    in the main viewport), and read the resulting `Manifold`'s mesh back
    as flat (Z=0) triangles. Returns `None` if the paths enclose no area
    (e.g. degenerate/self-cancelling input)."""
    section = m3d.CrossSection([np.array(p, dtype=np.float64) for p in paths], m3d.FillRule.EvenOdd)
    manifold = m3d.Manifold.extrude(section, 1e-3)
    mesh = manifold.to_mesh()
    verts = np.array(mesh.vert_properties, dtype=np.float32)[:, :3]
    tris = np.array(mesh.tri_verts, dtype=np.int32)
    if len(verts) == 0 or len(tris) == 0:
        return None
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    tris_pos = np.concatenate([v0, v1, v2], axis=1).reshape(-1, 3)
    tris_pos[:, 2] = 0.0
    tris_norm = np.zeros_like(tris_pos)
    tris_norm[:, 2] = 1.0
    return tris_pos, tris_norm


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


def _grid_is_triangular(row_lens: list[int], row_wrap: bool = False) -> bool:
    """A grid is "triangular" (as opposed to a plain rectangular quad grid)
    if any two adjacent rows have different lengths — e.g. a cone's
    single-point apex row next to a wider base row, or a triangular-number
    row progression (1, 2, 3, ...). `_GridViewport` draws a third,
    diagonal, line direction for such grids in addition to the row/column
    lines every grid gets."""
    rows = len(row_lens)
    r_range = rows if row_wrap else rows - 1
    for r in range(r_range):
        r_next = (r + 1) % rows
        if row_lens[r] != row_lens[r_next]:
            return True
    return False


def _grid_fan_spec(len_a: int, len_b: int, col_wrap: bool):
    """Describes the "fan" needed to give every point a face/line when two
    adjacent grid rows (lengths `len_a`, `len_b`) differ in length — e.g. a
    cone's single-point apex row fanning out to its multi-point base row, or
    a triangular-number row progression's one extra point per step. The
    shared prefix (columns `[0, min(len_a, len_b))`) is already handled by
    ordinary quad/column logic; this covers only the longer row's remaining
    points, anchored at the shorter row's last shared-index point.

    Returns `None` if `len_a == len_b` (no fan needed — a plain quad
    connects the two rows completely). Otherwise returns
    `(anchor_in_a, anchor_col, longer_len, ks)`:
    - `anchor_in_a`: whether the anchor point belongs to row A (True) or
      row B (False) — i.e. whether A or B is the shorter row.
    - `anchor_col`: the anchor's column index (`min(len_a, len_b) - 1`,
      valid in both rows since it's within the shared prefix).
    - `longer_len`: the longer row's length.
    - `ks`: 0-based column indices into the *longer* row; each `k` is one
      fan triangle `(longer[k], anchor, longer[k+1])` if `anchor_in_a`,
      else `(anchor, longer[k], longer[k+1])` — and, for lines, one spoke
      edge `(anchor, longer[(k+1) % longer_len])` (the edge `anchor` to
      `longer[k]` for the first `k` is already drawn by the shared-prefix
      column line).
    """
    if len_a == len_b:
        return None
    shared = min(len_a, len_b)
    anchor_in_a = len_a <= len_b
    longer_len = len_b if anchor_in_a else len_a
    longer_range = longer_len if col_wrap else longer_len - 1
    return anchor_in_a, shared - 1, longer_len, range(shared - 1, longer_range)


def _bezier_patch_mesh(cp: np.ndarray, steps: int = 16) -> tuple[np.ndarray, np.ndarray]:
    """Tessellate a 4x4 grid of control points (`cp`, shape (4, 4, 3)) as a
    bicubic Bezier patch — the surface analog of `_PathViewport.
    _tessellate_bezier`'s per-segment cubic curve, using the same cubic
    Bernstein basis in both the row and column parametric directions:
    `S(u,v) = sum_i sum_j B_i(u) * B_j(v) * cp[i][j]`. Returns flat
    `(tris_pos, tris_norm)` arrays (`steps * steps * 2` triangles), ready
    for `SceneRenderer.upload_mesh`."""
    t_vals = np.linspace(0.0, 1.0, steps + 1, dtype=np.float64)
    omt = 1.0 - t_vals
    basis = np.stack([omt ** 3, 3 * t_vals * omt ** 2, 3 * t_vals ** 2 * omt, t_vals ** 3], axis=1)
    surface = np.einsum('ia,jb,abk->ijk', basis, basis, cp)

    tris_pos = []
    tris_norm = []
    for i in range(steps):
        for j in range(steps):
            p00, p01 = surface[i, j], surface[i, j + 1]
            p10, p11 = surface[i + 1, j], surface[i + 1, j + 1]
            for a, b, c in [(p00, p01, p11), (p00, p11, p10)]:
                n = np.cross(b - a, c - a)
                ln = np.linalg.norm(n)
                if ln > 0:
                    n = n / ln
                tris_pos.extend([a, b, c])
                tris_norm.extend([n, n, n])
    return np.array(tris_pos, dtype=np.float32), np.array(tris_norm, dtype=np.float32)


def _is_vnf(v) -> bool:
    if not (_is_list(v) and len(v) == 2):
        return False
    verts, faces = v[0], v[1]
    if not (_is_list(verts) and len(verts) >= 3
            and _is_list(faces) and len(faces) >= 1):
        return False
    if not all(_is_numeric_point(p) and len(p) == 3 for p in verts):
        return False
    return all(_is_list(f) and len(f) >= 3
               and all(isinstance(i, (int, float)) for i in f) for f in faces)


def _is_matrix(v) -> bool:
    """A matrix is a square list of lists of numbers, 2x2 through 5x5 —
    e.g. a transform matrix. No overlap with `_is_grid`: a grid row is a
    list of *points* (2/3-number lists), one nesting level deeper than a
    matrix row, which is a list of plain numbers."""
    if not (_is_list(v) and 2 <= len(v) <= 5):
        return False
    n = len(v)
    return all(_is_list(row) and len(row) == n
               and all(isinstance(x, (int, float)) for x in row) for row in v)


def _is_affine_matrix(v) -> bool:
    """A 3x3 (2D) or 4x4 (3D) homogeneous affine transform matrix: same
    square-numeric-list shape as `_is_matrix`, but additionally the
    bottom row must be the homogeneous identity row `[0, ..., 0, 1]`
    (within floating-point tolerance) — what actually makes a matrix
    "affine" rather than an arbitrary NxN array of numbers. Every affine
    matrix is also a `_is_matrix` match (3x3/4x4 are both in its 2-5
    range), so both viewers can apply to the same value."""
    if not (_is_list(v) and len(v) in (3, 4)):
        return False
    n = len(v)
    if not all(_is_list(row) and len(row) == n
               and all(isinstance(x, (int, float)) for x in row) for row in v):
        return False
    expected = [0.0] * (n - 1) + [1.0]
    return all(abs(float(a) - b) < 1e-9 for a, b in zip(v[-1], expected))


def _affine_reference_shape(n: int) -> list:
    """Corner points of the reference shape used to visualize an affine
    transform: a unit square centered at the origin for a 3x3 (2D)
    matrix (`n == 3`), or a unit cube for a 4x4 (3D) matrix (`n == 4`)."""
    if n == 3:
        return [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]
    return [
        [-0.5, -0.5, -0.5], [0.5, -0.5, -0.5], [0.5, 0.5, -0.5], [-0.5, 0.5, -0.5],
        [-0.5, -0.5, 0.5], [0.5, -0.5, 0.5], [0.5, 0.5, 0.5], [-0.5, 0.5, 0.5],
    ]


_AFFINE_SQUARE_EDGES = [(0, 1), (1, 2), (2, 3), (3, 0)]
_AFFINE_CUBE_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
]


def _affine_shape_edges(n: int) -> list:
    """Edge index pairs connecting `_affine_reference_shape(n)`'s corners."""
    return _AFFINE_SQUARE_EDGES if n == 3 else _AFFINE_CUBE_EDGES


def _apply_affine(matrix: list, points: list) -> list:
    """Apply an NxN homogeneous affine matrix to a list of (N-1)-dim
    points, returning the transformed (N-1)-dim points as plain lists."""
    m = np.array(matrix, dtype=np.float64)
    n = m.shape[0]
    result = []
    for p in points:
        homog = np.array(list(p) + [1.0], dtype=np.float64)
        transformed = m @ homog
        result.append(transformed[:n - 1].tolist())
    return result


_HEADER_STYLE = (
    "QHeaderView::section {"
    "  background-color: #e8e8e8;"
    "  border: 1px solid #c0c0c0;"
    "  padding: 2px 4px;"
    "}"
)


def _style_table_headers(table: QTableWidget):
    table.horizontalHeader().setStyleSheet(_HEADER_STYLE)
    table.verticalHeader().setStyleSheet(_HEADER_STYLE)


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


def _cube_faces(r: float, is_2d: bool) -> list:
    """Triangle triples (as vertex-offset vectors) forming a cube point
    marker of half-width `r` — a flat axis-aligned square in the XY plane
    when `is_2d`, a full cube otherwise. Shared by every viewport that
    draws corner/vertex markers (`_GridViewport`, `_PathViewport`,
    `_AffineViewport`, `_VNFViewport`)."""
    if is_2d:
        ppp = np.array([r, r, 0])
        pnp = np.array([r, -r, 0])
        nnp = np.array([-r, -r, 0])
        npp = np.array([-r, r, 0])
        return [(ppp, pnp, nnp), (ppp, nnp, npp)]

    ppp = np.array([r, r, r])
    ppn = np.array([r, r, -r])
    pnp = np.array([r, -r, r])
    pnn = np.array([r, -r, -r])
    npp = np.array([-r, r, r])
    npn = np.array([-r, r, -r])
    nnp = np.array([-r, -r, r])
    nnn = np.array([-r, -r, -r])
    return [
        (ppp, pnp, pnn), (ppp, pnn, ppn),   # +X
        (npp, npn, nnn), (npp, nnn, nnp),   # -X
        (ppp, ppn, npn), (ppp, npn, npp),   # +Y
        (pnp, nnp, nnn), (pnp, nnn, pnn),   # -Y
        (ppp, npp, nnp), (ppp, nnp, pnp),   # +Z
        (ppn, pnn, nnn), (ppn, nnn, npn),   # -Z
    ]


def _diamond_faces(r: float, is_2d: bool) -> list:
    """Triangle triples forming a diamond point marker of half-width `r`
    -- a rotated square (rhombus, points N/E/S/W) in the XY plane when
    `is_2d`, an octahedron (points along +-X/+-Y/+-Z) otherwise. Used for
    `_PathViewport`'s bezier "same_angle" node-type marker -- same
    offset-vector convention as `_cube_faces`, just a different silhouette
    so it's visually distinct at marker size."""
    px = np.array([r, 0, 0])
    nx = np.array([-r, 0, 0])
    py = np.array([0, r, 0])
    ny = np.array([0, -r, 0])
    if is_2d:
        return [(py, px, ny), (py, ny, nx)]

    pz = np.array([0, 0, r])
    nz = np.array([0, 0, -r])
    faces = []
    for a, b in [(px, py), (py, nx), (nx, ny), (ny, px)]:
        faces.append((pz, a, b))
        faces.append((nz, b, a))
    return faces


def _triangle_faces(r: float, is_2d: bool) -> list:
    """Triangle triples forming a triangle point marker of half-width `r`
    -- an equilateral triangle in the XY plane when `is_2d`, a tetrahedron
    otherwise. Used for `_PathViewport`'s bezier "symmetric" node-type
    marker -- same offset-vector convention as `_cube_faces`."""
    if is_2d:
        p0 = np.array([0.0, r, 0.0])
        p1 = np.array([-r * 0.866, -r * 0.5, 0.0])
        p2 = np.array([r * 0.866, -r * 0.5, 0.0])
        return [(p0, p1, p2)]

    a = np.array([r, r, r])
    b = np.array([r, -r, -r])
    c = np.array([-r, r, -r])
    d = np.array([-r, -r, r])
    return [(a, b, c), (a, c, d), (a, d, b), (b, d, c)]


def _marker_radius_for_point(vp: "Viewport", world_point) -> float:
    """Screen-space-constant marker radius (in world units) for a marker
    positioned AT `world_point`, so it stays ~6px on screen regardless of
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
    return world_per_px * 3


def _view_locked_axis(camera) -> int:
    """Return 0/1/2 (X/Y/Z) for whichever world axis is most nearly parallel
    to the camera's current view direction — the one axis a 2D mouse drag
    can't usefully control (foreshortened to ~a point), so 3D vertex-
    dragging locks it and moves the vertex only within the plane spanned by
    the other two (most-perpendicular-to-view) axes."""
    forward = camera.target - camera.eye_position()
    norm = np.linalg.norm(forward)
    if norm > 1e-9:
        forward = forward / norm
    return int(np.argmax(np.abs(forward)))


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


def _classify_node_type(v0: np.ndarray, handle_a: np.ndarray | None,
                         handle_b: np.ndarray | None, tol: float = 1e-4) -> str:
    """Bezier node type for a v0 given its two adjacent handles (either may
    be None at an open path's start/end, where there's nothing to link):
    "disjointed" if a handle is missing or the two aren't opposite-direction
    from v0 within `tol`; "symmetric" if they're also equidistant from v0
    within `tol`; else "same_angle" (opposite direction, different length).
    Used both for `_PathViewport`'s auto-detect-on-load classification and
    (indirectly, by construction) to predict a fresh De Casteljau split's
    new node's type."""
    if handle_a is None or handle_b is None:
        return "disjointed"
    da = handle_a - v0
    db = handle_b - v0
    len_a = float(np.linalg.norm(da))
    len_b = float(np.linalg.norm(db))
    if len_a < tol or len_b < tol:
        return "disjointed"
    # Opposite-direction check: da/len_a should be ~ -db/len_b.
    if np.linalg.norm(da / len_a + db / len_b) > tol:
        return "disjointed"
    if abs(len_a - len_b) <= tol:
        return "symmetric"
    return "same_angle"


def _v0_handle_indices(v0_idx: int, n: int, closed: bool) -> tuple[int | None, int | None]:
    """The (preceding, following) handle indices for a v0 -- `None` for
    either that doesn't exist (an open path's start has no preceding
    handle, its end has no following handle); closed paths always have
    both, wrapping via `% n`. Shared by classify_single_node and
    _snap_handles_to_node_type so the boundary rule lives in one place."""
    if closed:
        return (v0_idx - 1) % n, (v0_idx + 1) % n
    prev_idx = v0_idx - 1 if v0_idx - 1 >= 0 else None
    fwd_idx = v0_idx + 1 if v0_idx + 1 < n else None
    return prev_idx, fwd_idx


def _snap_handles_to_node_type(v0: np.ndarray, handle_a: np.ndarray | None,
                                handle_b: np.ndarray | None, node_type: str,
                                tol: float = 1e-9) -> tuple[np.ndarray, np.ndarray] | None:
    """Bring both handles into line with `node_type`, immediately, rather
    than waiting for the next drag: "same_angle" averages their *direction*
    through v0 only (each handle keeps its own existing distance from v0);
    "symmetric" also averages their *distance* (both end up equidistant).
    Returns the new (handle_a, handle_b) positions, or None if nothing
    should change: node_type == "disjointed", a handle is missing (open-
    path boundary), a handle is coincident with v0 (direction undefined),
    or the two handles already point in the exact same direction from v0
    (an ill-defined/degenerate "average" -- e.g. both handles on the same
    side of v0 already, not opposing at all)."""
    if node_type == "disjointed" or handle_a is None or handle_b is None:
        return None
    da, db = handle_a - v0, handle_b - v0
    len_a, len_b = float(np.linalg.norm(da)), float(np.linalg.norm(db))
    if len_a < tol or len_b < tol:
        return None
    # Target shared axis: average of da's own direction and db's *opposing*
    # direction -- da/len_a - db/len_b equals 2*(da/len_a) exactly when the
    # two are already perfectly opposite (idempotent on an already-good pair).
    u = da / len_a - db / len_b
    unorm = float(np.linalg.norm(u))
    if unorm < tol:
        return None
    u /= unorm
    if node_type == "symmetric":
        avg_len = (len_a + len_b) / 2.0
        return v0 + u * avg_len, v0 - u * avg_len
    return v0 + u * len_a, v0 - u * len_b  # same_angle


def _remap_node_types(node_types: dict[int, str], index_map: dict[int, int]) -> dict[int, str]:
    """Rebuild a bezier `_node_types` dict after the underlying point list's
    indices shifted (an insert or delete) -- `index_map` gives old index ->
    new index for every point that still exists; entries whose old index
    isn't in `index_map` (deleted, or no longer a valid v0) are dropped."""
    return {index_map[old]: t for old, t in node_types.items() if old in index_map}


def _owning_v0_index(idx: int, n: int, closed: bool) -> int:
    """The v0 a handle (v1 or v2) is attached to, for node-type lookup and
    linking purposes -- v0 itself if idx is already a v0. v1 (idx % 3 == 1)
    is THIS v0's own forward handle, so it belongs to the v0 right before
    it (idx - 1). v2 (idx % 3 == 2) is the *next* v0's own backward
    handle, so it belongs to the v0 right after it (idx + 1), not idx - 2
    -- getting this backwards was a real bug: dragging the handle *before*
    an on-curve point always looked disjointed regardless of its actual
    node type, since it read the wrong (preceding) v0's type instead of
    the one it's actually attached to."""
    kind = idx % 3
    if kind == 0:
        return idx
    v0_idx = idx - 1 if kind == 1 else idx + 1
    return v0_idx % n if closed else v0_idx


def _bezier_linked_moves(path_pts: np.ndarray, closed: bool, dragged_idx: int,
                          new_pos: np.ndarray, node_type: str) -> list[tuple[int, np.ndarray]]:
    """Given a bezier point being dragged/nudged to `new_pos`, return
    [(dragged_idx, new_pos), ...] plus any linked partner point(s) that
    must also move -- see the plan's "Behavior" section:
    - dragged_idx is a v0 (idx % 3 == 0): ALWAYS rigid-translate both
      adjacent handles (if they exist -- open-path endpoints may have
      only one) by the same delta as v0's own move. node_type is unused.
    - dragged_idx is a v1/v2: node_type comes from the *owning* v0 (looked
      up by the caller). "disjointed", or no partner index exists (open-
      path boundary): only the dragged point moves. "symmetric": partner
      mirrors the dragged handle's new distance from v0. "same_angle":
      partner mirrors direction only, keeping its own prior distance."""
    n = len(path_pts)
    kind = dragged_idx % 3

    if kind == 0:
        delta = new_pos - path_pts[dragged_idx]
        moves = [(dragged_idx, new_pos)]
        if closed:
            prev_idx, fwd_idx = (dragged_idx - 1) % n, (dragged_idx + 1) % n
        else:
            prev_idx = dragged_idx - 1 if dragged_idx - 1 >= 0 else None
            fwd_idx = dragged_idx + 1 if dragged_idx + 1 < n else None
        if prev_idx is not None:
            moves.append((prev_idx, path_pts[prev_idx] + delta))
        if fwd_idx is not None:
            moves.append((fwd_idx, path_pts[fwd_idx] + delta))
        return moves

    v0_idx = _owning_v0_index(dragged_idx, n, closed)
    partner_idx = dragged_idx - 2 if kind == 1 else dragged_idx + 2
    if closed:
        partner_idx %= n
    elif partner_idx < 0 or partner_idx >= n:
        return [(dragged_idx, new_pos)]

    if node_type == "disjointed":
        return [(dragged_idx, new_pos)]

    v0 = path_pts[v0_idx]
    if node_type == "symmetric":
        partner_new = v0 + (v0 - new_pos)
    else:  # same_angle
        partner_old = path_pts[partner_idx]
        dist = float(np.linalg.norm(partner_old - v0))
        direction = v0 - new_pos
        dnorm = float(np.linalg.norm(direction))
        if dnorm < 1e-9:
            return [(dragged_idx, new_pos)]
        partner_new = v0 + dist * direction / dnorm
    return [(dragged_idx, new_pos), (partner_idx, partner_new)]


def _decasteljau_split(p0: np.ndarray, c1: np.ndarray, c2: np.ndarray, p3: np.ndarray,
                        t: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Standard cubic De Casteljau split of one bezier segment (p0, c1, c2,
    p3) at parameter t -- returns the 5 points (a, d, f, e, c) that replace
    the segment's 2 interior control points (c1, c2) when splicing a new
    on-curve vertex into the path: `path[i0+1:i0+3] = [a, d, f, e, c]`
    turns the original 4-point segment (p0, c1, c2, p3) into two new
    curve-preserving cubic segments (p0, a, d, f) and (f, e, c, p3) -- f is
    the new on-curve vertex, sitting exactly on the original curve."""
    a = p0 + (c1 - p0) * t
    b = c1 + (c2 - c1) * t
    c = c2 + (p3 - c2) * t
    d = a + (b - a) * t
    e = b + (c - b) * t
    f = d + (e - d) * t
    return a, d, f, e, c


def _bernstein_cubic(p0: np.ndarray, c1: np.ndarray, c2: np.ndarray, p3: np.ndarray,
                      t: np.ndarray) -> np.ndarray:
    """Standard cubic bezier blend, evaluated at every parameter in the
    array `t` at once -- returns shape (len(t), 3)."""
    omt = 1 - t
    return (np.outer(omt**3, p0) + np.outer(3 * omt**2 * t, c1)
            + np.outer(3 * omt * t**2, c2) + np.outer(t**3, p3))


def _fit_merged_segment(p0: np.ndarray, c1: np.ndarray, c2: np.ndarray, v0: np.ndarray,
                         c3: np.ndarray, c4: np.ndarray, p3: np.ndarray,
                         samples: int = 16) -> tuple[np.ndarray, np.ndarray]:
    """Deleting an on-curve vertex (v0) merges its two adjacent cubic
    segments (p0, c1, c2, v0) and (v0, c3, c4, p3) into one new segment
    (p0, NEW_C1, NEW_C2, p3) -- a single cubic can't generally reproduce
    two arbitrary cubics exactly, so this does its best via ordinary
    linear least-squares (closed-form, no iteration): `samples` points are
    evenly sampled across both original segments (first half from the
    p0-side segment, second half from the p3-side one) and NEW_C1/NEW_C2
    are solved to minimize the sum of squared distances from the new
    single segment to those samples, with p0/p3 held fixed. Falls back to
    keeping the two *outer* handles (c1, c4) unchanged -- the simplest
    reasonable approximation -- only in the degenerate case where the
    least-squares system is singular (e.g. samples all coincide)."""
    ts = np.linspace(0.0, 1.0, samples)
    first_half = ts <= 0.5
    q = np.empty((samples, 3))
    q[first_half] = _bernstein_cubic(p0, c1, c2, v0, ts[first_half] * 2)
    q[~first_half] = _bernstein_cubic(v0, c3, c4, p3, (ts[~first_half] - 0.5) * 2)

    omt = 1 - ts
    b1 = 3 * omt**2 * ts
    b2 = 3 * omt * ts**2
    r = q - np.outer(omt**3, p0) - np.outer(ts**3, p3)

    s11, s12, s22 = float(b1 @ b1), float(b1 @ b2), float(b2 @ b2)
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-12:
        return c1, c4
    t1, t2 = b1 @ r, b2 @ r
    new_c1 = (t1 * s22 - t2 * s12) / det
    new_c2 = (t2 * s11 - t1 * s12) / det
    return new_c1, new_c2


def _unlocked_plane_name(lock_axis: int) -> str:
    """Name of the drag plane spanned by the two axes *other* than
    `lock_axis` (e.g. lock_axis=2 (Z) -> "XY") — shown as a `_show_delta`
    overlay while a vertex drag is in progress, so it's clear which plane
    a 3D drag is currently constrained to."""
    return "".join("XYZ"[i] for i in range(3) if i != lock_axis)


def _key_nudge_magnitude(modifiers) -> float:
    """Arrow-key nudge step size for the held modifiers: Cmd (`Control` on
    macOS) for a fine 0.1-unit nudge, Shift for a coarse 10-unit nudge,
    neither for the default 1-unit nudge. Shared by every editable
    viewport's `keyPressEvent`."""
    if modifiers & Qt.KeyboardModifier.ControlModifier:  # Cmd on macOS
        return 0.1
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return 10.0
    return 1.0


def _key_nudge_delta(camera, lock_axis: int, key, magnitude: float = 1.0) -> np.ndarray | None:
    """World-space delta (`magnitude` world units, from `_key_nudge_magnitude`)
    for arrow-key vertex nudging, confined to the same axis-locked plane
    Cmd+drag uses (`_view_locked_axis`/`_ray_plane_axis_locked`) rather
    than a fixed pair of world axes -- important once the locked plane
    isn't the always-top-down XY plane a 2D viewport uses, e.g. a 3D
    grid/path orbited to look along +/-X or +/-Y instead of +/-Z. Unlike a
    drag (which can move freely to anywhere in that plane), each key press
    changes exactly one coordinate: of the plane's two free axes,
    "Right"/"Left" moves along whichever one the camera's actual
    screen-right direction (`view_matrix()` row 0 -- the same row the
    gimbal-lock regression tests read) has the larger component in
    (sign-matched), and "Up"/"Down" does the same using screen-up (row 1)
    against the *other* free axis -- so a press always reads as a clean
    single-axis nudge in the table, oriented to match the screen
    regardless of camera orientation. Returns None for any other key."""
    free_axes = [a for a in range(3) if a != lock_axis]
    view = camera.view_matrix()
    cam_right = view[0, :3]
    cam_up = view[1, :3]

    def _snap(v, axis):
        delta = np.zeros(3)
        delta[axis] = magnitude if v[axis] >= 0 else -magnitude
        return delta

    right_axis = max(free_axes, key=lambda a: abs(cam_right[a]))
    up_axis = free_axes[0] if right_axis == free_axes[1] else free_axes[1]
    right_delta = _snap(cam_right, right_axis)
    up_delta = _snap(cam_up, up_axis)
    return {
        Qt.Key.Key_Right: right_delta,
        Qt.Key.Key_Left: -right_delta,
        Qt.Key.Key_Up: up_delta,
        Qt.Key.Key_Down: -up_delta,
    }.get(key)


# ---------------------------------------------------------------------------
# List / Object Viewer
# ---------------------------------------------------------------------------

class ListViewer(QDialog):
    """Scrollable table displaying a list (with indices) or OscObject (with keys)."""

    def __init__(self, title: str, value, parent=None):
        super().__init__(parent)
        self._title = title
        self.setWindowTitle(f"List Viewer: {title}" if title else "List Viewer")
        self.resize(500, 400)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._value = value

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setColumnCount(2)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._entries: list[tuple[str, object]] = []
        self._populate(value)

    def _populate(self, value):
        if _is_oscobject(value):
            self._table.setHorizontalHeaderLabels(["Key", "Value"])
            for k, v in value.items():
                self._entries.append((str(k), v))
        elif _is_list(value):
            self._table.setHorizontalHeaderLabels(["Index", "Value"])
            for i, v in enumerate(value):
                self._entries.append((str(i), v))
        else:
            return

        self._table.setRowCount(len(self._entries))
        for row, (key, val) in enumerate(self._entries):
            key_item = QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, key_item)
            val_item = QTableWidgetItem(_fmt_short(val))
            val_item.setFlags(val_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, val_item)

    def _context_menu(self, pos):
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        if row < 0 or row >= len(self._entries):
            return
        key, val = self._entries[row]

        menu = QMenu(self)
        sub_title = f"{self._title}[{key}]"
        if _is_list(val) or _is_oscobject(val):
            menu.addAction("View as List...", lambda: _open_list_viewer(
                sub_title, val, self))
        if _is_vnf(val):
            menu.addAction("View as VNF...", lambda: _open_vnf_viewer(
                sub_title, val, self))
        if _is_path(val):
            menu.addAction("View as Path...", lambda: _open_path_viewer(
                sub_title, val, self))
        if _is_matrix(val):
            menu.addAction("View as Matrix...", lambda: _open_matrix_viewer(
                sub_title, val, self))
        if _is_affine_matrix(val):
            menu.addAction("View as Affine Transform...", lambda: _open_affine_matrix_viewer(
                sub_title, val, self))
        if menu.isEmpty():
            return
        menu.exec(self._table.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------------------
# Profile Viewer
# ---------------------------------------------------------------------------

class _NumericTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by a numeric value instead of its
    displayed text -- setSortingEnabled's default comparison is
    lexical/string-based, which would sort "100.0" before "20.0". No
    sortable-column precedent exists elsewhere in this file; this is the
    minimal fix (one method), not a new dependency."""

    def __init__(self, value: float, text: str):
        super().__init__(text)
        self._value = value
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, _NumericTableWidgetItem):
            return self._value < other._value
        return super().__lt__(other)


class ProfileViewer(QDialog):
    """Sortable per-call-site profiling report from a "Render with
    Profiling" run (see Evaluator's profile=True instrumentation).

    Self time is the primary, always-correct metric (a frame's own code,
    excluding children -- disjoint wall-clock slices, never overlapping).
    Cumulative time is secondary and recursion-guarded: a call site that
    recurses through itself only counts its outermost invocation's
    elapsed time, since that already includes every nested invocation's
    time -- see CallSiteProfile's docstring. Double-click or right-click
    a row to jump to its call site or declaration in the editor."""

    navigate_requested = Signal(str, int)  # (file_path, line)

    _SELF_MS_COL = 6
    _SEARCH_COLS = (0, 1, 3)  # Name, Caller, Caller File

    def __init__(self, result: "ProfileResult", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profile Report")
        self.resize(900, 500)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._result = result

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        resolve_ms = result.resolve_time * 1000
        unattributed_pct = 100 * result.unattributed_time / result.resolve_time if result.resolve_time > 0 else 0.0
        summary = QLabel(
            f"Total: {result.total_time * 1000:.1f} ms   "
            f"Resolve: {resolve_ms:.1f} ms   "
            f"Generate: {result.generate_time * 1000:.1f} ms   "
            f"Unattributed: {result.unattributed_time * 1000:.1f} ms ({unattributed_pct:.1f}% of resolve)"
        )
        layout.addWidget(summary)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search Name / Caller / Caller File…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_search_filter)
        layout.addWidget(self._search)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        cols = ["Name", "Caller", "Kind", "Caller File", "Line", "Calls",
                "Self (ms)", "Self %", "Total (ms)", "Total %"]
        self._table.setColumnCount(len(cols))
        self._table.setHorizontalHeaderLabels(cols)
        self._table.verticalHeader().setVisible(False)
        _style_table_headers(self._table)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.cellDoubleClicked.connect(self._goto_call_site)
        header = self._table.horizontalHeader()
        # Interactive (the default), not Stretch -- Stretch fills available
        # space but also disables user drag-resizing for that section
        # entirely, which is the opposite of what these two variable-length
        # text columns need. Just give them a wider starting width.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 160)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 140)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(3, 220)
        layout.addWidget(self._table)

        self._populate(result)
        self._table.setSortingEnabled(True)
        self._table.sortItems(self._SELF_MS_COL, Qt.SortOrder.DescendingOrder)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

    def _populate(self, result: "ProfileResult"):
        resolve_time = result.resolve_time
        self._table.setRowCount(len(result.call_sites))
        for row, site in enumerate(result.call_sites):
            self_ms = site.self_time * 1000
            cum_ms = site.cumulative_time * 1000
            self_pct = 100 * site.self_time / resolve_time if resolve_time > 0 else 0.0
            cum_pct = 100 * site.cumulative_time / resolve_time if resolve_time > 0 else 0.0
            values = [
                QTableWidgetItem(site.name),
                QTableWidgetItem(site.caller_name),
                QTableWidgetItem(site.kind),
                QTableWidgetItem(site.call_origin),
                _NumericTableWidgetItem(site.call_line, str(site.call_line)),
                _NumericTableWidgetItem(site.call_count, str(site.call_count)),
                _NumericTableWidgetItem(self_ms, f"{self_ms:.2f}"),
                _NumericTableWidgetItem(self_pct, f"{self_pct:.1f}"),
                _NumericTableWidgetItem(cum_ms, f"{cum_ms:.2f}"),
                _NumericTableWidgetItem(cum_pct, f"{cum_pct:.1f}"),
            ]
            for col, item in enumerate(values):
                if col < 4:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, col, item)
            # setSortingEnabled(True) reorders the table's *visual* rows
            # without touching self._entries, so a post-sort row index no
            # longer matches self._entries[row] -- stash the site directly
            # on the row's own item instead, so lookups stay correct no
            # matter how the user has sorted the table (a real bug: this
            # is the codebase's first sortable table, and the "index a
            # parallel list by row" pattern every other viewer here uses
            # only happens to work because none of their tables sort).
            self._table.item(row, 0).setData(Qt.ItemDataRole.UserRole, site)

    def _site_at_row(self, row: int) -> "CallSiteProfile | None":
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _apply_search_filter(self, _=None):
        text = self._search.text().strip().lower()
        for row in range(self._table.rowCount()):
            match = not text or any(
                (item := self._table.item(row, col)) is not None and text in item.text().lower()
                for col in self._SEARCH_COLS
            )
            self._table.setRowHidden(row, not match)

    def _goto_call_site(self, row: int, _col: int):
        site = self._site_at_row(row)
        if site is not None:
            self.navigate_requested.emit(site.call_origin, site.call_line)

    def _context_menu(self, pos):
        item = self._table.itemAt(pos)
        if item is None:
            return
        site = self._site_at_row(item.row())
        if site is None:
            return

        menu = QMenu(self)
        menu.addAction("Go to Call Site", lambda: self.navigate_requested.emit(site.call_origin, site.call_line))
        menu.addAction("Go to Declaration", lambda: self.navigate_requested.emit(site.decl_origin, site.decl_line))
        menu.addAction("Filter by Name", lambda: self._search.setText(site.name))
        menu.addAction("Filter by Caller", lambda: self._search.setText(site.caller_name))
        menu.exec(self._table.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------------------
# Matrix Viewer
# ---------------------------------------------------------------------------

class MatrixViewer(QDialog):
    """Displays a square 2x2-5x5 list of lists of numbers as a grid of
    cells, row/column headers 0-indexed to match OpenSCAD list indexing.
    Read-only by default. Pass `editable=True` for a Save/Cancel editing
    mode: cell edits only update this dialog's own table (no writeback),
    and `committed` fires once, with the whole re-serialized value, when
    Save is clicked."""

    committed = Signal(str)

    def __init__(self, title: str, value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        self._title = title
        self._editable = editable
        label = "Matrix Editor" if editable else "Matrix Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._value = value

        n = len(value)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setRowCount(n)
        self._table.setColumnCount(n)
        self._table.setHorizontalHeaderLabels([str(c) for c in range(n)])
        self._table.setVerticalHeaderLabels([str(r) for r in range(n)])
        _style_table_headers(self._table)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        if editable:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                         | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, row in enumerate(value):
            for c, val in enumerate(row):
                item = QTableWidgetItem(_fmt_short(val))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        layout.addWidget(self._table)
        if editable:
            self._table.itemChanged.connect(self._on_item_changed)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        col_w = sum(self._table.columnWidth(c) for c in range(n))
        row_h = sum(self._table.rowHeight(r) for r in range(n))
        width = col_w + self._table.verticalHeader().width() + 40
        height = row_h + self._table.horizontalHeader().height() + 80
        self.resize(max(220, width), max(180, height))

    def _on_item_changed(self, item: QTableWidgetItem):
        parsed = _parse_number(item.text())
        if parsed is None:
            self._table.blockSignals(True)
            item.setText(_fmt_short(self._value[item.row()][item.column()]))
            self._table.blockSignals(False)
            return
        self._value[item.row()][item.column()] = parsed
        self._table.blockSignals(True)
        item.setText(_fmt_short(parsed))
        self._table.blockSignals(False)

    def _on_save(self):
        self.committed.emit(_format_value(self._value))
        self.accept()


# ---------------------------------------------------------------------------
# Affine Matrix Viewer
# ---------------------------------------------------------------------------

class _AffineViewport(Viewport):
    """Viewport showing a reference unit square/cube (gray wireframe)
    alongside its image under an affine transform matrix (orange
    wireframe + corner markers), for `AffineMatrixViewer`. The first
    corner of the transformed shape is marked red rather than orange, so
    reflections/orientation flips are visible even though the untransformed
    shape has no other distinguishing features."""

    def __init__(self, matrix: list, parent=None):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        cam = self._renderer.camera
        cam.fov = 45.0
        self._renderer.line_width = 2.0
        self._is_2d = len(matrix) == 3
        self._corners: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        if self._is_2d:
            cam.azimuth = 270.0
            # Not exactly 90: gimbal lock at precisely elevation=+-90 makes
            # _look_at fall back to an arbitrary +X "right" vector that
            # doesn't match the azimuth-dependent basis orbit-dragging
            # continuously converges to just off the pole -- see the
            # matching fix/comment in viewport.py's "top" view preset.
            cam.elevation = 89.9999
            cam.orthographic = True
        else:
            cam.orthographic = False
        self.schedule_load(lambda: self.load_matrix(matrix))

    @staticmethod
    def _to_3d(points: list) -> np.ndarray:
        return np.array(
            [[p[0], p[1], p[2] if len(p) > 2 else 0.0] for p in points],
            dtype=np.float32,
        )

    def load_matrix(self, matrix: list):
        self.makeCurrent()
        self._renderer._clear_buffers()
        self._renderer.clear_simple_buffers()

        n = len(matrix)
        ref = _affine_reference_shape(n)
        transformed = _apply_affine(matrix, ref)
        edges = _affine_shape_edges(n)

        ref_pts = self._to_3d(ref)
        xf_pts = self._to_3d(transformed)
        self._corners = xf_pts

        bb_min = np.minimum(ref_pts.min(axis=0), xf_pts.min(axis=0))
        bb_max = np.maximum(ref_pts.max(axis=0), xf_pts.max(axis=0))
        self.frame_scene(bb_min, bb_max)

        ref_color = np.array([0.55, 0.55, 0.55], dtype=np.float32)
        xf_color = np.array([0.9, 0.45, 0.1], dtype=np.float32)
        line_verts = []
        for a, b in edges:
            line_verts.append(np.concatenate([ref_pts[a], ref_color]))
            line_verts.append(np.concatenate([ref_pts[b], ref_color]))
        for a, b in edges:
            line_verts.append(np.concatenate([xf_pts[a], xf_color]))
            line_verts.append(np.concatenate([xf_pts[b], xf_color]))
        self._renderer.upload_lines(np.array(line_verts, dtype=np.float32))

        self._build_point_markers()
        self.doneCurrent()
        self.update()

    def _build_point_markers(self):
        self._renderer.clear_points()
        if len(self._corners) == 0 or self._ctx is None:
            return
        unit_faces = _cube_faces(1.0, self._is_2d)
        first_color = np.array([0.85, 0.15, 0.15], dtype=np.float32)
        rest_color = np.array([0.9, 0.45, 0.1], dtype=np.float32)
        marker_tris = []
        for i, pt in enumerate(self._corners):
            color = first_color if i == 0 else rest_color
            r = _marker_radius_for_point(self, pt)
            for v0, v1, v2 in unit_faces:
                marker_tris.append(np.concatenate([pt + v0 * r, color]))
                marker_tris.append(np.concatenate([pt + v1 * r, color]))
                marker_tris.append(np.concatenate([pt + v2 * r, color]))
        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))


class AffineMatrixViewer(QDialog):
    """Visualizes a 3x3 (2D) or 4x4 (3D) homogeneous affine transform
    matrix by showing a reference unit square/cube next to its image
    under the matrix, alongside the matrix's numbers. Read-only by default;
    pass `editable=True` for a Save/Cancel editing mode (see `MatrixViewer`
    for the shared editing convention)."""

    committed = Signal(str)

    def __init__(self, title: str, value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        self._title = title
        self._editable = editable
        label = "Affine Transform Editor" if editable else "Affine Transform Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._value = value
        n = len(value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _AffineViewport(value, self)
        splitter.addWidget(self._vp)

        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.addWidget(QLabel(f"{n}x{n} Matrix"))
        self._table = QTableWidget()
        self._table.setFont(QFont("Menlo", 11))
        self._table.setRowCount(n)
        self._table.setColumnCount(n)
        self._table.setHorizontalHeaderLabels([str(c) for c in range(n)])
        self._table.setVerticalHeaderLabels([str(r) for r in range(n)])
        _style_table_headers(self._table)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        if editable:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                         | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, row in enumerate(value):
            for c, val in enumerate(row):
                item = QTableWidgetItem(_fmt_short(val))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        tc_layout.addWidget(self._table)
        splitter.addWidget(table_container)
        if editable:
            self._table.itemChanged.connect(self._on_item_changed)

        t = self._table
        table_w = (t.verticalHeader().width()
                   + sum(t.columnWidth(j) for j in range(n))
                   + t.frameWidth() * 2 + 20)
        splitter.setSizes([600, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self.resize(600 + table_w, 480)

    def _on_item_changed(self, item: QTableWidgetItem):
        parsed = _parse_number(item.text())
        if parsed is None:
            self._table.blockSignals(True)
            item.setText(_fmt_short(self._value[item.row()][item.column()]))
            self._table.blockSignals(False)
            return
        self._value[item.row()][item.column()] = parsed
        self._table.blockSignals(True)
        item.setText(_fmt_short(parsed))
        self._table.blockSignals(False)
        if self._vp._ctx is not None:
            self._vp.load_matrix(self._value)

    def _on_save(self):
        self.committed.emit(_format_value(self._value))
        self.accept()


# ---------------------------------------------------------------------------
# VNF Viewport (adds face picking and highlight)
# ---------------------------------------------------------------------------

class _VNFViewport(Viewport):
    face_clicked = Signal(int)
    vertex_moved = Signal(int, float, float, float)  # (index, new_x, new_y, new_z) -- Cmd+drag, editable only

    def __init__(self, parent=None, editable: bool = False):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        self._renderer.camera.fov = 45.0
        self._renderer.depth_test_points = True
        self._editable = editable
        self._cpu_positions: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._tri_to_face: np.ndarray = np.zeros(0, dtype=np.int32)
        self._highlight_vao = None
        self._highlight_vbo = None
        self._vert_marker_vao_r = None
        self._vert_marker_vbo_r = None
        self._vert_marker_vao_w = None
        self._vert_marker_vbo_w = None
        self._vert_blink_red = True
        self._vert_indices: list[int] = []
        self._vert_blink_timer = QTimer(self)
        self._vert_blink_timer.setInterval(250)
        self._vert_blink_timer.timeout.connect(self._blink_tick)
        self._verts_3d: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._selected_face: int = -1
        self._drag_started = False
        self._press_pos: QPoint | None = None
        self._drag_vertex_idx = -1
        self._drag_lock_axis = 2
        self._drag_plane_point: np.ndarray = np.zeros(3, dtype=np.float32)

    def set_face_data(self, cpu_positions: np.ndarray, tri_to_face: np.ndarray,
                      verts_3d: np.ndarray | None = None):
        self._cpu_positions = cpu_positions
        self._tri_to_face = tri_to_face
        if verts_3d is not None:
            self._verts_3d = verts_3d

    def highlight_face(self, face_idx: int):
        if face_idx == self._selected_face:
            return
        self.makeCurrent()
        self._selected_face = face_idx
        self._rebuild_highlight()
        if face_idx >= 0 and len(self._cpu_positions) > 0:
            mask = self._tri_to_face == face_idx
            tri_indices = np.where(mask)[0]
            if len(tri_indices) > 0:
                verts = np.array([
                    self._cpu_positions[ti * 3 + k]
                    for ti in tri_indices for k in range(3)
                ], dtype=np.float32)
                self.scroll_to_visible(verts.mean(axis=0))
        self.doneCurrent()
        self.update()

    def _rebuild_highlight(self):
        if self._highlight_vao is not None:
            self._highlight_vao.release()
            self._highlight_vbo.release()
            self._highlight_vao = None
            self._highlight_vbo = None

        if self._selected_face < 0 or self._ctx is None:
            return

        mask = self._tri_to_face == self._selected_face
        tri_indices = np.where(mask)[0]
        if len(tri_indices) == 0:
            return

        positions = []
        normals = []
        for ti in tri_indices:
            v0 = self._cpu_positions[ti * 3]
            v1 = self._cpu_positions[ti * 3 + 1]
            v2 = self._cpu_positions[ti * 3 + 2]
            n = np.cross(v1 - v0, v2 - v0)
            ln = np.linalg.norm(n)
            if ln > 0:
                n /= ln
            positions.extend([v0, v1, v2])
            normals.extend([n, n, n])

        pos_arr = np.array(positions, dtype=np.float32)
        norm_arr = np.array(normals, dtype=np.float32)
        interleaved = np.concatenate([pos_arr, norm_arr], axis=1)
        self._highlight_vbo = self._ctx.buffer(interleaved.tobytes())
        self._highlight_vao = self._ctx.vertex_array(
            self._renderer._mesh_prog, [(self._highlight_vbo, "3f 3f", "in_position", "in_normal")],
        )

    def _blink_tick(self):
        self._vert_blink_red = not self._vert_blink_red
        self.update()

    def highlight_vertices(self, indices: list[int]):
        self.makeCurrent()
        self._release_vert_markers()
        self._vert_indices = []

        if not indices or self._ctx is None or len(self._verts_3d) == 0:
            self._vert_blink_timer.stop()
            self.doneCurrent()
            self.update()
            return

        valid_indices = [vi for vi in indices if 0 <= vi < len(self._verts_3d)]
        self._vert_indices = valid_indices
        if valid_indices:
            self.scroll_to_visible(self._verts_3d[valid_indices[0]])
        self._vert_blink_red = True
        self._vert_blink_timer.start()
        self._build_vert_markers()
        self.doneCurrent()
        self.update()

    def _release_vert_markers(self):
        for attr in ("_vert_marker_vao_r", "_vert_marker_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_vert_marker_vbo_r", "_vert_marker_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_vert_markers(self):
        self._release_vert_markers()
        if not self._vert_indices or self._ctx is None:
            return

        unit_faces = _cube_faces(1.0, False)

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_vert_marker_vao_r", "_vert_marker_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_vert_marker_vao_w", "_vert_marker_vbo_w"),
        ]:
            tris = []
            for vi in self._vert_indices:
                pt = self._verts_3d[vi]
                r = _marker_radius_for_point(self, pt)
                for v0, v1, v2 in unit_faces:
                    tris.append(np.concatenate([pt + v0 * r, color_val]))
                    tris.append(np.concatenate([pt + v1 * r, color_val]))
                    tris.append(np.concatenate([pt + v2 * r, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._gizmo_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        # Always called from within an already-makeCurrent'd caller
        # (load_path/load_grid/load_matrix's own bracket, or the safe
        # initial schedule_load path) -- must NOT bracket with its own
        # makeCurrent/doneCurrent, since doneCurrent() would prematurely
        # release the context out from under that caller's remaining work.
        super().frame_scene(bb_min, bb_max, reframe=reframe)
        if self._vert_indices:
            self._build_vert_markers()

    def wheelEvent(self, event):
        # Unlike frame_scene above, this is a genuine external Qt event --
        # never called from inside another makeCurrent'd block -- so it
        # does need its own bracket.
        super().wheelEvent(event)
        if self._vert_indices:
            self.makeCurrent()
            self._build_vert_markers()
            self.doneCurrent()

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        # Vertex markers (swap red/white)
        vao = self._vert_marker_vao_r if self._vert_blink_red else self._vert_marker_vao_w
        if vao is not None:
            self._renderer._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            # Depth-tested (not disabled): markers must stay occluded by mesh
            # faces farther in front. Small polygon offset toward the camera
            # just breaks ties against coincident wireframe edges at the same
            # vertex position -- see SceneRenderer._render_simple_points.
            self._ctx.polygon_offset = (-1.0, -1.0)
            self._ctx.enable_direct(0x8037)  # GL_POLYGON_OFFSET_FILL
            vao.render(mgl.TRIANGLES)
            self._ctx.disable_direct(0x8037)
            self._ctx.polygon_offset = (0.0, 0.0)

        if self._highlight_vao is None:
            return
        import moderngl as mgl
        self._ctx.polygon_offset = (-1.0, -1.0)
        self._ctx.enable_direct(0x8037)
        cam = self._renderer.camera
        view = cam.view_matrix()
        light = np.array([0.6, 0.8, 1.0], dtype=np.float32)
        light /= np.linalg.norm(light)
        L_world = (view[:3, :3].T @ light).astype(np.float32)
        L_world /= np.linalg.norm(L_world)
        mesh_prog = self._renderer._mesh_prog
        mesh_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
        mesh_prog["light_dir"].value = tuple(L_world)
        mesh_prog["eye_pos"].value = tuple(cam.eye_position())
        mesh_prog["object_color"].value = (0.2, 0.9, 0.3, 1.0)
        mesh_prog["backface_color"].value = (0.8, 0.0, 0.8, 1.0)
        self._highlight_vao.render()
        self._ctx.disable_direct(0x8037)

    def _pick_vertex(self, px: float, py: float) -> int:
        """Nearest vertex to the cursor among *all* mesh vertices (unlike
        the tooltip logic below, which is restricted to the currently
        highlighted set) -- Cmd+drag can grab any vertex regardless of
        table/face selection, same as `_PathViewport`/`_GridViewport`."""
        if len(self._verts_3d) == 0:
            return -1
        return self._renderer.pick_nearest_point(self._verts_3d, px, py, self.width(), self.height())

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        if (self._editable
                and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier   # Cmd on macOS
                and not (event.modifiers() & Qt.KeyboardModifier.AltModifier)):
            vi = self._pick_vertex(self._press_pos.x(), self._press_pos.y())
            if vi >= 0:
                self._drag_vertex_idx = vi
                self._drag_lock_axis = _view_locked_axis(self._renderer.camera)
                self._drag_plane_point = self._verts_3d[vi].copy()
                self._show_delta(f"Plane: {_unlocked_plane_name(self._drag_lock_axis)}")
                return   # don't arm orbit/pan -- this press starts a vertex drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._drag_vertex_idx >= 0:
            pos = event.position().toPoint()
            ray_o, ray_d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
            hit = _ray_plane_axis_locked(ray_o, ray_d, self._drag_plane_point, self._drag_lock_axis)
            if hit is not None:
                self.vertex_moved.emit(self._drag_vertex_idx,
                                        round(float(hit[0]), 3), round(float(hit[1]), 3), round(float(hit[2]), 3))
            return
        if self._last_mouse is None and self._vert_indices:
            pos = event.position().toPoint()
            candidates = self._verts_3d[self._vert_indices]
            local_idx = self._renderer.pick_nearest_point(
                candidates, pos.x(), pos.y(), self.width(), self.height())
            if local_idx >= 0:
                best_idx = self._vert_indices[local_idx]
                pt = self._verts_3d[best_idx]
                self.setToolTip(f"[{best_idx}]: ({pt[0]:g}, {pt[1]:g}, {pt[2]:g})")
            else:
                self.setToolTip("")
        elif not self._vert_indices:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_vertex_idx >= 0:
            self._drag_vertex_idx = -1
            self._press_pos = None
            self._drag_started = False
            self._delta_label.hide()
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            face = self._pick_face(pos.x(), pos.y())
            self.highlight_face(face)
            self.face_clicked.emit(face)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        """Arrow keys nudge every currently-highlighted vertex, confined to
        the same axis-locked plane Cmd+drag uses -- see
        `_PathViewport.keyPressEvent`/`_GridViewport.keyPressEvent`. VNF
        vertices are always 3D (no 2D top-down special case). Step size
        (1 unit, or 0.1/10 with Cmd/Shift held) via `_key_nudge_magnitude`
        -- note Cmd here means the fine-nudge modifier, distinct from its
        other use starting a vertex *drag* on mouse-press."""
        if self._editable and self._vert_indices:
            lock_axis = _view_locked_axis(self._renderer.camera)
            magnitude = _key_nudge_magnitude(event.modifiers())
            delta = _key_nudge_delta(self._renderer.camera, lock_axis, event.key(), magnitude)
            if delta is not None:
                for vi in self._vert_indices:
                    if 0 <= vi < len(self._verts_3d):
                        new_pt = self._verts_3d[vi] + delta
                        self.vertex_moved.emit(vi, round(float(new_pt[0]), 3),
                                                round(float(new_pt[1]), 3), round(float(new_pt[2]), 3))
                event.accept()
                return
        super().keyPressEvent(event)

    def _pick_face(self, px: float, py: float) -> int:
        if len(self._cpu_positions) == 0:
            return -1
        w, h = self.width(), self.height()
        if w == 0 or h == 0:
            return -1
        # The mesh buffer was uploaded with tri_ids=tri_to_face (see
        # VNFViewer._load_mesh), so ray_cast directly resolves the hit
        # triangle back to its face index.
        ray_o, ray_d = self._renderer.camera_ray(px, py, w, h)
        face_id = self._renderer.ray_cast(ray_o, ray_d)
        return face_id if face_id is not None else -1

    def closeEvent(self, event):
        self._vert_blink_timer.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# VNF Viewer
# ---------------------------------------------------------------------------

class VNFViewer(QDialog):
    """3D mesh viewer for VNF [vertices, faces] structures with vertex/face
    tables. Read-only by default; pass `editable=True` for a Save/Cancel
    editing mode (see `MatrixViewer` for the shared editing convention) --
    vertex *positions* only, face topology is never edited here."""

    committed = Signal(str)

    def __init__(self, title: str, vnf_value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "VNF Editor" if editable else "VNF Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 560)
        self._editable = editable
        self._vnf = vnf_value
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Viewport — match main window's perspective mode
        self._vp = _VNFViewport(splitter, editable=editable)
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            if hasattr(w, '_viewport'):
                self._vp._renderer.camera.orthographic = w._viewport._renderer.camera.orthographic
                break
        self._vp.face_clicked.connect(self._on_viewport_face_clicked)
        splitter.addWidget(self._vp)

        # Tables in a tab widget
        self._tab_widget = QTabWidget(splitter)

        self._vert_table = self._make_vert_table(vnf_value[0], editable)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._tab_widget.addTab(self._vert_table, f"Vertices ({len(vnf_value[0])})")

        self._face_table = self._make_face_table(vnf_value[1])
        self._face_table.itemSelectionChanged.connect(self._on_face_table_selection)
        self._tab_widget.addTab(self._face_table, f"Faces ({len(vnf_value[1])})")

        splitter.addWidget(self._tab_widget)
        vt = self._vert_table
        fm = vt.fontMetrics()
        vh_w = fm.horizontalAdvance(str(max(len(vnf_value[0]) - 1, 0))) + 20
        table_w = (vh_w
                   + sum(vt.columnWidth(j) for j in range(vt.columnCount()))
                   + vt.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._load_mesh)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)

    @staticmethod
    def _make_vert_table(verts, editable: bool = False) -> QTableWidget:
        t = QTableWidget(len(verts), 3)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["X", "Y", "Z"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(verts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if editable:
            t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                               | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, v in enumerate(verts):
            for j in range(3):
                item = QTableWidgetItem(f"{v[j]:g}")
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(3):
            t.setColumnWidth(j, min_w)
        return t

    @staticmethod
    def _make_face_table(faces) -> QTableWidget:
        t = QTableWidget(len(faces), 1)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["Vertex Indices"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(faces))])
        _style_table_headers(t)
        t.horizontalHeader().setStretchLastSection(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, f in enumerate(faces):
            text = "[" + ", ".join(str(int(idx)) for idx in f) + "]"
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(i, 0, item)
        return t

    def _load_mesh(self, reframe: bool = True):
        # Clears any mesh buffer from a prior call -- a no-op the first
        # time (schedule_load's initial-load path, buffers start empty),
        # but required now that an editable dialog's _rebuild() can call
        # this repeatedly, or it'd leak/duplicate GPU buffers.
        self._vp._renderer._clear_buffers()
        verts_raw, faces_raw = self._vnf
        verts = np.array(verts_raw, dtype=np.float32)
        all_positions = []
        all_normals = []
        all_edge_starts = []
        all_edge_ends = []
        tri_to_face = []

        for fi, face in enumerate(faces_raw):
            idxs = [int(i) for i in face]
            if len(idxs) < 3:
                continue
            v0 = verts[idxs[0]]
            for k in range(1, len(idxs) - 1):
                v1, v2 = verts[idxs[k]], verts[idxs[k + 1]]
                # Reverse winding: OpenSCAD uses CW from outside, OpenGL expects CCW
                n = np.cross(v2 - v0, v1 - v0)
                ln = np.linalg.norm(n)
                if ln > 0:
                    n /= ln
                all_positions.extend([v0, v2, v1])
                all_normals.extend([n, n, n])
                tri_to_face.append(fi)
            for k in range(len(idxs)):
                all_edge_starts.append(verts[idxs[k]])
                all_edge_ends.append(verts[idxs[(k + 1) % len(idxs)]])

        if not all_positions:
            return

        positions = np.array(all_positions, dtype=np.float32)
        normals = np.array(all_normals, dtype=np.float32)
        tri_to_face_arr = np.array(tri_to_face, dtype=np.int32)

        self._vp.set_face_data(positions, tri_to_face_arr, verts)

        edge_color = np.array([0.15, 0.15, 0.15], dtype=np.float32)
        starts = np.array(all_edge_starts, dtype=np.float32)
        ends = np.array(all_edge_ends, dtype=np.float32)
        n_edges = len(starts)
        cols = np.tile(edge_color, (n_edges, 1))
        edge_data = np.empty((n_edges * 2, 6), dtype=np.float32)
        edge_data[0::2] = np.concatenate([starts, cols], axis=1)
        edge_data[1::2] = np.concatenate([ends, cols], axis=1)

        # tri_ids=tri_to_face_arr lets SceneRenderer.ray_cast resolve a hit
        # triangle straight back to its OpenSCAD face index for picking.
        self._vp._renderer.upload_mesh(positions, normals,
                             color=(0.9, 0.85, 0.1, 1.0),
                             edge_positions=edge_data[:, :3],
                             edge_colors=edge_data[:, 3:],
                             tri_ids=tri_to_face_arr)

        bb_min = verts.min(axis=0)
        bb_max = verts.max(axis=0)
        self._vp.frame_scene(bb_min, bb_max, reframe=reframe)
        self._vp.update()

    def _rebuild(self, reframe: bool = True):
        """Re-triangulate and re-upload the mesh after a vertex edit --
        face topology never changes in this dialog, so `_load_mesh` just
        recomputes from the same faces against updated vertex positions.
        Unlike the initial `schedule_load` call, this needs its own
        makeCurrent bracket (a live edit isn't guaranteed to run inside
        `initializeGL`), and needs to explicitly rebuild the face-highlight
        overlay too -- `frame_scene` (which `_load_mesh` calls) already
        rebuilds vertex markers for us but not the highlight VAO, which
        would otherwise keep showing the *old* vertex positions for
        whichever face is currently highlighted. `reframe=False` (used for
        a live vertex drag/nudge) skips the camera re-fit -- see
        `_on_viewport_vertex_moved`."""
        if self._vp._ctx is not None:
            self._vp.makeCurrent()
            self._load_mesh(reframe=reframe)
            if self._vp._selected_face >= 0:
                self._vp._rebuild_highlight()
            self._vp.doneCurrent()
            self._vp.update()

    def _on_item_changed(self, item: QTableWidgetItem):
        i, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._vnf[0][i][j]:g}")
            self._vert_table.blockSignals(False)
            return
        self._vnf[0][i][j] = parsed
        self._vert_table.blockSignals(True)
        item.setText(f"{parsed:g}")
        self._vert_table.blockSignals(False)
        self._rebuild()

    def _on_viewport_vertex_moved(self, vi: int, x: float, y: float, z: float):
        """Live update while Cmd+dragging or arrow-key-nudging a vertex
        marker in the editable viewport -- mirrors `_on_item_changed`'s
        self._vnf + table + rebuild update, just driven by the viewport
        instead of a table-cell edit. `reframe=False`: a live move
        shouldn't re-fit/zoom the camera to the whole mesh on every
        frame -- `scroll_to_visible` instead just pans (if needed) to
        keep this one vertex on-screen."""
        self._vnf[0][vi][0] = x
        self._vnf[0][vi][1] = y
        self._vnf[0][vi][2] = z
        self._vert_table.blockSignals(True)
        self._vert_table.item(vi, 0).setText(f"{x:g}")
        self._vert_table.item(vi, 1).setText(f"{y:g}")
        self._vert_table.item(vi, 2).setText(f"{z:g}")
        self._vert_table.blockSignals(False)
        self._rebuild(reframe=False)
        self._vp.scroll_to_visible(np.array([x, y, z]))

    def _on_save(self):
        self.committed.emit(_format_value(self._vnf))
        self.accept()

    def _on_vert_table_selection(self):
        if self._syncing:
            return
        rows = self._vert_table.selectionModel().selectedRows()
        indices = [r.row() for r in rows]
        self._vp.highlight_vertices(indices)

    def _on_face_table_selection(self):
        if self._syncing:
            return
        rows = self._face_table.selectionModel().selectedRows()
        if rows:
            face_idx = rows[0].row()
            self._syncing = True
            self._vp.highlight_face(face_idx)
            self._select_face_vertices(face_idx)
            self._syncing = False
        else:
            self._syncing = True
            self._vp.highlight_face(-1)
            self._vert_table.clearSelection()
            self._vp.highlight_vertices([])
            self._syncing = False

    def _on_viewport_face_clicked(self, face_idx: int):
        if self._syncing:
            return
        self._syncing = True
        self._tab_widget.setCurrentIndex(1)
        if 0 <= face_idx < self._face_table.rowCount():
            self._face_table.selectRow(face_idx)
            self._face_table.scrollTo(self._face_table.model().index(face_idx, 0))
            self._select_face_vertices(face_idx)
        else:
            self._face_table.clearSelection()
            self._vert_table.clearSelection()
            self._vp.highlight_vertices([])
        self._syncing = False

    def _select_face_vertices(self, face_idx: int):
        """Select the vertices referenced by the given face in the vertex table."""
        from PySide6.QtCore import QItemSelectionModel
        faces_raw = self._vnf[1]
        if face_idx < 0 or face_idx >= len(faces_raw):
            return
        vert_indices = [int(i) for i in faces_raw[face_idx]]
        sel = self._vert_table.selectionModel()
        sel.clearSelection()
        model = self._vert_table.model()
        for vi in vert_indices:
            if 0 <= vi < self._vert_table.rowCount():
                idx = model.index(vi, 0)
                sel.select(idx, QItemSelectionModel.SelectionFlag.Select
                           | QItemSelectionModel.SelectionFlag.Rows)
        self._vp.highlight_vertices(vert_indices)



# ---------------------------------------------------------------------------
# Path Viewer
# ---------------------------------------------------------------------------

class PathViewer(QDialog):
    """2D/3D path viewer with vertex table, selectable markers, and hover
    tooltips. Read-only by default; pass `editable=True` for a Save/Cancel
    editing mode (see `MatrixViewer` for the shared editing convention)."""

    committed = Signal(str)

    def __init__(self, title: str, path_value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "Path Editor" if editable else "Path Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 520)

        self._editable = editable
        self._path = path_value
        self._is_2d = all(len(p) == 2 for p in path_value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _PathViewport(path_value, self._is_2d, self, editable=editable)
        splitter.addWidget(self._vp)

        self._vert_table = self._make_vert_table(path_value, self._is_2d, editable)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.add_vertex_requested.connect(self._on_viewport_add_vertex_requested)
            self._vp.bezier_vertex_added.connect(self._on_viewport_bezier_vertex_added)
            self._vp.delete_vertex_requested.connect(self._delete_vertex)
            self._vert_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._vert_table.customContextMenuRequested.connect(self._show_vert_table_context_menu)
        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        self._pts_label = QLabel(f"Path Points ({len(path_value)})")
        tc_layout.addWidget(self._pts_label)
        tc_layout.addWidget(self._vert_table, 1)
        splitter.addWidget(table_container)
        t = self._vert_table
        fm = t.fontMetrics()
        vh_w = max(fm.horizontalAdvance(str(max(len(path_value) - 1, 0))),
                   fm.horizontalAdvance("0000")) + 20
        table_w = (vh_w
                   + sum(t.columnWidth(j) for j in range(t.columnCount()))
                   + t.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        self._closed_cb = QCheckBox("Close Path")
        self._closed_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._closed_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._closed_cb)
        self._bezier_cb = QCheckBox("Bezier")
        self._bezier_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._bezier_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._bezier_cb)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._do_initial_load)

    @staticmethod
    def _make_vert_table(path_value: list, is_2d: bool, editable: bool = False) -> QTableWidget:
        cols = 2 if is_2d else 3
        t = QTableWidget(len(path_value), cols)
        t.setFont(QFont("Menlo", 11))
        headers = ["X", "Y"] if is_2d else ["X", "Y", "Z"]
        t.setHorizontalHeaderLabels(headers)
        t.setVerticalHeaderLabels([str(i) for i in range(len(path_value))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if editable:
            t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                               | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, p in enumerate(path_value):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(cols):
            t.setColumnWidth(j, min_w)
        return t

    def _populate_vert_table(self):
        """Rebuild the vertex table from `self._path` after a row-count
        change (add/delete vertex) -- unlike `_on_item_changed`/
        `_on_viewport_vertex_moved`, which only ever touch existing cells'
        text in place, adding/removing a vertex changes the row count, so
        the whole table needs repopulating (mirrors `GridViewer.
        _populate_table`'s same row-count-change situation)."""
        cols = 2 if self._is_2d else 3
        self._vert_table.blockSignals(True)
        self._vert_table.setRowCount(len(self._path))
        self._vert_table.setVerticalHeaderLabels([str(i) for i in range(len(self._path))])
        for i, p in enumerate(self._path):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._vert_table.setItem(i, j, item)
        self._vert_table.blockSignals(False)
        self._pts_label.setText(f"Path Points ({len(self._path)})")

    def _show_vert_table_context_menu(self, pos):
        """Right-click menu on the vertex table (`editable=True` only) --
        "this vertex" is the specific table row under the cursor, so the
        action is hidden when the click lands on empty space below the
        last point (mirrors `GridViewer`/`RegionViewer`'s table context
        menu convention)."""
        vertex_idx = self._vert_table.rowAt(pos.y())
        if vertex_idx < 0:
            return
        menu = QMenu(self._vert_table)
        menu.addAction("Delete Vertex", lambda: self._delete_vertex(vertex_idx))
        menu.exec(self._vert_table.viewport().mapToGlobal(pos))

    def _delete_vertex(self, vertex_idx: int | None = None):
        """Remove the given vertex (right-click on it in the viewport or
        in the vertex table), or -- if none given -- the selected table
        rows (the Delete/Backspace key -- see `keyPressEvent`), refusing
        if that would leave fewer than the 2 points a path requires
        (`_is_path`'s minimum) -- silently, matching the no-popup
        convention used for invalid cell edits elsewhere in this dialog."""
        if vertex_idx is not None:
            rows = [vertex_idx]
        else:
            rows = sorted((r.row() for r in self._vert_table.selectionModel().selectedRows()), reverse=True)
        if not rows or len(self._path) - len(rows) < 2:
            return
        if (self._bezier_cb.isChecked() and len(rows) == 1
                and rows[0] % 3 == 0 and len(self._path) >= 4):
            self._delete_bezier_v0(rows[0])
            return
        old_len = len(self._path)
        deleted = set(rows)
        index_map = {}
        new_i = 0
        for old_i in range(old_len):
            if old_i in deleted:
                continue
            index_map[old_i] = new_i
            new_i += 1
        self._vp.remap_node_types(index_map)
        for r in rows:
            del self._path[r]
        self._populate_vert_table()
        self._vert_table.clearSelection()
        self._rebuild()

    def _delete_bezier_v0(self, i0: int):
        """Delete an on-curve bezier vertex (v0) while doing its best to
        keep the curve's shape close to before, rather than leaving a
        straight-line gap:
        - Interior v0 (both a preceding and following segment exist,
          which for a closed path with >1 segment is always true): the
          two adjacent segments (p0,c1,c2,v0) and (v0,c3,c4,p3) merge
          into one new segment (p0, NEW_C1, NEW_C2, p3), least-squares
          fit to approximate the shape of both (_fit_merged_segment) --
          p0/p3 (the surviving neighboring v0s) keep their own position,
          but their own adjacent handle (c1, c4 respectively) gets
          replaced by the fitted one. Net -3 points.
        - Endpoint v0 (open path start/end, only one adjacent segment):
          no merge is meaningful -- removing an entire terminal segment
          necessarily just shortens the curve -- so the v0 and its own
          2 handles on the one side that exists are dropped outright,
          leaving the neighboring v0 as the new path start/end.
        Either way, self._node_types is reindexed first (so an explicit
        override elsewhere in the path survives), then p0/p3 (or whichever
        of them still exist) are *reclassified* from their new handle
        geometry -- p0's/p3's own adjacent handle just changed value, so
        their previously-recorded type may no longer match."""
        n = len(self._path)
        closed = self._closed_cb.isChecked()
        pts = self._vp._path_pts
        has_prev = closed or i0 - 3 >= 0
        has_next = closed or i0 + 3 < n

        order = range(n)
        if has_prev and has_next:
            prev_v0, next_v0 = (i0 - 3) % n, (i0 + 3) % n
            c1_idx, c2_idx = (i0 - 2) % n, (i0 - 1) % n
            c3_idx, c4_idx = (i0 + 1) % n, (i0 + 2) % n
            new_c1, new_c2 = _fit_merged_segment(
                pts[prev_v0], pts[c1_idx], pts[c2_idx], pts[i0],
                pts[c3_idx], pts[c4_idx], pts[next_v0])
            remove_set = {c2_idx, i0, c3_idx}
            replace_map = {c1_idx: new_c1, c4_idx: new_c2}
            reclassify_old = [prev_v0, next_v0]
            if closed:
                # A closed path's v0s must land at idx % 3 == 0 -- if the
                # deleted v0 was at (or "before") whatever old index
                # happens to end up as new index 0, everything after it
                # would be off by 1 or 2. Walking the survivors starting
                # from next_v0 (itself a v0, guaranteed to survive) keeps
                # every v0 aligned to a multiple of 3 in the new list,
                # same as _tessellate_bezier's own indexing convention.
                order = [(next_v0 + k) % n for k in range(n)]
        elif has_next:  # v0 at an open path's start
            remove_set = {i0, i0 + 1, i0 + 2}
            replace_map = {}
            reclassify_old = [i0 + 3]
        elif has_prev:  # v0 at an open path's end
            remove_set = {i0 - 2, i0 - 1, i0}
            replace_map = {}
            reclassify_old = [i0 - 3]
        else:
            return  # too few points for even one adjacent segment -- shouldn't happen (len >= 4 guard)

        is_2d = self._is_2d
        new_path = []
        index_map = {}
        for old_i in order:
            if old_i in remove_set:
                continue
            if old_i in replace_map:
                p = replace_map[old_i]
                val = [float(p[0]), float(p[1])] if is_2d else [float(p[0]), float(p[1]), float(p[2])]
            else:
                val = self._path[old_i]
            index_map[old_i] = len(new_path)
            new_path.append(val)

        self._vp.remap_node_types(index_map)
        self._path[:] = new_path
        self._populate_vert_table()
        self._vert_table.clearSelection()
        self._rebuild()
        for old_idx in reclassify_old:
            new_idx = index_map.get(old_idx)
            if new_idx is not None:
                self._vp.classify_single_node(new_idx)
        self._vp.refresh_markers()

    def _on_item_changed(self, item: QTableWidgetItem):
        i, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._path[i][j]:g}")
            self._vert_table.blockSignals(False)
            return
        self._path[i][j] = parsed
        self._vert_table.blockSignals(True)
        item.setText(f"{parsed:g}")
        self._vert_table.blockSignals(False)
        self._rebuild()

    def _on_save(self):
        self.committed.emit(_format_value(self._path))
        self.accept()

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        indices = sorted(r.row() for r in rows)
        self._vp.set_selected(indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        self._vert_table.clearSelection()
        if 0 <= vi < self._vert_table.rowCount():
            self._vert_table.selectRow(vi)

    def _on_viewport_vertex_moved(self, vi: int, x: float, y: float, z: float):
        """Live update while Cmd+dragging or arrow-key-nudging a vertex
        marker in the editable viewport -- mirrors `_on_item_changed`'s
        self._path + table + rebuild update, just driven by the viewport
        instead of a table-cell edit. `z` is ignored for 2D data (points
        are `[x, y]`). `reframe=False`: a live move shouldn't re-fit/zoom
        the camera to the whole path on every frame -- `scroll_to_visible`
        instead just pans (if needed) to keep this one vertex on-screen."""
        self._path[vi][0] = x
        self._path[vi][1] = y
        is_3d = len(self._path[vi]) > 2
        if is_3d:
            self._path[vi][2] = z
        self._vert_table.blockSignals(True)
        self._vert_table.item(vi, 0).setText(f"{x:g}")
        self._vert_table.item(vi, 1).setText(f"{y:g}")
        if is_3d:
            self._vert_table.item(vi, 2).setText(f"{z:g}")
        self._vert_table.blockSignals(False)
        self._rebuild(reframe=False)
        self._vp.scroll_to_visible(np.array([x, y, z if is_3d else 0.0]))

    def _on_viewport_add_vertex_requested(self, insert_after: int, x: float, y: float, z: float):
        """Right-click "Add Vertex" on a path line (`_PathViewport.
        contextMenuEvent`) -- inserts exactly at the clicked point on
        that segment. Row-count change, so repopulates the whole table
        like `_delete_vertex` does, rather than an in-place cell update."""
        new_pt = [x, y] if self._is_2d else [x, y, z]
        self._path.insert(insert_after + 1, new_pt)
        self._populate_vert_table()
        self._vert_table.selectRow(insert_after + 1)
        self._rebuild()

    def _on_viewport_bezier_vertex_added(self, i0: int, new_pts: list):
        """Bezier-mode "Add Vertex" (`_PathViewport.contextMenuEvent`'s
        De Casteljau split) -- splices the 5 new points (a, d, f, e, c) in
        place of the clicked segment's 2 old interior control points
        (index i0+1, i0+2), a net +3 points. Reindexes _node_types for
        every v0 *before* mutating self._path (old i0+1/i0+2 are dropped --
        they're control points anyway, never had entries -- and old
        i0+3 onward shifts by +3), then classifies just the new on-curve
        vertex f (at new index i0+3) without touching any other v0's
        already-recorded type."""
        old_len = len(self._path)
        index_map = {}
        for old_i in range(old_len):
            if old_i <= i0:
                index_map[old_i] = old_i
            elif old_i >= i0 + 3:
                index_map[old_i] = old_i + 3
        self._vp.remap_node_types(index_map)
        pts_as_lists = [([p[0], p[1]] if self._is_2d else [p[0], p[1], p[2]]) for p in new_pts]
        self._path[i0 + 1:i0 + 3] = pts_as_lists
        self._populate_vert_table()
        self._vert_table.selectRow(i0 + 3)
        self._rebuild()
        self._vp.classify_single_node(i0 + 3)
        self._vp.refresh_markers()

    def _do_initial_load(self):
        self._vp.load_path(self._path, self._closed_cb.isChecked(),
                           self._bezier_cb.isChecked())

    def _rebuild(self, _=None, reframe: bool = True):
        if self._vp._ctx is not None:
            self._vp.load_path(self._path, self._closed_cb.isChecked(),
                               self._bezier_cb.isChecked(), reframe=reframe)

    def keyPressEvent(self, event):
        """Delete/Backspace deletes the currently selected vertices
        (table selection and viewport selection are always kept in sync,
        so this works the same regardless of whether the selection was
        made by clicking the table or a viewport marker) -- reuses
        `_delete_vertex`'s no-arg table-selection path. Catches the key
        at the dialog level rather than on the table/viewport
        individually, since neither of those widgets consumes
        Delete/Backspace itself, so the event otherwise just bubbles up
        here unhandled anyway."""
        if self._editable and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_vertex()
            event.accept()
            return
        super().keyPressEvent(event)


# v0 index -> marker shape, keyed by bezier node type -- disjointed keeps
# the plain cube look; the other two get a distinct silhouette so a node's
# type is visible at a glance. Module-level (not a class attribute) so the
# names here unambiguously resolve to these free functions, not to any
# same-named instance method defined later in _PathViewport's class body.
_NODE_TYPE_SHAPE_FN = {
    "disjointed": _cube_faces,
    "same_angle": _diamond_faces,
    "symmetric": _triangle_faces,
}


class _PathViewport(Viewport):
    """Viewport subclass with selectable vertex markers and hover tooltips."""
    vertex_clicked = Signal(int)
    vertex_moved = Signal(int, float, float, float)  # (index, new_x, new_y, new_z) -- Cmd+drag, editable only
    add_vertex_requested = Signal(int, float, float, float)  # (insert_after_idx, x, y, z) -- right-click on a line, editable only
    delete_vertex_requested = Signal(int)  # (vertex_idx) -- right-click directly on a vertex, editable only
    # (v0_idx, [a, d, f, e, c]) -- bezier-mode "Add Vertex": De Casteljau
    # split of the cubic segment starting at v0_idx, splicing 5 new points
    # in place of its 2 old interior control points (see
    # _decasteljau_split_points/PathViewer._on_viewport_bezier_vertex_added)
    bezier_vertex_added = Signal(int, object)

    def __init__(self, path_value: list, is_2d: bool, parent=None, editable: bool = False):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        cam = self._renderer.camera
        cam.fov = 45.0
        self._renderer.line_width = 2.0
        self._editable = editable
        self._press_pos = None
        self._drag_started = False
        self._drag_vertex_idx = -1
        self._drag_lock_axis = 2
        self._drag_plane_point: np.ndarray = np.zeros(3, dtype=np.float32)
        self._context_menu_suppressed = False   # set from mouseReleaseEvent -- a real drag shouldn't also pop a menu
        self._path_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._closed = False
        self._bezier = False
        # v0 index -> "disjointed"/"same_angle"/"symmetric" (see
        # _classify_node_type) -- transient, viewer-only state, never
        # serialized. Auto-(re)classified from scratch whenever bezier mode
        # turns on (load_path); reindexed (not reclassified) on structural
        # point-list changes via remap_node_types, so explicit context-menu
        # overrides survive adds/deletes elsewhere in the path.
        self._node_types: dict[int, str] = {}
        self._is_2d = is_2d
        self._selected_indices: list[int] = []
        self._blink_red = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(250)
        self._blink_timer.timeout.connect(self._blink_tick)
        # Separate buffers for selected vertex markers (red/white blink pair)
        self._sel_vao_r = None
        self._sel_vbo_r = None
        self._sel_vao_w = None
        self._sel_vbo_w = None
        # 2D data is always viewed locked top-down (no meaningful "other side"
        # to orbit to); dragging away from it just gimbal-locks/disorients
        # without adding anything, so orbit is disabled entirely for 2D.
        self._orbit_enabled = not is_2d
        if is_2d:
            cam.azimuth = 270.0
            # Not exactly 90: gimbal lock at precisely elevation=+-90 makes
            # _look_at fall back to an arbitrary +X "right" vector that
            # doesn't match the azimuth-dependent basis orbit-dragging
            # continuously converges to just off the pole -- see the
            # matching fix/comment in viewport.py's "top" view preset.
            cam.elevation = 89.9999
            cam.orthographic = True
        else:
            cam.orthographic = False

    def _blink_tick(self):
        self._blink_red = not self._blink_red
        self.update()

    @staticmethod
    def _tessellate_bezier(pts: np.ndarray, closed: bool,
                           steps: int = 32) -> list[np.ndarray]:
        """Return list of line-segment endpoint pairs for cubic Bezier curves.

        Each group of 4 points (P0, C1, C2, P3) is one cubic segment, with
        shared endpoints between consecutive segments (P3 of one = P0 of next).
        Open path: needs 3k+1 points for k segments.
        Closed path: needs 3k points (last segment wraps to pts[0]).
        """
        n = len(pts)
        segments: list[tuple[int, int, int, int]] = []
        if closed:
            num_segs = n // 3
            for s in range(num_segs):
                i0 = (s * 3) % n
                i1 = (s * 3 + 1) % n
                i2 = (s * 3 + 2) % n
                i3 = (s * 3 + 3) % n
                segments.append((i0, i1, i2, i3))
        else:
            num_segs = (n - 1) // 3
            for s in range(num_segs):
                i0 = s * 3
                segments.append((i0, i0 + 1, i0 + 2, i0 + 3))

        pairs: list[np.ndarray] = []
        t_vals = np.linspace(0.0, 1.0, steps + 1, dtype=np.float32)
        for i0, i1, i2, i3 in segments:
            p0, p1, p2, p3 = pts[i0], pts[i1], pts[i2], pts[i3]
            omt = 1.0 - t_vals
            curve = (omt**3)[:, None] * p0 + \
                    (3 * omt**2 * t_vals)[:, None] * p1 + \
                    (3 * omt * t_vals**2)[:, None] * p2 + \
                    (t_vals**3)[:, None] * p3
            for j in range(steps):
                pairs.append(curve[j])
                pairs.append(curve[j + 1])
        return pairs

    def load_path(self, path_value: list, closed: bool, bezier: bool = False, reframe: bool = True):
        self.makeCurrent()
        self._renderer._clear_buffers()
        self._renderer.clear_simple_buffers()
        self._release_sel_markers()

        pts_3d = []
        for p in path_value:
            if len(p) == 2:
                pts_3d.append([p[0], p[1], 0.0])
            else:
                pts_3d.append([p[0], p[1], p[2]])
        pts = np.array(pts_3d, dtype=np.float32)
        self._path_pts = pts
        self._closed = closed
        # Auto-(re)classify node types only on the off->on transition --
        # geometry may have changed while bezier mode was off, but an
        # ordinary drag/nudge-triggered rebuild (bezier already on) must
        # NOT reclassify, or it would clobber explicit context-menu
        # overrides (and the type the user is actively dragging toward).
        if bezier and not self._bezier:
            self._classify_all_node_types()
        self._bezier = bezier

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_scene(bb_min, bb_max, reframe=reframe)

        line_color = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        if bezier and len(pts) >= 4:
            pairs = self._tessellate_bezier(pts, closed)
            if pairs:
                line_data = np.empty((len(pairs), 6), dtype=np.float32)
                for i, pt in enumerate(pairs):
                    line_data[i] = np.concatenate([pt, line_color])
                self._renderer.upload_lines(line_data)
            handle_color = np.array([0.0, 0.0, 0.0], dtype=np.float32)
            handle_pairs = []
            n = len(pts)
            if closed:
                num_segs = n // 3
                for s in range(num_segs):
                    i0 = (s * 3) % n
                    i1 = (s * 3 + 1) % n
                    i2 = (s * 3 + 2) % n
                    i3 = (s * 3 + 3) % n
                    handle_pairs.append(pts[i0])
                    handle_pairs.append(pts[i1])
                    handle_pairs.append(pts[i2])
                    handle_pairs.append(pts[i3])
            else:
                num_segs = (n - 1) // 3
                for s in range(num_segs):
                    i0 = s * 3
                    handle_pairs.append(pts[i0])
                    handle_pairs.append(pts[i0 + 1])
                    handle_pairs.append(pts[i0 + 2])
                    handle_pairs.append(pts[i0 + 3])
            if handle_pairs:
                hdata = np.empty((len(handle_pairs), 6), dtype=np.float32)
                for i, pt in enumerate(handle_pairs):
                    hdata[i] = np.concatenate([pt, handle_color])
                self._renderer.upload_lines(hdata)
        else:
            n = len(pts)
            seg_count = n if closed else n - 1
            line_data = np.empty((seg_count * 2, 6), dtype=np.float32)
            for i in range(seg_count):
                j = (i + 1) % n
                line_data[i * 2] = np.concatenate([pts[i], line_color])
                line_data[i * 2 + 1] = np.concatenate([pts[j], line_color])
            if seg_count > 0:
                self._renderer.upload_lines(line_data)

        self._build_point_markers()
        self.doneCurrent()
        self.update()

    def _classify_all_node_types(self):
        """(Re)build self._node_types from scratch by inspecting every v0's
        current handle geometry (see classify_single_node) -- called only
        on the bezier-mode off->on transition (load_path), never on an
        ordinary drag/nudge rebuild, so it can't clobber an explicit
        context-menu override or a type the user is actively dragging
        toward."""
        self._node_types = {}
        for i0 in range(0, len(self._path_pts), 3):
            self.classify_single_node(i0)

    def classify_single_node(self, v0_idx: int):
        """Classify (and store) just one v0's type from its current handle
        geometry (see _classify_node_type), without touching any other
        v0's already-recorded type -- used for a freshly-added vertex
        (bezier-mode Add Vertex) where every *other* v0's type must be
        left exactly as it was."""
        pts = self._path_pts
        prev_idx, fwd_idx = _v0_handle_indices(v0_idx, len(pts), self._closed)
        handle_a = pts[prev_idx] if prev_idx is not None else None
        handle_b = pts[fwd_idx] if fwd_idx is not None else None
        self._node_types[v0_idx] = _classify_node_type(pts[v0_idx], handle_a, handle_b)

    def remap_node_types(self, index_map: dict[int, int]):
        """Called by PathViewer right before a structural point-list change
        (add/delete vertex) takes effect, so self._node_types tracks the
        same v0 across the reindex instead of being silently dropped or
        misattributed to whatever point ends up at its old index."""
        self._node_types = _remap_node_types(self._node_types, index_map)

    def _set_node_type(self, vi: int, node_type: str):
        """Explicit context-menu override -- persists across ordinary
        drag/nudge-triggered rebuilds (those never call
        _classify_all_node_types()), only reset by a bezier-mode off->on
        toggle or a structural point-list change (see remap_node_types).
        Also immediately snaps both adjacent handles into line with the
        new type (see _snap_handles_to_node_type) rather than leaving
        them wherever they were until the next drag -- emitted as
        ordinary vertex_moved signals so the write-back into self._path,
        the table, and the live rebuild all go through the exact same
        path a real drag would use."""
        self._node_types[vi] = node_type
        pts = self._path_pts
        prev_idx, fwd_idx = _v0_handle_indices(vi, len(pts), self._closed)
        handle_a = pts[prev_idx] if prev_idx is not None else None
        handle_b = pts[fwd_idx] if fwd_idx is not None else None
        snapped = _snap_handles_to_node_type(pts[vi], handle_a, handle_b, node_type)
        if snapped is not None:
            new_a, new_b = snapped
            self.vertex_moved.emit(prev_idx, round(float(new_a[0]), 3),
                                    round(float(new_a[1]), 3), round(float(new_a[2]), 3))
            self.vertex_moved.emit(fwd_idx, round(float(new_b[0]), 3),
                                    round(float(new_b[1]), 3), round(float(new_b[2]), 3))
        else:
            self.refresh_markers()

    def refresh_markers(self):
        """Rebuild + repaint the point markers outside the normal
        load_path/set_selected flow (both of which already bracket
        _build_point_markers with makeCurrent/doneCurrent themselves) --
        needed wherever _node_types changes without a full load_path, e.g.
        a context-menu node-type override or classifying a freshly-split
        vertex."""
        self.makeCurrent()
        self._build_point_markers()
        self.doneCurrent()
        self.update()

    def _resolve_bezier_moves(self, dragged_idx: int, new_pos: np.ndarray) -> list[tuple[int, np.ndarray]]:
        """Shared by mouseMoveEvent (drag) and keyPressEvent (arrow-key
        nudge) -- both just need "this point moved to new_pos, what else
        (if anything) needs to move with it." Bezier off, or too few
        points to have a real bezier structure: today's plain single-point
        behavior, unchanged."""
        if not self._bezier or len(self._path_pts) < 4:
            return [(dragged_idx, new_pos)]
        v0_idx = _owning_v0_index(dragged_idx, len(self._path_pts), self._closed)
        node_type = self._node_types.get(v0_idx, "disjointed")
        return _bezier_linked_moves(self._path_pts, self._closed, dragged_idx, new_pos, node_type)

    def _cube_faces(self, r):
        return _cube_faces(r, self._is_2d)

    def _unit_faces_for_point(self, idx: int) -> list:
        if self._bezier and idx % 3 == 0:
            shape_fn = _NODE_TYPE_SHAPE_FN.get(
                self._node_types.get(idx, "disjointed"), _cube_faces)
            return shape_fn(1.0, self._is_2d)
        return self._cube_faces(1.0)

    def _build_point_markers(self):
        self._renderer.clear_points()

        pts = self._path_pts
        if len(pts) == 0 or self._ctx is None:
            return

        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            r = _marker_radius_for_point(self, pt)
            for v0, v1, v2 in self._unit_faces_for_point(i):
                marker_tris.append(np.concatenate([pt + v0 * r, green]))
                marker_tris.append(np.concatenate([pt + v1 * r, green]))
                marker_tris.append(np.concatenate([pt + v2 * r, green]))

        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected(self, indices: list[int]):
        self.makeCurrent()
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.doneCurrent()
            self.update()
            return

        if 0 <= indices[0] < len(self._path_pts):
            self.scroll_to_visible(self._path_pts[indices[0]])

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
        self.doneCurrent()
        self.update()

    def _release_sel_markers(self):
        for attr in ("_sel_vao_r", "_sel_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_sel_vbo_r", "_sel_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_sel_markers(self):
        self._release_sel_markers()
        if not self._selected_indices or self._ctx is None:
            return

        unit_faces = self._cube_faces(1.0)
        pts = self._path_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    r = _marker_radius_for_point(self, pt)
                    for v0, v1, v2 in unit_faces:
                        tris.append(np.concatenate([pt + v0 * r, color_val]))
                        tris.append(np.concatenate([pt + v1 * r, color_val]))
                        tris.append(np.concatenate([pt + v2 * r, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._gizmo_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        vao = self._sel_vao_r if self._blink_red else self._sel_vao_w
        if vao is not None:
            self._renderer._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            self._ctx.disable(mgl.DEPTH_TEST)
            vao.render(mgl.TRIANGLES)
            self._ctx.enable(mgl.DEPTH_TEST)

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        # Always called from within an already-makeCurrent'd caller
        # (load_path's own bracket) -- must NOT bracket with its own
        # makeCurrent/doneCurrent, since doneCurrent() would prematurely
        # release the context out from under load_path's remaining work.
        super().frame_scene(bb_min, bb_max, reframe=reframe)
        if len(self._path_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        # Unlike frame_scene above, this is a genuine external Qt event --
        # never called from inside another makeCurrent'd block -- so it
        # does need its own bracket.
        super().wheelEvent(event)
        if len(self._path_pts) > 0:
            self.makeCurrent()
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
            self.doneCurrent()
            self.update()

    def closeEvent(self, event):
        self._blink_timer.stop()
        super().closeEvent(event)

    def _pick_vertex(self, px: float, py: float) -> int:
        if len(self._path_pts) == 0:
            return -1
        return self._renderer.pick_nearest_point(self._path_pts, px, py, self.width(), self.height())

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        self._drag_vertex_idx = -1
        # A new press always starts a fresh gesture -- clear any leftover
        # suppression from the *previous* one. Needed because contextMenuEvent
        # fires on the right-button press on macOS (not after release), so
        # without this a single right-drag-pan would permanently block every
        # later right-click's context menu (this new press's own release
        # hasn't happened yet to update the flag by the time contextMenuEvent
        # runs for it).
        self._context_menu_suppressed = False
        if (self._editable
                and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier   # Cmd on macOS
                and not (event.modifiers() & Qt.KeyboardModifier.AltModifier)):
            vi = self._pick_vertex(self._press_pos.x(), self._press_pos.y())
            if vi >= 0:
                self._drag_vertex_idx = vi
                if self._is_2d:
                    self._drag_lock_axis = 2
                    self._drag_plane_point = np.zeros(3, dtype=np.float32)
                else:
                    self._drag_lock_axis = _view_locked_axis(self._renderer.camera)
                    self._drag_plane_point = self._path_pts[vi].copy()
                self._show_delta(f"Plane: {_unlocked_plane_name(self._drag_lock_axis)}")
                return   # don't arm orbit/pan -- this press starts a vertex drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._drag_vertex_idx >= 0:
            pos = event.position().toPoint()
            ray_o, ray_d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
            hit = _ray_plane_axis_locked(ray_o, ray_d, self._drag_plane_point, self._drag_lock_axis)
            if hit is not None:
                for idx, new_pos in self._resolve_bezier_moves(self._drag_vertex_idx, hit):
                    self.vertex_moved.emit(idx, round(float(new_pos[0]), 3),
                                            round(float(new_pos[1]), 3), round(float(new_pos[2]), 3))
            return
        if self._last_mouse is None:
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            if vi >= 0:
                pt = self._path_pts[vi]
                coords = f"({pt[0]:g}, {pt[1]:g}" + (f", {pt[2]:g})" if not self._is_2d else ")")
                self.setToolTip(f"[{vi}]: {coords}")
            else:
                self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_vertex_idx >= 0:
            vi = self._drag_vertex_idx
            moved = self._drag_started
            self._context_menu_suppressed = moved
            self._drag_vertex_idx = -1
            self._press_pos = None
            self._drag_started = False
            self._delta_label.hide()
            if not moved:
                self.vertex_clicked.emit(vi)   # plain click on a vertex, no actual drag
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        # Captured here (a genuine right-button pan drag), not left as a
        # side effect of the reset below -- contextMenuEvent fires right
        # after this handler and needs to know whether *this* release
        # followed a drag, so a pan gesture doesn't also pop up a menu.
        self._context_menu_suppressed = self._drag_started
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """Right-clicking a path line (editable only, and only when not
        over an existing vertex, and not immediately after a drag) offers
        "Add Vertex", inserting a new point exactly where clicked on that
        segment. The segment list always includes the closing edge when
        `Close Path` is checked (`self._closed`) -- the wrap-around
        segment's "insert after" index is `len(self._path_pts) - 1`, which
        correctly appends the new point at the end of the list rather
        than inserting before index 0. Right-clicking directly on a vertex
        instead offers "Delete Vertex" for that specific point."""
        if not self._editable or self._context_menu_suppressed:
            return
        pos = event.pos()
        vi = self._pick_vertex(pos.x(), pos.y())
        if vi >= 0:
            # On macOS, contextMenuEvent fires on the right-button *press*,
            # not after release (unlike a plain click) -- so this reset
            # must happen only once we're sure a menu is actually about to
            # show, right before QMenu.exec(). Resetting unconditionally at
            # the top of this method (an earlier version of this fix) reset
            # _last_mouse/_mouse_button on every right-click including
            # blank-space ones that show no menu at all, which broke
            # ordinary right-drag panning outright. Qt still doesn't
            # reliably deliver this widget's own mouseReleaseEvent for a
            # click that *does* open a menu (the popup's own event loop can
            # swallow it), so the reset is still needed here -- just scoped
            # to only the paths that actually call exec().
            self._last_mouse = None
            self._mouse_button = None
            menu = QMenu(self)
            if self._bezier and vi % 3 == 0:
                type_menu = menu.addMenu("Bezier Node Type")
                current = self._node_types.get(vi, "disjointed")
                for label, key in (("Disjointed", "disjointed"),
                                   ("Same Angle", "same_angle"),
                                   ("Symmetric", "symmetric")):
                    action = type_menu.addAction(label, lambda k=key: self._set_node_type(vi, k))
                    action.setCheckable(True)
                    action.setChecked(key == current)
                menu.addSeparator()
            menu.addAction("Delete Vertex", lambda: self.delete_vertex_requested.emit(vi))
            menu.exec(event.globalPos())
            return
        n = len(self._path_pts)
        if n < 2:
            return
        if self._bezier and n >= 4:
            picked = self._pick_bezier_segment_t(pos.x(), pos.y())
            if picked is None:
                return
            i0, t = picked
            new_pts = self._decasteljau_split_points(i0, t)
            self._last_mouse = None
            self._mouse_button = None
            menu = QMenu(self)
            menu.addAction("Add Vertex", lambda: self.bezier_vertex_added.emit(i0, new_pts))
            menu.exec(event.globalPos())
            return
        seg_count = n if self._closed else n - 1
        segments = [(i, (i + 1) % n) for i in range(seg_count)]
        result = self._renderer.pick_nearest_segment(self._path_pts, segments, pos.x(), pos.y(), self.width(), self.height())
        if result is None:
            return
        seg_idx, world_pt = result
        insert_after, _ = segments[seg_idx]
        x, y, z = float(world_pt[0]), float(world_pt[1]), float(world_pt[2])
        self._last_mouse = None
        self._mouse_button = None
        menu = QMenu(self)
        menu.addAction("Add Vertex", lambda: self.add_vertex_requested.emit(insert_after, x, y, z))
        menu.exec(event.globalPos())

    def _pick_bezier_segment_t(self, px: float, py: float) -> tuple[int, float] | None:
        """Pick against the *tessellated* bezier curve (not the straight
        v0-to-v0 line -- the actual curve doesn't lie on it once handles
        pull it away) -- returns (v0_idx, t) for the cubic segment and
        curve parameter nearest the click, or None. Reuses
        _tessellate_bezier's own sampling (steps=32) so picking and
        rendering can never drift apart."""
        steps = 32
        pairs = self._tessellate_bezier(self._path_pts, self._closed, steps)
        if not pairs:
            return None
        sample_pts = np.array(pairs, dtype=np.float32)
        pick_segments = [(2 * k, 2 * k + 1) for k in range(len(pairs) // 2)]
        result = self._renderer.pick_nearest_segment(sample_pts, pick_segments, px, py, self.width(), self.height())
        if result is None:
            return None
        micro_idx, world_pt = result
        cubic_seg, local_k = divmod(micro_idx, steps)
        n = len(self._path_pts)
        i0 = (cubic_seg * 3) % n if self._closed else cubic_seg * 3
        t_vals = np.linspace(0.0, 1.0, steps + 1)
        p_a, p_b = sample_pts[2 * micro_idx], sample_pts[2 * micro_idx + 1]
        seg_vec = p_b - p_a
        seg_len_sq = float(np.dot(seg_vec, seg_vec))
        frac = float(np.dot(world_pt - p_a, seg_vec) / seg_len_sq) if seg_len_sq > 1e-12 else 0.0
        frac = max(0.0, min(1.0, frac))
        t = float(t_vals[local_k] + frac * (t_vals[local_k + 1] - t_vals[local_k]))
        return i0, t

    def _decasteljau_split_points(self, i0: int, t: float) -> list[tuple[float, float, float]]:
        """The 5 new points (a, d, f, e, c) from splitting the cubic
        segment starting at v0 index i0, at curve parameter t -- see
        _decasteljau_split. f (index 2 of the 5) is the new on-curve
        vertex."""
        pts = self._path_pts
        n = len(pts)
        if self._closed:
            i1, i2, i3 = (i0 + 1) % n, (i0 + 2) % n, (i0 + 3) % n
        else:
            i1, i2, i3 = i0 + 1, i0 + 2, i0 + 3
        a, d, f, e, c = _decasteljau_split(pts[i0], pts[i1], pts[i2], pts[i3], t)
        return [tuple(float(x) for x in p) for p in (a, d, f, e, c)]

    def keyPressEvent(self, event):
        """Arrow keys nudge every selected vertex, confined to the same
        axis-locked plane Cmd+drag uses -- Z for 2D data (always top-down
        locked), whichever axis `_view_locked_axis` picks for the current
        camera angle otherwise. Step size (1 unit, or 0.1/10 with
        Cmd/Shift held) via `_key_nudge_magnitude` -- note Cmd here means
        the fine-nudge modifier, distinct from its other use starting a
        vertex *drag* on mouse-press."""
        if self._editable and self._selected_indices:
            lock_axis = 2 if self._is_2d else _view_locked_axis(self._renderer.camera)
            magnitude = _key_nudge_magnitude(event.modifiers())
            delta = _key_nudge_delta(self._renderer.camera, lock_axis, event.key(), magnitude)
            if delta is not None:
                # Same bezier linking as a drag (_resolve_bezier_moves) --
                # if two linked points are *both* independently selected,
                # the later one's own independent nudge runs after the
                # earlier one's link already moved it (last-write-wins for
                # that one key event); a minor, acceptable edge case.
                for vi in self._selected_indices:
                    if 0 <= vi < len(self._path_pts):
                        new_pt = self._path_pts[vi] + delta
                        for idx, moved_pt in self._resolve_bezier_moves(vi, new_pt):
                            self.vertex_moved.emit(idx, round(float(moved_pt[0]), 3),
                                                    round(float(moved_pt[1]), 3), round(float(moved_pt[2]), 3))
                event.accept()
                return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Grid Viewer
# ---------------------------------------------------------------------------

class GridViewer(QDialog):
    """3D grid viewer for lists of lists of points with quad mesh faces.
    Read-only by default; pass `editable=True` for a Save/Cancel editing
    mode (see `MatrixViewer` for the shared editing convention). Edits
    apply to whichever row is currently shown."""

    committed = Signal(str)

    def __init__(self, title: str, grid_value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "Grid Editor" if editable else "Grid Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 520)

        self._editable = editable
        self._grid = grid_value
        self._rows = len(grid_value)
        self._row_offsets = _grid_row_offsets(grid_value)
        all_pts = [p for row in grid_value for p in row]
        self._is_2d = all(len(p) == 2 for p in all_pts)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _GridViewport(grid_value, self._is_2d, self, editable=editable)
        splitter.addWidget(self._vp)

        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)

        row_bar = QHBoxLayout()
        row_bar.addWidget(QLabel("Row:"))
        self._row_combo = QComboBox()
        for i in range(self._rows):
            self._row_combo.addItem(str(i))
        self._row_combo.currentIndexChanged.connect(self._on_row_changed)
        row_bar.addWidget(self._row_combo, 1)
        tc_layout.addLayout(row_bar)

        self._pts_label = QLabel()
        tc_layout.addWidget(self._pts_label)

        self._vert_table = self._make_vert_table(grid_value[0], self._is_2d, editable)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        if editable:
            self._vert_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._vert_table.customContextMenuRequested.connect(self._show_vert_table_context_menu)
        tc_layout.addWidget(self._vert_table, 1)

        splitter.addWidget(table_container)
        t = self._vert_table
        fm = t.fontMetrics()
        max_row_len = max((len(row) for row in grid_value), default=0)
        vh_w = max(fm.horizontalAdvance(str(max(max_row_len - 1, 0))),
                   fm.horizontalAdvance("0000")) + 20
        table_w = (vh_w
                   + sum(t.columnWidth(j) for j in range(t.columnCount()))
                   + t.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        self._face_mode_combo = QComboBox()
        self._face_mode_combo.addItem("Grid Only")
        self._face_mode_combo.addItem("Grid Faces")
        is_4x4 = self._rows == 4 and all(len(row) == 4 for row in grid_value)
        if is_4x4:
            self._face_mode_combo.addItem("Bezier Patch")
        self._face_mode_combo.setCurrentIndex(1)
        self._face_mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        _size_combo_to_widest_item(self._face_mode_combo)
        self._face_mode_combo.currentIndexChanged.connect(self._rebuild)
        btn_row.addWidget(self._face_mode_combo)
        btn_row.addSpacing(20)
        self._wrap_combo = QComboBox()
        self._wrap_combo.addItem("No Wrap")
        self._wrap_combo.addItem("Wrap Columns")
        self._wrap_combo.addItem("Wrap Rows")
        self._wrap_combo.addItem("Wrap Both")
        self._wrap_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        _size_combo_to_widest_item(self._wrap_combo)
        self._wrap_combo.currentIndexChanged.connect(self._rebuild)
        btn_row.addWidget(self._wrap_combo)
        btn_row.addSpacing(20)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._on_row_changed(0)
        self._vp.schedule_load(self._do_initial_load)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.delete_row_requested.connect(self._on_viewport_delete_row_requested)
            self._vp.delete_column_requested.connect(self._on_viewport_delete_column_requested)

    @staticmethod
    def _make_vert_table(row_pts: list, is_2d: bool, editable: bool = False) -> QTableWidget:
        cols = 2 if is_2d else 3
        t = QTableWidget(len(row_pts), cols)
        t.setFont(QFont("Menlo", 11))
        headers = ["X", "Y"] if is_2d else ["X", "Y", "Z"]
        t.setHorizontalHeaderLabels(headers)
        t.setVerticalHeaderLabels([str(i) for i in range(len(row_pts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if editable:
            t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                               | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setStyleSheet(
            "QTableWidget::item:selected:!active {"
            "  background: palette(highlight);"
            "  color: palette(highlighted-text);"
            "}"
        )
        for i, p in enumerate(row_pts):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(cols):
            t.setColumnWidth(j, min_w)
        return t

    def _populate_table(self, row_pts: list):
        cols = 2 if self._is_2d else 3
        self._vert_table.blockSignals(True)
        self._vert_table.setRowCount(len(row_pts))
        self._vert_table.setVerticalHeaderLabels(
            [str(i) for i in range(len(row_pts))])
        for i, p in enumerate(row_pts):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._vert_table.setItem(i, j, item)
        self._vert_table.blockSignals(False)

    def _on_row_changed(self, row_idx: int, select_all: bool = True):
        if row_idx < 0 or row_idx >= self._rows:
            return
        row_pts = self._grid[row_idx]
        self._pts_label.setText(f"Row Points ({len(row_pts)})")
        self._populate_table(row_pts)
        if select_all:
            self._vert_table.selectAll()
            self._vp.set_selected_row(row_idx)
        else:
            # Delete Row/Column pass select_all=False -- the user should
            # end up with nothing selected after a delete, not the whole
            # (possibly renumbered) row re-selected as a side effect of
            # switching to it. clearSelection() alone isn't enough: if
            # _populate_table happened to leave no rows selected already,
            # Qt won't fire itemSelectionChanged (no change to signal),
            # so _vp.set_selected([]) is called explicitly too rather
            # than relying on that signal to clear the viewport's own
            # (still-blinking) selection.
            self._vert_table.clearSelection()
            self._vp.set_selected([])

    def _sync_face_mode_combo(self):
        """Add/remove the "Bezier Patch" option to match whether the grid
        is still exactly 4x4 after a row/column add/delete (a plain
        checkbox-style toggle isn't enough here since the option should
        only exist at all for a 4x4 grid, same as at construction time)."""
        is_4x4 = len(self._grid) == 4 and all(len(row) == 4 for row in self._grid)
        has_bezier = self._face_mode_combo.count() == 3
        if is_4x4 and not has_bezier:
            self._face_mode_combo.addItem("Bezier Patch")
        elif not is_4x4 and has_bezier:
            if self._face_mode_combo.currentIndex() == 2:
                self._face_mode_combo.setCurrentIndex(1)
            self._face_mode_combo.removeItem(2)
        _size_combo_to_widest_item(self._face_mode_combo)

    def _refresh_row_bookkeeping(self):
        """Recompute self._rows/self._row_offsets from self._grid -- call
        after any row or column count change, before anything that indexes
        into the grid via the cached offsets (_on_vert_table_selection,
        _on_viewport_vertex_clicked/_moved)."""
        self._rows = len(self._grid)
        self._row_offsets = _grid_row_offsets(self._grid)

    def _refresh_row_combo_items(self):
        """Repopulate the row combo's 0..N-1 item labels -- call only when
        the row *count* itself changed (Add/Delete Row), not for a
        column-only change (row count is unaffected by those)."""
        self._row_combo.blockSignals(True)
        self._row_combo.clear()
        for i in range(self._rows):
            self._row_combo.addItem(str(i))
        self._row_combo.blockSignals(False)

    def _is_bezier_mode(self) -> bool:
        """Add/Delete Row/Column would break the exactly-4x4 shape a
        Bezier Patch's control net requires, so every entry point into
        those mutations (table context menu, viewport context menu) is
        disabled while this mode is active."""
        return self._face_mode_combo.currentText() == "Bezier Patch"

    def _show_vert_table_context_menu(self, pos):
        """Right-click menu on the vertex table (`editable=True` only),
        replacing the old Add/Delete Row/Column buttons -- "this row" is
        always whichever row `_row_combo` currently shows; "this column"
        is the specific table row (a grid column/point index) under the
        cursor, so the column-specific actions are hidden when the click
        lands on empty space below the last point. No menu at all in
        Bezier Patch mode -- see `_is_bezier_mode`."""
        if self._is_bezier_mode():
            return
        col_idx = self._vert_table.rowAt(pos.y())
        menu = QMenu(self._vert_table)
        menu.addAction("Add Row Before", lambda: self._add_row(before=True))
        menu.addAction("Add Row After", lambda: self._add_row(before=False))
        if col_idx >= 0:
            menu.addAction("Add Column Before", lambda: self._add_column(before=True, col_idx=col_idx))
            menu.addAction("Add Column After", lambda: self._add_column(before=False, col_idx=col_idx))
        menu.addSeparator()
        menu.addAction("Delete this Row", self._delete_row)
        if col_idx >= 0:
            menu.addAction("Delete this Column", lambda: self._delete_column(col_idx))
        menu.exec(self._vert_table.viewport().mapToGlobal(pos))

    def _add_row(self, before: bool):
        """Insert a new row before or after the currently displayed row.
        New points are the midpoint of the corresponding column in this
        row and its neighbor in that direction (wrapping around if Row
        Wrap is on), truncated/padded to this row's own length if the
        neighbor is a different length (ragged grid); or, if there's no
        such neighbor (row 0's "before", or the last row's "after", with
        no wrap), this row's own points nudged along their last
        coordinate so the new row isn't exactly coincident. Refuses
        silently in Bezier Patch mode -- see `_is_bezier_mode`."""
        if self._is_bezier_mode():
            return
        row_idx = self._row_combo.currentIndex()
        cur = self._grid[row_idx]
        row_wrap = self._wrap_flags()[1]
        if before:
            neighbor_idx = row_idx - 1
            if neighbor_idx < 0:
                neighbor_idx = self._rows - 1 if row_wrap else None
            insert_at = row_idx
        else:
            neighbor_idx = row_idx + 1
            if neighbor_idx >= self._rows:
                neighbor_idx = 0 if row_wrap else None
            insert_at = row_idx + 1
        if neighbor_idx is not None:
            nbr = self._grid[neighbor_idx]
            shared = min(len(cur), len(nbr))
            new_row = [[(a + b) / 2.0 for a, b in zip(cur[c], nbr[c])] for c in range(shared)]
            new_row += [list(cur[c]) for c in range(shared, len(cur))]
        else:
            new_row = []
            for p in cur:
                pt = list(p)
                pt[-1] += 1.0
                new_row.append(pt)
        self._grid.insert(insert_at, new_row)
        self._sync_face_mode_combo()
        self._vp.sync_row_bookkeeping(self._grid)
        self._rebuild()
        self._refresh_row_bookkeeping()
        self._refresh_row_combo_items()
        self._row_combo.blockSignals(True)
        self._row_combo.setCurrentIndex(insert_at)
        self._row_combo.blockSignals(False)
        self._on_row_changed(insert_at)

    def _delete_row(self):
        """Remove the currently displayed row, refusing if that would
        leave fewer than the 2 rows a grid requires (`_is_grid`'s
        minimum), or in Bezier Patch mode (see `_is_bezier_mode`) --
        silently, matching the no-popup convention used for invalid cell
        edits elsewhere in this dialog."""
        if self._rows <= 2 or self._is_bezier_mode():
            return
        row_idx = self._row_combo.currentIndex()
        del self._grid[row_idx]
        self._sync_face_mode_combo()
        self._vp.sync_row_bookkeeping(self._grid)
        self._rebuild()
        self._refresh_row_bookkeeping()
        self._refresh_row_combo_items()
        select = min(row_idx, self._rows - 1)
        self._row_combo.blockSignals(True)
        self._row_combo.setCurrentIndex(select)
        self._row_combo.blockSignals(False)
        self._on_row_changed(select, select_all=False)

    def _add_column(self, before: bool, col_idx: int):
        """Insert a new point before or after `col_idx` in every row that
        reaches that column -- rows too short for it (a ragged grid) are
        left unchanged. Each row's neighbor/insert position is computed
        against its *own* length (not the clicked row's), so a ragged
        grid's shorter/longer rows each still get a sensible placement.
        Per-row placement follows the same midpoint/nudge logic as
        `_add_row`, just along the row instead of across rows. Refuses
        silently in Bezier Patch mode -- see `_is_bezier_mode`."""
        if self._is_bezier_mode():
            return
        row_idx = self._row_combo.currentIndex()
        col_wrap = self._wrap_flags()[0]
        for row in self._grid:
            if col_idx >= len(row):
                continue
            cur = row[col_idx]
            if before:
                neighbor_idx = col_idx - 1
                if neighbor_idx < 0:
                    neighbor_idx = len(row) - 1 if col_wrap else None
                insert_at = col_idx
            else:
                neighbor_idx = col_idx + 1
                if neighbor_idx >= len(row):
                    neighbor_idx = 0 if col_wrap else None
                insert_at = col_idx + 1
            if neighbor_idx is not None:
                nxt = row[neighbor_idx]
                new_pt = [(a + b) / 2.0 for a, b in zip(cur, nxt)]
            else:
                new_pt = list(cur)
                new_pt[-1] += 1.0
            row.insert(insert_at, new_pt)
        self._sync_face_mode_combo()
        self._vp.sync_row_bookkeeping(self._grid)
        self._rebuild()
        self._refresh_row_bookkeeping()
        self._on_row_changed(row_idx)

    def _delete_column(self, col_idx: int):
        """Remove `col_idx` from every row that reaches it, refusing
        entirely if that would empty any affected row (a grid row needs
        >= 1 point), or in Bezier Patch mode (see `_is_bezier_mode`) --
        silently, same convention as `_delete_row`."""
        if self._is_bezier_mode():
            return
        row_idx = self._row_combo.currentIndex()
        for row in self._grid:
            if col_idx < len(row) and len(row) - 1 < 1:
                return
        for row in self._grid:
            if col_idx < len(row):
                del row[col_idx]
        self._sync_face_mode_combo()
        self._vp.sync_row_bookkeeping(self._grid)
        self._rebuild()
        self._refresh_row_bookkeeping()
        self._on_row_changed(row_idx, select_all=False)

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        col_indices = sorted(r.row() for r in rows)
        row_idx = self._row_combo.currentIndex()
        row_start = self._row_offsets[row_idx]
        global_indices = [row_start + c for c in col_indices]
        self._vp.set_selected(global_indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        if vi < 0:
            # Plain left-click on blank viewport space -- deselect
            # everything (matches PathViewer/RegionViewer/VNFViewer),
            # rather than the previous no-op. Clearing the table
            # selection is enough: itemSelectionChanged already drives
            # _on_vert_table_selection -> _vp.set_selected([]), which
            # stops the blink timer and rebuilds markers as all-green.
            self._vert_table.clearSelection()
            return
        row_idx, col_idx = _grid_flat_to_rc(vi, self._row_offsets)
        if row_idx != self._row_combo.currentIndex():
            self._row_combo.setCurrentIndex(row_idx)
        self._vert_table.clearSelection()
        if 0 <= col_idx < self._vert_table.rowCount():
            self._vert_table.selectRow(col_idx)

    def _on_viewport_delete_row_requested(self, vi: int):
        """Right-click "Delete Row" on a vertex in the viewport
        (`_GridViewport.contextMenuEvent`) -- `_delete_row` always acts on
        whichever row `_row_combo` currently shows, so switch to the
        clicked vertex's row first (matches `_on_viewport_vertex_clicked`)."""
        row_idx, _ = _grid_flat_to_rc(vi, self._row_offsets)
        self._row_combo.setCurrentIndex(row_idx)
        self._delete_row()

    def _on_viewport_delete_column_requested(self, vi: int):
        """Right-click "Delete Column" on a vertex in the viewport --
        `_delete_column` removes that column position from every row that
        reaches it, so the row switch here is only to keep the visible
        table in sync with the clicked vertex, not required for correctness."""
        row_idx, col_idx = _grid_flat_to_rc(vi, self._row_offsets)
        self._row_combo.setCurrentIndex(row_idx)
        self._delete_column(col_idx)

    def _on_viewport_vertex_moved(self, vi: int, x: float, y: float, z: float):
        """Live update while Cmd+dragging or arrow-key-nudging a vertex
        marker in the editable viewport -- mirrors `_on_item_changed`'s
        self._grid + table + rebuild update, just driven by the viewport
        instead of a table-cell edit. `z` is ignored for 2D data (points
        are `[x, y]`). Switching to the dragged vertex's row (if
        different) repopulates the table from self._grid, which already
        reflects the new coordinates. `reframe=False`: a live move
        shouldn't re-fit/zoom the camera to the whole grid on every
        frame -- `scroll_to_visible` instead just pans (if needed) to
        keep this one vertex on-screen."""
        row_idx, col_idx = _grid_flat_to_rc(vi, self._row_offsets)
        pt = self._grid[row_idx][col_idx]
        pt[0] = x
        pt[1] = y
        is_3d = len(pt) > 2
        if is_3d:
            pt[2] = z
        if row_idx != self._row_combo.currentIndex():
            self._row_combo.setCurrentIndex(row_idx)
        else:
            self._vert_table.blockSignals(True)
            self._vert_table.item(col_idx, 0).setText(f"{x:g}")
            self._vert_table.item(col_idx, 1).setText(f"{y:g}")
            if is_3d:
                self._vert_table.item(col_idx, 2).setText(f"{z:g}")
            self._vert_table.blockSignals(False)
        self._rebuild(reframe=False)
        self._vp.scroll_to_visible(np.array([x, y, z if is_3d else 0.0]))

    def _wrap_flags(self) -> tuple[bool, bool]:
        wrap = self._wrap_combo.currentText()
        return wrap in ("Wrap Columns", "Wrap Both"), wrap in ("Wrap Rows", "Wrap Both")

    def _do_initial_load(self):
        mode = self._face_mode_combo.currentText()
        col_wrap, row_wrap = self._wrap_flags()
        self._vp.load_grid(self._grid,
                           col_wrap=col_wrap,
                           row_wrap=row_wrap,
                           draw_faces=(mode == "Grid Faces"),
                           bezier_patch=(mode == "Bezier Patch"))

    def _rebuild(self, _=None, reframe: bool = True):
        if self._vp._ctx is not None:
            mode = self._face_mode_combo.currentText()
            col_wrap, row_wrap = self._wrap_flags()
            self._vp.load_grid(self._grid,
                               col_wrap=col_wrap,
                               row_wrap=row_wrap,
                               draw_faces=(mode == "Grid Faces"),
                               bezier_patch=(mode == "Bezier Patch"),
                               reframe=reframe)

    def _on_item_changed(self, item: QTableWidgetItem):
        row_idx = self._row_combo.currentIndex()
        col_idx, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._grid[row_idx][col_idx][j]:g}")
            self._vert_table.blockSignals(False)
            return
        self._grid[row_idx][col_idx][j] = parsed
        self._vert_table.blockSignals(True)
        item.setText(f"{parsed:g}")
        self._vert_table.blockSignals(False)
        self._rebuild()

    def _on_save(self):
        self.committed.emit(_format_value(self._grid))
        self.accept()


class _GridViewport(Viewport):
    """Viewport for grid data with quad mesh faces and selectable vertex markers."""
    vertex_clicked = Signal(int)
    vertex_moved = Signal(int, float, float, float)  # (flat index, new_x, new_y, new_z) -- Cmd+drag, editable only
    delete_row_requested = Signal(int)  # (flat index) -- right-click a vertex, editable only
    delete_column_requested = Signal(int)  # (flat index) -- right-click a vertex, editable only

    def __init__(self, grid_value: list, is_2d: bool, parent=None, editable: bool = False):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        cam = self._renderer.camera
        cam.fov = 45.0
        self._renderer.depth_test_points = True
        self._renderer.show_edges = True  # enables polygon offset fill so skeleton lines render in front of faces
        self._editable = editable
        self._press_pos = None
        self._drag_started = False
        self._context_menu_suppressed = False   # set from mouseReleaseEvent -- a real drag shouldn't also pop a menu
        self._drag_vertex_idx = -1
        self._drag_lock_axis = 2
        self._drag_plane_point: np.ndarray = np.zeros(3, dtype=np.float32)
        self._all_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._bezier_patch_mode = False  # set from load_grid -- disables Add/Delete Row/Column via the viewport context menu
        self._grid_rows = len(grid_value)
        self._row_offsets = _grid_row_offsets(grid_value)
        self._is_2d = is_2d
        self._selected_indices: list[int] = []
        self._selected_row: int = -1
        self._blink_red = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(250)
        self._blink_timer.timeout.connect(self._blink_tick)
        self._sel_vao_r = None
        self._sel_vbo_r = None
        self._sel_vao_w = None
        self._sel_vbo_w = None
        # 2D data is always viewed locked top-down (no meaningful "other side"
        # to orbit to); dragging away from it just gimbal-locks/disorients
        # without adding anything, so orbit is disabled entirely for 2D.
        self._orbit_enabled = not is_2d
        if is_2d:
            cam.azimuth = 270.0
            # Not exactly 90: gimbal lock at precisely elevation=+-90 makes
            # _look_at fall back to an arbitrary +X "right" vector that
            # doesn't match the azimuth-dependent basis orbit-dragging
            # continuously converges to just off the pole -- see the
            # matching fix/comment in viewport.py's "top" view preset.
            cam.elevation = 89.9999
            cam.orthographic = True
        else:
            cam.orthographic = False

    def _blink_tick(self):
        self._blink_red = not self._blink_red
        self.update()

    def sync_row_bookkeeping(self, grid_value: list):
        """Refresh `_grid_rows`/`_row_offsets` from `grid_value` --
        independent of whether a GL context exists yet, unlike `load_grid`
        (which is a no-op before `initializeGL` has run, e.g. if called
        right after construction but before the dialog is first shown).
        `GridViewer`'s Add/Delete Row/Column handlers call this directly,
        right after mutating the grid, so `set_selected_row`/`_pick_vertex`
        never read stale offsets even if the GL-side rebuild is skipped."""
        self._grid_rows = len(grid_value)
        self._row_offsets = _grid_row_offsets(grid_value)

    def load_grid(self, grid_value: list, col_wrap: bool = False,
                  row_wrap: bool = False, draw_faces: bool = True,
                  bezier_patch: bool = False, reframe: bool = True):
        self._bezier_patch_mode = bezier_patch
        self.makeCurrent()
        self._renderer._clear_buffers()
        self._renderer.clear_simple_buffers()
        self._release_sel_markers()

        pts_3d = []
        for row in grid_value:
            for p in row:
                if len(p) == 2:
                    pts_3d.append([p[0], p[1], 0.0])
                else:
                    pts_3d.append([p[0], p[1], p[2]])
        pts = np.array(pts_3d, dtype=np.float32)
        self._all_pts = pts
        self.sync_row_bookkeeping(grid_value)
        rows, row_offsets = self._grid_rows, self._row_offsets
        row_lens = [len(row) for row in grid_value]

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_scene(bb_min, bb_max, reframe=reframe)

        r_range = rows if row_wrap else rows - 1

        # Row lines (red, within one row) and column lines (blue, between
        # adjacent rows) — always drawn in both modes. Rows can have
        # different lengths (a ragged/non-rectangular grid): row lines use
        # each row's own length independently, and column lines between a
        # pair of rows are limited to the columns the two rows actually share.
        # For a triangular grid (any two adjacent rows differ in length —
        # e.g. a cone's apex-to-base taper, or a triangular-number row
        # progression) a third, diagonal direction (green) is also drawn,
        # completing the triangulation implied by the row-length mismatch.
        row_color = np.array([0.85, 0.15, 0.15], dtype=np.float32)
        col_color = np.array([0.15, 0.45, 0.85], dtype=np.float32)
        diag_color = np.array([0.15, 0.75, 0.25], dtype=np.float32)
        is_triangular = _grid_is_triangular(row_lens, row_wrap)
        line_verts = []
        for r in range(rows):
            n = row_lens[r]
            if n < 2:
                continue
            c_range_row = n if col_wrap else n - 1
            base = row_offsets[r]
            for c in range(c_range_row):
                a = base + c
                b = base + (c + 1) % n
                line_verts.append(np.concatenate([pts[a], row_color]))
                line_verts.append(np.concatenate([pts[b], row_color]))
        for r in range(r_range):
            r_next = (r + 1) % rows
            len_a, len_b = row_lens[r], row_lens[r_next]
            shared = min(len_a, len_b)
            base_a, base_b = row_offsets[r], row_offsets[r_next]
            for c in range(shared):
                a = base_a + c
                b = base_b + c
                line_verts.append(np.concatenate([pts[a], col_color]))
                line_verts.append(np.concatenate([pts[b], col_color]))
            if is_triangular:
                if shared >= 2:
                    c_range_diag = shared if col_wrap else shared - 1
                    for c in range(c_range_diag):
                        a = base_a + c
                        b = base_b + (c + 1) % shared
                        line_verts.append(np.concatenate([pts[a], diag_color]))
                        line_verts.append(np.concatenate([pts[b], diag_color]))
                fan = _grid_fan_spec(len_a, len_b, col_wrap)
                if fan is not None:
                    anchor_in_a, anchor_col, longer_len, ks = fan
                    anchor_idx = (base_a if anchor_in_a else base_b) + anchor_col
                    longer_base = base_b if anchor_in_a else base_a
                    for k in ks:
                        spoke = longer_base + (k + 1) % longer_len
                        line_verts.append(np.concatenate([pts[anchor_idx], diag_color]))
                        line_verts.append(np.concatenate([pts[spoke], diag_color]))
        if line_verts:
            self._renderer.upload_lines(np.array(line_verts, dtype=np.float32))

        # Quad faces (faces mode only); polygon offset fill (from show_edges=True)
        # ensures the skeleton lines render in front of the mesh faces. Each
        # row pair contributes quads across the columns it shares with its
        # neighbour, plus a fan (`_grid_fan_spec`) covering any points beyond
        # that shared range — e.g. a cone's apex row fanning out to its base
        # row, or a triangular-number row's one extra point per step — so a
        # ragged grid's mesh has no missing/uncovered points instead of only
        # tapering down to whichever row is shorter.
        if draw_faces:
            tris_pos = []
            tris_norm = []

            def _add_tri(p0, p1, p2):
                n = np.cross(p1 - p0, p2 - p0)
                ln = np.linalg.norm(n)
                if ln > 0:
                    n = n / ln
                tris_pos.extend([p0, p1, p2])
                tris_norm.extend([n, n, n])

            for r in range(r_range):
                r_next = (r + 1) % rows
                len_a, len_b = row_lens[r], row_lens[r_next]
                shared = min(len_a, len_b)
                base_a, base_b = row_offsets[r], row_offsets[r_next]
                if shared >= 2:
                    c_range = shared if col_wrap else shared - 1
                    for c in range(c_range):
                        i00 = base_a + c
                        i01 = base_a + (c + 1) % shared
                        i10 = base_b + c
                        i11 = base_b + (c + 1) % shared
                        p00, p01, p10, p11 = pts[i00], pts[i01], pts[i10], pts[i11]
                        _add_tri(p00, p01, p11)
                        _add_tri(p00, p11, p10)
                fan = _grid_fan_spec(len_a, len_b, col_wrap)
                if fan is not None:
                    anchor_in_a, anchor_col, longer_len, ks = fan
                    anchor_idx = (base_a if anchor_in_a else base_b) + anchor_col
                    longer_base = base_b if anchor_in_a else base_a
                    p_anchor = pts[anchor_idx]
                    for k in ks:
                        p_k = pts[longer_base + k]
                        p_k1 = pts[longer_base + (k + 1) % longer_len]
                        if anchor_in_a:
                            _add_tri(p_k, p_anchor, p_k1)
                        else:
                            _add_tri(p_anchor, p_k, p_k1)
            if tris_pos:
                self._renderer.upload_mesh(np.array(tris_pos, dtype=np.float32),
                                 np.array(tris_norm, dtype=np.float32),
                                 backface_color=(0.9, 0.85, 0.1, 1.0))

        if bezier_patch and rows == 4 and row_lens == [4, 4, 4, 4]:
            cp = pts.reshape(4, 4, 3)
            tris_pos, tris_norm = _bezier_patch_mesh(cp)
            self._renderer.upload_mesh(tris_pos, tris_norm, backface_color=(0.9, 0.85, 0.1, 1.0))

        self._build_point_markers()
        if self._selected_indices:
            self._build_sel_markers()
        self.doneCurrent()
        self.update()

    def _cube_faces(self, r):
        return _cube_faces(r, self._is_2d)

    def _build_point_markers(self):
        self._renderer.clear_points()

        pts = self._all_pts
        if len(pts) == 0 or self._ctx is None:
            return

        unit_faces = self._cube_faces(1.0)
        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            r = _marker_radius_for_point(self, pt)
            for v0, v1, v2 in unit_faces:
                marker_tris.append(np.concatenate([pt + v0 * r, green]))
                marker_tris.append(np.concatenate([pt + v1 * r, green]))
                marker_tris.append(np.concatenate([pt + v2 * r, green]))

        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected_row(self, row_idx: int):
        self._selected_row = row_idx
        start, end = self._row_offsets[row_idx], self._row_offsets[row_idx + 1]
        self.set_selected(list(range(start, end)))

    def set_selected(self, indices: list[int]):
        self.makeCurrent()
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.doneCurrent()
            self.update()
            return

        if 0 <= indices[0] < len(self._all_pts):
            self.scroll_to_visible(self._all_pts[indices[0]])

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
        self.doneCurrent()
        self.update()

    def _release_sel_markers(self):
        for attr in ("_sel_vao_r", "_sel_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_sel_vbo_r", "_sel_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_sel_markers(self):
        self._release_sel_markers()
        if not self._selected_indices or self._ctx is None:
            return

        unit_faces = self._cube_faces(1.0)
        pts = self._all_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    r = _marker_radius_for_point(self, pt)
                    for v0, v1, v2 in unit_faces:
                        tris.append(np.concatenate([pt + v0 * r, color_val]))
                        tris.append(np.concatenate([pt + v1 * r, color_val]))
                        tris.append(np.concatenate([pt + v2 * r, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._gizmo_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        vao = self._sel_vao_r if self._blink_red else self._sel_vao_w
        if vao is not None:
            self._renderer._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            # Depth-tested (not disabled): markers must stay occluded by mesh
            # faces farther in front. Small polygon offset toward the camera
            # just breaks ties against coincident wireframe edges at the same
            # vertex position -- see SceneRenderer._render_simple_points.
            self._ctx.polygon_offset = (-1.0, -1.0)
            self._ctx.enable_direct(0x8037)  # GL_POLYGON_OFFSET_FILL
            vao.render(mgl.TRIANGLES)
            self._ctx.disable_direct(0x8037)
            self._ctx.polygon_offset = (0.0, 0.0)

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        # Always called from within an already-makeCurrent'd caller
        # (load_grid's own bracket) -- must NOT bracket with its own
        # makeCurrent/doneCurrent, since doneCurrent() would prematurely
        # release the context out from under load_grid's remaining work.
        super().frame_scene(bb_min, bb_max, reframe=reframe)
        if len(self._all_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        # Unlike frame_scene above, this is a genuine external Qt event --
        # never called from inside another makeCurrent'd block -- so it
        # does need its own bracket.
        super().wheelEvent(event)
        if len(self._all_pts) > 0:
            self.makeCurrent()
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
            self.doneCurrent()
            self.update()

    def closeEvent(self, event):
        self._blink_timer.stop()
        super().closeEvent(event)

    def _pick_vertex(self, px: float, py: float) -> int:
        if len(self._all_pts) == 0:
            return -1
        return self._renderer.pick_nearest_point(self._all_pts, px, py, self.width(), self.height())

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        self._drag_vertex_idx = -1
        # See _PathViewport.mousePressEvent -- a new press always starts a
        # fresh gesture, clearing any leftover suppression from the
        # *previous* one (contextMenuEvent fires on press on macOS, before
        # this press's own release could otherwise update the flag).
        self._context_menu_suppressed = False
        if (self._editable
                and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier   # Cmd on macOS
                and not (event.modifiers() & Qt.KeyboardModifier.AltModifier)):
            vi = self._pick_vertex(self._press_pos.x(), self._press_pos.y())
            if vi >= 0:
                self._drag_vertex_idx = vi
                if self._is_2d:
                    self._drag_lock_axis = 2
                    self._drag_plane_point = np.zeros(3, dtype=np.float32)
                else:
                    self._drag_lock_axis = _view_locked_axis(self._renderer.camera)
                    self._drag_plane_point = self._all_pts[vi].copy()
                self._show_delta(f"Plane: {_unlocked_plane_name(self._drag_lock_axis)}")
                return   # don't arm orbit/pan -- this press starts a vertex drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._drag_vertex_idx >= 0:
            pos = event.position().toPoint()
            ray_o, ray_d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
            hit = _ray_plane_axis_locked(ray_o, ray_d, self._drag_plane_point, self._drag_lock_axis)
            if hit is not None:
                self.vertex_moved.emit(self._drag_vertex_idx,
                                        round(float(hit[0]), 3), round(float(hit[1]), 3), round(float(hit[2]), 3))
            return
        if self._last_mouse is None:
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            if vi >= 0:
                pt = self._all_pts[vi]
                r, c = _grid_flat_to_rc(vi, self._row_offsets)
                coords = f"({pt[0]:g}, {pt[1]:g}" + (f", {pt[2]:g})" if not self._is_2d else ")")
                self.setToolTip(f"[{r},{c}]: {coords}")
            else:
                self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_vertex_idx >= 0:
            vi = self._drag_vertex_idx
            moved = self._drag_started
            self._context_menu_suppressed = moved
            self._drag_vertex_idx = -1
            self._press_pos = None
            self._drag_started = False
            self._delta_label.hide()
            if not moved:
                self.vertex_clicked.emit(vi)   # plain click on a vertex, no actual drag
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        # Captured here (a genuine right-button pan drag), not left as a
        # side effect of the reset below -- contextMenuEvent fires right
        # after this handler and needs to know whether *this* release
        # followed a drag, so a pan gesture doesn't also pop up a menu.
        self._context_menu_suppressed = self._drag_started
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """Right-clicking directly on a vertex (editable only, and not
        immediately after a right-button pan drag) offers "Delete Row" and
        "Delete Column" for that vertex's row/column. No menu at all in
        Bezier Patch mode (`_bezier_patch_mode`, set from `load_grid`) --
        deleting a row/column would break the exactly-4x4 shape a Bezier
        Patch's control net requires."""
        if not self._editable or self._context_menu_suppressed or self._bezier_patch_mode:
            return
        pos = event.pos()
        vi = self._pick_vertex(pos.x(), pos.y())
        if vi < 0:
            return
        # See _PathViewport.contextMenuEvent -- must only reset the base
        # Viewport's orbit/pan tracking once we're sure a menu is about to
        # show (right before exec()), not unconditionally at the top of
        # this method: on macOS contextMenuEvent fires on the right-button
        # *press*, so an unconditional reset here fired on every right
        # click, including blank-space ones that show no menu, breaking
        # ordinary right-drag panning.
        self._last_mouse = None
        self._mouse_button = None
        menu = QMenu(self)
        menu.addAction("Delete Row", lambda: self.delete_row_requested.emit(vi))
        menu.addAction("Delete Column", lambda: self.delete_column_requested.emit(vi))
        menu.exec(event.globalPos())

    def keyPressEvent(self, event):
        """Arrow keys nudge every selected vertex, confined to the same
        axis-locked plane Cmd+drag uses -- Z for 2D data (always top-down
        locked), whichever axis `_view_locked_axis` picks for the current
        camera angle otherwise. Step size (1 unit, or 0.1/10 with
        Cmd/Shift held) via `_key_nudge_magnitude` -- note Cmd here means
        the fine-nudge modifier, distinct from its other use starting a
        vertex *drag* on mouse-press."""
        if self._editable and self._selected_indices:
            lock_axis = 2 if self._is_2d else _view_locked_axis(self._renderer.camera)
            magnitude = _key_nudge_magnitude(event.modifiers())
            delta = _key_nudge_delta(self._renderer.camera, lock_axis, event.key(), magnitude)
            if delta is not None:
                for vi in self._selected_indices:
                    if 0 <= vi < len(self._all_pts):
                        new_pt = self._all_pts[vi] + delta
                        self.vertex_moved.emit(vi, round(float(new_pt[0]), 3),
                                                round(float(new_pt[1]), 3), round(float(new_pt[2]), 3))
                event.accept()
                return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Region Viewer
# ---------------------------------------------------------------------------

class RegionViewer(QDialog):
    """2D viewer for a "region" -- a list of closed polygon paths under
    even-odd fill semantics (a path nested inside another alternates
    solid/hole, e.g. three concentric circles = a disc surrounded by a
    ring). Modeled on `GridViewer`'s "index dropdown selects one sub-list"
    UI, combined with `PathViewer`'s per-path vertex editing (add/delete
    vertex, always-closed loop, 2D-only drag/nudge) -- since each path
    *is* an independently-closed polygon, unlike a grid's rows, which are
    connected to their neighbors. Read-only by default; pass
    `editable=True` for a Save/Cancel editing mode (see `MatrixViewer` for
    the shared editing convention)."""

    committed = Signal(str)

    def __init__(self, title: str, region_value: list, parent=None, editable: bool = False):
        super().__init__(parent)
        label = "Region Editor" if editable else "Region Viewer"
        self.setWindowTitle(f"{label}: {title}" if title else label)
        self.resize(900, 520)

        self._editable = editable
        self._region = region_value
        self._num_paths = len(region_value)
        self._path_offsets = _grid_row_offsets(region_value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _RegionViewport(region_value, self, editable=editable)
        splitter.addWidget(self._vp)

        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)

        path_bar = QHBoxLayout()
        path_bar.addWidget(QLabel("Path:"))
        self._path_combo = QComboBox()
        for i in range(self._num_paths):
            self._path_combo.addItem(str(i))
        self._path_combo.currentIndexChanged.connect(self._on_path_changed)
        path_bar.addWidget(self._path_combo, 1)
        tc_layout.addLayout(path_bar)

        self._pts_label = QLabel()
        tc_layout.addWidget(self._pts_label)

        self._vert_table = self._make_vert_table(region_value[0], editable)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        if editable:
            self._vert_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._vert_table.customContextMenuRequested.connect(self._show_vert_table_context_menu)
        tc_layout.addWidget(self._vert_table, 1)

        splitter.addWidget(table_container)
        t = self._vert_table
        fm = t.fontMetrics()
        max_path_len = max((len(p) for p in region_value), default=0)
        vh_w = max(fm.horizontalAdvance(str(max(max_path_len - 1, 0))),
                   fm.horizontalAdvance("0000")) + 20
        table_w = (vh_w
                   + sum(t.columnWidth(j) for j in range(t.columnCount()))
                   + t.frameWidth() * 2 + 2)
        splitter.setSizes([self.width() - table_w, table_w])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 0, 20, 0)
        self._fill_combo = QComboBox()
        self._fill_combo.addItem("Region Only")
        self._fill_combo.addItem("Region Filled")
        self._fill_combo.setCurrentIndex(1)
        self._fill_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        _size_combo_to_widest_item(self._fill_combo)
        self._fill_combo.currentIndexChanged.connect(self._rebuild)
        btn_row.addWidget(self._fill_combo)
        btn_row.addSpacing(20)
        btn_row.addStretch()
        if editable:
            cancel = QPushButton("Cancel")
            cancel.clicked.connect(self.reject)
            btn_row.addWidget(cancel)
            save = QPushButton("Save")
            save.clicked.connect(self._on_save)
            btn_row.addWidget(save)
        else:
            dismiss = QPushButton("Dismiss")
            dismiss.clicked.connect(self.close)
            btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._on_path_changed(0)
        self._vp.schedule_load(self._do_initial_load)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.add_path_requested.connect(self._on_viewport_add_path_requested)
            self._vp.add_vertex_requested.connect(self._on_viewport_add_vertex_requested)

    @staticmethod
    def _make_vert_table(path_pts: list, editable: bool = False) -> QTableWidget:
        t = QTableWidget(len(path_pts), 2)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["X", "Y"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(path_pts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if editable:
            t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                               | QAbstractItemView.EditTrigger.EditKeyPressed)
        else:
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, p in enumerate(path_pts):
            for j in range(2):
                item = QTableWidgetItem(f"{p[j]:g}")
                if not editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(2):
            t.setColumnWidth(j, min_w)
        return t

    def _populate_table(self, path_pts: list):
        self._vert_table.blockSignals(True)
        self._vert_table.setRowCount(len(path_pts))
        self._vert_table.setVerticalHeaderLabels([str(i) for i in range(len(path_pts))])
        for i, p in enumerate(path_pts):
            for j in range(2):
                item = QTableWidgetItem(f"{p[j]:g}")
                if not self._editable:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._vert_table.setItem(i, j, item)
        self._vert_table.blockSignals(False)

    def _on_path_changed(self, path_idx: int):
        if path_idx < 0 or path_idx >= self._num_paths:
            return
        path_pts = self._region[path_idx]
        self._pts_label.setText(f"Path Points ({len(path_pts)})")
        self._populate_table(path_pts)
        self._vert_table.selectAll()
        self._vp.set_selected_path(path_idx)

    def _refresh_path_bookkeeping(self):
        """Recompute self._num_paths/self._path_offsets from self._region
        -- call after any path/vertex-count change, before anything that
        indexes into the region via the cached offsets."""
        self._num_paths = len(self._region)
        self._path_offsets = _grid_row_offsets(self._region)

    def _refresh_path_combo_items(self):
        """Repopulate the path combo's 0..N-1 item labels -- call only
        when the path *count* itself changed (Add/Delete Path)."""
        self._path_combo.blockSignals(True)
        self._path_combo.clear()
        for i in range(self._num_paths):
            self._path_combo.addItem(str(i))
        self._path_combo.blockSignals(False)

    def _show_vert_table_context_menu(self, pos):
        """Right-click menu on the vertex table (`editable=True` only) --
        "this path" is always whichever path `_path_combo` currently
        shows; "this vertex" is the specific table row (a point index
        within that path) under the cursor, so the vertex-specific
        actions are hidden when the click lands on empty space below the
        last point. Unlike `GridViewer`'s Row/Column menu, there's no
        "Add Path" here at all -- see `_on_viewport_add_path_requested`
        for why that lives on the viewport's right-click instead."""
        vertex_idx = self._vert_table.rowAt(pos.y())
        menu = QMenu(self._vert_table)
        if vertex_idx >= 0:
            menu.addAction("Add Vertex Before", lambda: self._add_vertex(before=True, vertex_idx=vertex_idx))
            menu.addAction("Add Vertex After", lambda: self._add_vertex(before=False, vertex_idx=vertex_idx))
            menu.addSeparator()
        menu.addAction("Delete this Path", self._delete_path)
        if vertex_idx >= 0:
            menu.addAction("Delete this Vertex", lambda: self._delete_vertex(vertex_idx))
        menu.exec(self._vert_table.viewport().mapToGlobal(pos))

    def _on_viewport_add_path_requested(self, x: float, y: float):
        self._add_path_at(x, y)

    def _add_path_at(self, x: float, y: float):
        """Insert a new path -- a small rectangle centered at `(x, y)`,
        sized to look reasonable at the current zoom (reusing the same
        screen-space-constant scaling as vertex markers) -- at the end of
        the region. This is the *only* way to add a path: unlike Add Row/
        Column, a brand new path has no meaningful "before"/"after"
        position, so the natural place to add one is wherever the user
        right-clicked in the viewport (see `_RegionViewport.
        contextMenuEvent`), not a table-context-menu action."""
        half = _marker_radius_for_point(self._vp, np.array([x, y, 0.0])) * 8
        new_path = [[x - half, y - half], [x + half, y - half],
                    [x + half, y + half], [x - half, y + half]]
        self._region.append(new_path)
        insert_at = len(self._region) - 1
        self._vp.sync_path_bookkeeping(self._region)
        self._rebuild()
        self._refresh_path_bookkeeping()
        self._refresh_path_combo_items()
        self._path_combo.blockSignals(True)
        self._path_combo.setCurrentIndex(insert_at)
        self._path_combo.blockSignals(False)
        self._on_path_changed(insert_at)

    def _on_viewport_add_vertex_requested(self, path_idx: int, insert_after: int, x: float, y: float):
        """Right-click "Add Vertex" on a path's line (`_RegionViewport.
        contextMenuEvent`) -- inserts exactly at the clicked point on
        that segment, unlike `_add_vertex` (always a midpoint). Switches
        the path dropdown first if the clicked line belongs to a
        different path than the one currently displayed, same as
        `_on_viewport_vertex_clicked` does for a clicked vertex."""
        if path_idx != self._path_combo.currentIndex():
            self._path_combo.setCurrentIndex(path_idx)
        path = self._region[path_idx]
        path.insert(insert_after + 1, [x, y])
        self._vp.sync_path_bookkeeping(self._region)
        self._rebuild()
        self._refresh_path_bookkeeping()
        self._populate_table(path)
        self._pts_label.setText(f"Path Points ({len(path)})")
        self._vert_table.selectRow(insert_after + 1)
        self._vp.set_selected_path(path_idx)

    def _delete_path(self):
        """Remove the currently displayed path, refusing if that would
        leave 0 paths (`_is_region`'s minimum) -- silently, matching the
        no-popup convention used for invalid cell edits elsewhere."""
        if self._num_paths <= 1:
            return
        path_idx = self._path_combo.currentIndex()
        del self._region[path_idx]
        self._vp.sync_path_bookkeeping(self._region)
        self._rebuild()
        self._refresh_path_bookkeeping()
        self._refresh_path_combo_items()
        select = min(path_idx, self._num_paths - 1)
        self._path_combo.blockSignals(True)
        self._path_combo.setCurrentIndex(select)
        self._path_combo.blockSignals(False)
        self._on_path_changed(select)

    def _add_vertex(self, before: bool, vertex_idx: int):
        """Insert a new vertex before or after `vertex_idx` in the
        currently displayed path -- the midpoint of the two points it
        lands between. Every region path is an implicitly closed polygon
        (unlike `PathViewer`'s optional "Close Path"), so the neighbor is
        always found by wrapping (`% n`) -- no "no neighbor" case to
        handle, unlike `GridViewer`'s Add Row/Column."""
        path_idx = self._path_combo.currentIndex()
        path = self._region[path_idx]
        n = len(path)
        cur = path[vertex_idx]
        if before:
            neighbor_idx = (vertex_idx - 1) % n
            insert_at = vertex_idx
        else:
            neighbor_idx = (vertex_idx + 1) % n
            insert_at = vertex_idx + 1
        nxt = path[neighbor_idx]
        new_pt = [(a + b) / 2.0 for a, b in zip(cur, nxt)]
        path.insert(insert_at, new_pt)
        self._vp.sync_path_bookkeeping(self._region)
        self._rebuild()
        self._refresh_path_bookkeeping()
        self._populate_table(path)
        self._pts_label.setText(f"Path Points ({len(path)})")
        self._vert_table.selectRow(insert_at)
        self._vp.set_selected_path(path_idx)

    def _delete_vertex(self, vertex_idx: int):
        """Remove `vertex_idx` from the currently displayed path,
        refusing if that would leave fewer than the 3 points a polygon
        requires (`_is_region`'s minimum) -- silently, same convention as
        `_delete_path`."""
        path_idx = self._path_combo.currentIndex()
        path = self._region[path_idx]
        if len(path) - 1 < 3:
            return
        del path[vertex_idx]
        self._vp.sync_path_bookkeeping(self._region)
        self._rebuild()
        self._refresh_path_bookkeeping()
        self._populate_table(path)
        self._pts_label.setText(f"Path Points ({len(path)})")
        self._vert_table.clearSelection()
        self._vp.set_selected_path(path_idx)

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        col_indices = sorted(r.row() for r in rows)
        path_idx = self._path_combo.currentIndex()
        path_start = self._path_offsets[path_idx]
        global_indices = [path_start + c for c in col_indices]
        self._vp.set_selected(global_indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        if vi < 0:
            # Plain left-click on blank viewport space -- deselect
            # everything, rather than the previous no-op. Clearing the
            # table selection is enough: itemSelectionChanged already
            # drives _on_vert_table_selection -> _vp.set_selected([]),
            # which stops the blink timer and rebuilds markers as all-green.
            self._vert_table.clearSelection()
            return
        path_idx, col_idx = _grid_flat_to_rc(vi, self._path_offsets)
        if path_idx != self._path_combo.currentIndex():
            self._path_combo.setCurrentIndex(path_idx)
        self._vert_table.clearSelection()
        if 0 <= col_idx < self._vert_table.rowCount():
            self._vert_table.selectRow(col_idx)

    def _on_viewport_vertex_moved(self, vi: int, x: float, y: float, z: float):
        """Live update while Cmd+dragging or arrow-key-nudging a vertex
        marker in the editable viewport -- mirrors `_on_item_changed`'s
        self._region + table + rebuild update, just driven by the
        viewport instead of a table-cell edit. `z` is always ignored --
        regions are 2D-only. `reframe=False`: a live move shouldn't
        re-fit/zoom the camera to the whole region on every frame --
        `scroll_to_visible` instead just pans (if needed) to keep this
        one vertex on-screen."""
        path_idx, col_idx = _grid_flat_to_rc(vi, self._path_offsets)
        pt = self._region[path_idx][col_idx]
        pt[0] = x
        pt[1] = y
        if path_idx != self._path_combo.currentIndex():
            self._path_combo.setCurrentIndex(path_idx)
        else:
            self._vert_table.blockSignals(True)
            self._vert_table.item(col_idx, 0).setText(f"{x:g}")
            self._vert_table.item(col_idx, 1).setText(f"{y:g}")
            self._vert_table.blockSignals(False)
        self._rebuild(reframe=False)
        self._vp.scroll_to_visible(np.array([x, y, 0.0]))

    def _do_initial_load(self):
        self._vp.load_region(self._region, draw_fill=(self._fill_combo.currentText() == "Region Filled"))

    def _rebuild(self, _=None, reframe: bool = True):
        if self._vp._ctx is not None:
            self._vp.load_region(self._region, draw_fill=(self._fill_combo.currentText() == "Region Filled"),
                                 reframe=reframe)

    def _on_item_changed(self, item: QTableWidgetItem):
        path_idx = self._path_combo.currentIndex()
        col_idx, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._region[path_idx][col_idx][j]:g}")
            self._vert_table.blockSignals(False)
            return
        self._region[path_idx][col_idx][j] = parsed
        self._vert_table.blockSignals(True)
        item.setText(f"{parsed:g}")
        self._vert_table.blockSignals(False)
        self._rebuild()

    def _on_save(self):
        self.committed.emit(_format_value(self._region))
        self.accept()


_REGION_PATH_COLORS = [
    (0.85, 0.15, 0.15), (0.15, 0.45, 0.85), (0.15, 0.75, 0.25),
    (0.85, 0.55, 0.1), (0.6, 0.2, 0.8), (0.1, 0.7, 0.7),
]


class _RegionViewport(Viewport):
    """Viewport for region data -- always 2D/top-down-locked (no 3D mode,
    per BelfrySCAD's region semantics), each path drawn as its own closed
    line loop (cycling `_REGION_PATH_COLORS` so overlapping/nested paths
    stay visually distinguishable while editing), with an optional
    even-odd filled mesh (`_region_fill_mesh`) underneath."""

    vertex_clicked = Signal(int)
    vertex_moved = Signal(int, float, float, float)  # (flat index, new_x, new_y, new_z) -- Cmd+drag, editable only
    add_path_requested = Signal(float, float)  # (x, y) -- right-click blank space, editable only
    add_vertex_requested = Signal(int, int, float, float)  # (path_idx, insert_after_col_idx, x, y) -- right-click on a line, editable only

    def __init__(self, region_value: list, parent=None, editable: bool = False):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        cam = self._renderer.camera
        cam.fov = 45.0
        cam.azimuth = 270.0
        cam.elevation = 89.9999   # see _PathViewport's matching comment re: gimbal lock
        cam.orthographic = True
        self._orbit_enabled = False   # a region has no "other side" to orbit to -- always top-down
        self._renderer.line_width = 2.0
        self._renderer.show_edges = True   # polygon-offsets the fill mesh back so outlines win the depth tie
        self._editable = editable
        self._press_pos: QPoint | None = None
        self._drag_started = False
        self._drag_vertex_idx = -1
        self._context_menu_suppressed = False   # set from mouseReleaseEvent -- a real drag shouldn't also pop a menu
        self._all_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._num_paths = len(region_value)
        self._path_offsets = _grid_row_offsets(region_value)
        self._selected_indices: list[int] = []
        self._selected_path: int = -1
        self._blink_red = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(250)
        self._blink_timer.timeout.connect(self._blink_tick)
        self._sel_vao_r = None
        self._sel_vbo_r = None
        self._sel_vao_w = None
        self._sel_vbo_w = None

    def sync_path_bookkeeping(self, region_value: list):
        """Refresh `_num_paths`/`_path_offsets` from `region_value` --
        independent of whether a GL context exists yet, unlike
        `load_region` (a no-op before `initializeGL` has run). See
        `_GridViewport.sync_row_bookkeeping`, the identical fix for the
        identical staleness gap."""
        self._num_paths = len(region_value)
        self._path_offsets = _grid_row_offsets(region_value)

    def load_region(self, region_value: list, draw_fill: bool = True, reframe: bool = True):
        self.makeCurrent()
        self._renderer._clear_buffers()
        self._renderer.clear_simple_buffers()
        self._release_sel_markers()

        pts_3d = [[p[0], p[1], 0.0] for path in region_value for p in path]
        pts = np.array(pts_3d, dtype=np.float32)
        self._all_pts = pts
        self.sync_path_bookkeeping(region_value)

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_scene(bb_min, bb_max, reframe=reframe)

        line_verts = []
        for pi, path in enumerate(region_value):
            color = np.array(_REGION_PATH_COLORS[pi % len(_REGION_PATH_COLORS)], dtype=np.float32)
            n = len(path)
            base = self._path_offsets[pi]
            for c in range(n):
                a = base + c
                b = base + (c + 1) % n
                line_verts.append(np.concatenate([pts[a], color]))
                line_verts.append(np.concatenate([pts[b], color]))
        if line_verts:
            self._renderer.upload_lines(np.array(line_verts, dtype=np.float32))

        if draw_fill:
            fill = _region_fill_mesh(region_value)
            if fill is not None:
                tris_pos, tris_norm = fill
                self._renderer.upload_mesh(tris_pos, tris_norm, backface_color=(0.9, 0.85, 0.1, 1.0))

        self._build_point_markers()
        if self._selected_indices:
            self._build_sel_markers()
        self.doneCurrent()
        self.update()

    def _cube_faces(self, r):
        return _cube_faces(r, True)

    def _build_point_markers(self):
        self._renderer.clear_points()

        pts = self._all_pts
        if len(pts) == 0 or self._ctx is None:
            return

        unit_faces = self._cube_faces(1.0)
        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            r = _marker_radius_for_point(self, pt)
            for v0, v1, v2 in unit_faces:
                marker_tris.append(np.concatenate([pt + v0 * r, green]))
                marker_tris.append(np.concatenate([pt + v1 * r, green]))
                marker_tris.append(np.concatenate([pt + v2 * r, green]))

        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected_path(self, path_idx: int):
        self._selected_path = path_idx
        start, end = self._path_offsets[path_idx], self._path_offsets[path_idx + 1]
        self.set_selected(list(range(start, end)))

    def set_selected(self, indices: list[int]):
        self.makeCurrent()
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.doneCurrent()
            self.update()
            return

        if 0 <= indices[0] < len(self._all_pts):
            self.scroll_to_visible(self._all_pts[indices[0]])

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
        self.doneCurrent()
        self.update()

    def _release_sel_markers(self):
        for attr in ("_sel_vao_r", "_sel_vao_w"):
            vao = getattr(self, attr)
            if vao is not None:
                vao.release()
                setattr(self, attr, None)
        for attr in ("_sel_vbo_r", "_sel_vbo_w"):
            vbo = getattr(self, attr)
            if vbo is not None:
                vbo.release()
                setattr(self, attr, None)

    def _build_sel_markers(self):
        self._release_sel_markers()
        if not self._selected_indices or self._ctx is None:
            return

        unit_faces = self._cube_faces(1.0)
        pts = self._all_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    r = _marker_radius_for_point(self, pt)
                    for v0, v1, v2 in unit_faces:
                        tris.append(np.concatenate([pt + v0 * r, color_val]))
                        tris.append(np.concatenate([pt + v1 * r, color_val]))
                        tris.append(np.concatenate([pt + v2 * r, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._gizmo_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        vao = self._sel_vao_r if self._blink_red else self._sel_vao_w
        if vao is not None:
            self._renderer._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            self._ctx.disable(mgl.DEPTH_TEST)
            vao.render(mgl.TRIANGLES)
            self._ctx.enable(mgl.DEPTH_TEST)

    def frame_scene(self, bb_min, bb_max, reframe: bool = True):
        # Always called from within an already-makeCurrent'd caller
        # (load_region's own bracket, or the safe initial schedule_load
        # path) -- must NOT bracket with its own makeCurrent/doneCurrent.
        super().frame_scene(bb_min, bb_max, reframe=reframe)
        if len(self._all_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        # Unlike frame_scene above, this is a genuine external Qt event --
        # never called from inside another makeCurrent'd block -- so it
        # does need its own bracket.
        super().wheelEvent(event)
        if len(self._all_pts) > 0:
            self.makeCurrent()
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
            self.doneCurrent()
            self.update()

    def closeEvent(self, event):
        self._blink_timer.stop()
        super().closeEvent(event)

    def _blink_tick(self):
        self._blink_red = not self._blink_red
        self.update()

    def _pick_vertex(self, px: float, py: float) -> int:
        if len(self._all_pts) == 0:
            return -1
        return self._renderer.pick_nearest_point(self._all_pts, px, py, self.width(), self.height())

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        # See _PathViewport.mousePressEvent -- a new press always starts a
        # fresh gesture, clearing any leftover suppression from the
        # *previous* one (contextMenuEvent fires on press on macOS, before
        # this press's own release could otherwise update the flag).
        self._context_menu_suppressed = False
        if (self._editable
                and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier   # Cmd on macOS
                and not (event.modifiers() & Qt.KeyboardModifier.AltModifier)):
            vi = self._pick_vertex(self._press_pos.x(), self._press_pos.y())
            if vi >= 0:
                self._drag_vertex_idx = vi
                return   # don't arm orbit/pan -- this press starts a vertex drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
        if self._drag_vertex_idx >= 0:
            pos = event.position().toPoint()
            ray_o, ray_d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
            hit = _ray_plane_axis_locked(ray_o, ray_d, np.zeros(3, dtype=np.float32), 2)
            if hit is not None:
                self.vertex_moved.emit(self._drag_vertex_idx,
                                        round(float(hit[0]), 3), round(float(hit[1]), 3), 0.0)
            return
        if self._last_mouse is None:
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            if vi >= 0:
                pt = self._all_pts[vi]
                self.setToolTip(f"[{vi}]: ({pt[0]:g}, {pt[1]:g})")
            else:
                self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_vertex_idx >= 0:
            vi = self._drag_vertex_idx
            moved = self._drag_started
            self._context_menu_suppressed = moved
            self._drag_vertex_idx = -1
            self._press_pos = None
            self._drag_started = False
            if not moved:
                self.vertex_clicked.emit(vi)   # plain click on a vertex, no actual drag
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        # Captured here (a genuine right-button pan drag), not left as a
        # side effect of the reset below -- contextMenuEvent fires right
        # after this handler and needs to know whether *this* release
        # followed a drag, so a pan gesture doesn't also pop up a menu.
        self._context_menu_suppressed = self._drag_started
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        """Right-clicking the viewport (editable only, and only when not
        over an existing vertex, and only when the right button wasn't
        just used to pan the camera) offers one of two actions, checked
        in this order:
        - On a path's line: "Add Vertex", inserting exactly at the
          clicked point on that segment (every region path is implicitly
          closed, so the segment list always includes each path's own
          wrap-around edge -- see the loop below).
        - Otherwise, blank space: "Add Path" -- unlike Add Row/Column, a
          brand new path has no meaningful "before"/"after" position, so
          this is the only way to add one, placed wherever the user
          actually clicked rather than duplicating whatever path happens
          to be selected."""
        if not self._editable or self._context_menu_suppressed:
            return
        pos = event.pos()
        if self._pick_vertex(pos.x(), pos.y()) >= 0:
            return
        segments = []
        for p in range(self._num_paths):
            start, end = self._path_offsets[p], self._path_offsets[p + 1]
            n = end - start
            for k in range(n):
                segments.append((start + k, start + (k + 1) % n))
        seg_result = self._renderer.pick_nearest_segment(self._all_pts, segments, pos.x(), pos.y(), self.width(), self.height())
        if seg_result is not None:
            seg_idx, world_pt = seg_result
            a, _ = segments[seg_idx]
            path_idx, insert_after = _grid_flat_to_rc(a, self._path_offsets)
            x, y = float(world_pt[0]), float(world_pt[1])
            # See _PathViewport.contextMenuEvent -- must only reset the
            # base Viewport's orbit/pan tracking once we're sure a menu is
            # about to show (right before exec()), not unconditionally at
            # the top of this method (an earlier version of this fix did
            # that, which broke ordinary right-drag panning since
            # contextMenuEvent fires on the right-button *press* on
            # macOS -- before this method even knows whether a menu will
            # end up showing).
            self._last_mouse = None
            self._mouse_button = None
            menu = QMenu(self)
            menu.addAction("Add Vertex", lambda: self.add_vertex_requested.emit(path_idx, insert_after, x, y))
            menu.exec(event.globalPos())
            return
        ray_o, ray_d = self._renderer.camera_ray(pos.x(), pos.y(), self.width(), self.height())
        hit = _ray_plane_axis_locked(ray_o, ray_d, np.zeros(3, dtype=np.float32), 2)
        if hit is None:
            return
        x, y = float(hit[0]), float(hit[1])
        self._last_mouse = None
        self._mouse_button = None
        menu = QMenu(self)
        menu.addAction("Add Path", lambda: self.add_path_requested.emit(x, y))
        menu.exec(event.globalPos())

    def keyPressEvent(self, event):
        """Arrow keys nudge every selected vertex -- always the simple
        fixed X/Y mapping (regions are always the locked top-down 2D
        view, never orbited), see `_PathViewport.keyPressEvent`'s
        matching 2D case. Step size (1 unit, or 0.1/10 with Cmd/Shift
        held) via `_key_nudge_magnitude`."""
        if self._editable and self._selected_indices:
            magnitude = _key_nudge_magnitude(event.modifiers())
            delta = _key_nudge_delta(self._renderer.camera, 2, event.key(), magnitude)
            if delta is not None:
                for vi in self._selected_indices:
                    if 0 <= vi < len(self._all_pts):
                        new_pt = self._all_pts[vi] + delta
                        self.vertex_moved.emit(vi, round(float(new_pt[0]), 3),
                                                round(float(new_pt[1]), 3), 0.0)
                event.accept()
                return
        super().keyPressEvent(event)


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
        if "matrix" not in found and _is_matrix(value):
            found["matrix"] = (start, end, value)
        if "affine" not in found and _is_affine_matrix(value):
            found["affine"] = (start, end, value)
        if "vnf" not in found and _is_vnf(value):
            found["vnf"] = (start, end, value)
        if "region" not in found and _is_region(value):
            found["region"] = (start, end, value)
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
        if "grid" not in found and _is_grid(value):
            found["grid"] = (start, end, value)
        if "path" not in found and _is_path(value):
            found["path"] = (start, end, value)
        if "region" not in found and _is_region(value):
            found["region"] = (start, end, value)
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


def _open_path_viewer(title: str, value, parent=None):
    dlg = PathViewer(title, value, parent)
    dlg.show()


def _open_grid_viewer(title: str, value, parent=None):
    dlg = GridViewer(title, value, parent)
    dlg.show()


def _open_region_viewer(title: str, value, parent=None):
    dlg = RegionViewer(title, value, parent)
    dlg.show()


def _open_matrix_viewer(title: str, value, parent=None):
    dlg = MatrixViewer(title, value, parent)
    dlg.show()


def _open_affine_matrix_viewer(title: str, value, parent=None):
    dlg = AffineMatrixViewer(title, value, parent)
    dlg.show()


def build_viewer_menu(menu: QMenu, name: str, value, parent=None):
    """Add viewer actions to a QMenu based on the value's type."""
    if _is_list(value) or _is_oscobject(value):
        menu.addAction("View as List...", lambda: _open_list_viewer(name, value, parent))
    if _is_vnf(value):
        menu.addAction("View as VNF...", lambda: _open_vnf_viewer(name, value, parent))
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

    if "list" in literals:
        start, end, value = literals["list"]
        menu.addAction("View as List...", lambda start=start, end=end, value=value:
                       _open_list_viewer(_preview(start, end), value, parent))
    if "vnf" in literals:
        start, end, value = literals["vnf"]
        menu.addAction("View as VNF...", lambda start=start, end=end, value=value:
                       _open_vnf_viewer(_preview(start, end), value, parent))
    if "grid" in literals:
        start, end, value = literals["grid"]
        menu.addAction("View as Grid...", lambda start=start, end=end, value=value:
                       _open_grid_viewer(_preview(start, end), value, parent))
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
    if "region" in literals:
        start, end, value = literals["region"]
        menu.addAction("Edit as Region...", lambda start=start, end=end, value=value:
                       _open_region_editor(_preview(start, end), value,
                                            lambda t, s=start, e=end: on_commit(t, s, e), parent))
