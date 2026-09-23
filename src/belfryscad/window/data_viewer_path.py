"""PathViewer: a 2D/3D path viewer and editor -- straight, Bezier and NURBS."""
from __future__ import annotations

import copy

import numpy as np

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QSpinBox, QAbstractItemView, QCheckBox,
                               QMenu, QLabel, QPushButton, QSplitter, QWidget,
                               QComboBox)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QMouseEvent

from belfryscad import nurbs
from belfryscad.window.viewport import (Viewport, _key_nudge_delta,
                                        _key_nudge_magnitude, _view_locked_axis)
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin,
                                                  _apply_click_selection,
                                                  _dodecahedron_faces, _format_value,
                                                  _is_list, _is_numeric_point,
                                                  _lit_marker_triangles,
                                                  _marker_radius_for_point,
                                                  _parse_number, _ray_plane_axis_locked,
                                                  _size_combo_to_widest_item,
                                                  _style_table_headers,
                                                  _sync_viewport_to_main_window,
                                                  _unlocked_plane_name,
                                                  _vertex_click_mode)


def _is_path(v) -> bool:
    return (_is_list(v)
            and len(v) >= 2
            and all(_is_numeric_point(p) for p in v))


def _diamond_faces(r: float, is_2d: bool) -> list:
    """Triangle triples forming a diamond point marker of half-width `r`
    -- a rotated square (rhombus, points N/E/S/W) in the XY plane when
    `is_2d`, an octahedron (points along +-X/+-Y/+-Z) otherwise. Used for
    `_PathViewport`'s bezier "same_angle" node-type marker -- same
    offset-vector convention as `_dodecahedron_faces`, just a different
    silhouette so it's visually distinct at marker size."""
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
    marker -- same offset-vector convention as `_dodecahedron_faces`."""
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


class PathViewer(QDialog, _UndoableViewerMixin):
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
        _sync_viewport_to_main_window(self._vp)
        splitter.addWidget(self._vp)

        self._vert_table = self._make_vert_table(path_value, self._is_2d, editable)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        if editable:
            self._setup_undo(path_value)
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.vertex_drag_started.connect(self._begin_live_edit)
            self._vp.vertex_drag_finished.connect(lambda: self._end_live_edit("Move Vertex"))
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
        # Checked by default here, unlike VNFViewer's opt-in: these
        # shapes carry few enough points to draw them all, and
        # unchecking this is what clears the view.
        self._show_verts_cb = QCheckBox("Show Vertices")
        self._show_verts_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._show_verts_cb.setChecked(True)
        self._show_verts_cb.toggled.connect(self._vp.set_show_unselected)
        btn_row.addWidget(self._show_verts_cb)
        self._closed_cb = QCheckBox("Close Path")
        self._closed_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._closed_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._closed_cb)
        # How the points are read: a plain polyline, cubic Bezier segments,
        # NURBS control points, or points a NURBS interpolates. View-only,
        # like Close Path -- the literal is the same list of points in all
        # four, so the mode is never written back.
        self._mode_combo = QComboBox()
        for label, key in (("Path", "path"), ("Bezier Path", "bezier"),
                           ("NURBS Path", "nurbs"),
                           ("NURBS Interpolated Path", "nurbs_interp")):
            self._mode_combo.addItem(label, key)
        self._mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        _size_combo_to_widest_item(self._mode_combo)
        self._mode_combo.currentIndexChanged.connect(self._rebuild)
        btn_row.addWidget(self._mode_combo)
        self._degree_label = QLabel("Degree")
        self._degree_label.setStyleSheet("QLabel { padding-left: 12px; }")
        btn_row.addWidget(self._degree_label)
        self._degree_spin = QSpinBox()
        self._degree_spin.setValue(3)
        self._degree_spin.valueChanged.connect(self._rebuild)
        btn_row.addWidget(self._degree_spin)
        self._sync_degree_spin()
        if editable:
            btn_row.addLayout(self._make_reset_button_row())
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
        if (self._mode() == "bezier" and len(rows) == 1
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
        new_path = copy.deepcopy(self._path)
        for r in rows:
            del new_path[r]
        self._commit_value(new_path, "Delete Vertex")
        self._vert_table.clearSelection()

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
        self._commit_value(new_path, "Delete Vertex")
        self._vert_table.clearSelection()
        for old_idx in reclassify_old:
            new_idx = index_map.get(old_idx)
            if new_idx is not None:
                self._vp.classify_single_node(new_idx)
        self._vp.refresh_markers()

    def _get_value(self):
        return self._path

    def _apply_value(self, value):
        self._path = value
        self._populate_vert_table()
        self._rebuild()

    def _on_item_changed(self, item: QTableWidgetItem):
        i, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._path[i][j]:g}")
            self._vert_table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._path)
        new_value[i][j] = parsed
        self._commit_value(new_value, "Edit Vertex")

    def _on_save(self):
        self.committed.emit(_format_value(self._path))
        self.accept()

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        indices = sorted(r.row() for r in rows)
        self._vp.set_selected(indices)

    def _on_viewport_vertex_clicked(self, vi: int, mode: str):
        _apply_click_selection(self._vert_table, vi, mode)

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
        new_path = copy.deepcopy(self._path)
        new_path.insert(insert_after + 1, new_pt)
        self._commit_value(new_path, "Add Vertex")
        self._vert_table.selectRow(insert_after + 1)

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
        new_path = copy.deepcopy(self._path)
        new_path[i0 + 1:i0 + 3] = pts_as_lists
        self._commit_value(new_path, "Add Vertex")
        self._vert_table.selectRow(i0 + 3)
        self._vp.classify_single_node(i0 + 3)
        self._vp.refresh_markers()

    def _mode(self) -> str:
        return self._mode_combo.currentData()

    def _sync_degree_spin(self):
        """Show the degree only in the NURBS modes, and cap it at what the
        point count supports (BOSL2 asserts degree+1 points; interpolation
        also needs degree >= 2), so the curve never silently degrades to
        the straight-line fallback just because a point was deleted."""
        mode = self._mode()
        nurbs = mode in ("nurbs", "nurbs_interp")
        self._degree_label.setVisible(nurbs)
        self._degree_spin.setVisible(nurbs)
        low = 2 if mode == "nurbs_interp" else 1
        self._degree_spin.blockSignals(True)
        self._degree_spin.setRange(low, max(low, len(self._path) - 1))
        self._degree_spin.blockSignals(False)

    def _do_initial_load(self):
        self._vp.load_path(self._path, self._closed_cb.isChecked(),
                           self._mode(), self._degree_spin.value())

    def _rebuild(self, _=None, reframe: bool = True):
        self._sync_degree_spin()
        if self._vp._ctx is not None:
            self._vp.load_path(self._path, self._closed_cb.isChecked(),
                               self._mode(), self._degree_spin.value(), reframe=reframe)

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
# the plain dodecahedron look; the other two get a distinct silhouette so a
# node's type is visible at a glance. Module-level (not a class attribute)
# so the names here unambiguously resolve to these free functions, not to
# any same-named instance method defined later in _PathViewport's class body.
_NODE_TYPE_SHAPE_FN = {
    "disjointed": _dodecahedron_faces,
    "same_angle": _diamond_faces,
    "symmetric": _triangle_faces,
}


class _PathViewport(Viewport):
    """Viewport subclass with selectable vertex markers and hover tooltips."""
    vertex_clicked = Signal(int, str)  # (vertex index, click mode: "replace"/"add"/"toggle" -- see _vertex_click_mode)
    vertex_moved = Signal(int, float, float, float)  # (index, new_x, new_y, new_z) -- Cmd+drag, editable only
    # Bracket a continuous vertex_moved sequence (a Cmd+drag gesture, or one
    # keyboard nudge) so the dialog can push exactly one undo step for the
    # whole gesture instead of one per live frame -- see
    # PathViewer._begin_live_edit/_end_live_edit.
    vertex_drag_started = Signal()
    vertex_drag_finished = Signal()
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
        # Vertex markers start on here, unlike _VNFViewport where they are
        # opt-in: a path/grid/region carries few enough points to draw them
        # all by default, but they still hide the shape underneath, so the
        # viewer offers the same "Show Vertices" toggle to clear the view.
        self._show_unselected = True
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

    def load_path(self, path_value: list, closed: bool, mode: str = "path",
                  degree: int = 3, reframe: bool = True):
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
        bezier = mode == "bezier"
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
        elif mode in ("nurbs", "nurbs_interp") and (curve := self._nurbs_curve(pts, closed, mode, degree)) is not None:
            if closed:
                curve = np.vstack([curve, curve[:1]])
            line_data = np.empty((2 * (len(curve) - 1), 6), dtype=np.float32)
            line_data[0::2, :3] = curve[:-1]
            line_data[1::2, :3] = curve[1:]
            line_data[:, 3:] = line_color
            self._renderer.upload_lines(line_data)
            if mode == "nurbs":
                # The control polygon, lighter than the curve it shapes.
                ring = np.vstack([pts, pts[:1]]) if closed else pts
                hdata = np.empty((2 * (len(ring) - 1), 6), dtype=np.float32)
                hdata[0::2, :3] = ring[:-1]
                hdata[1::2, :3] = ring[1:]
                hdata[:, 3:] = 0.6
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

    @staticmethod
    def _nurbs_curve(pts: np.ndarray, closed: bool, mode: str, degree: int):
        """Sampled NURBS curve for the NURBS modes, or None when BOSL2
        would refuse these points (too few for the degree, duplicate
        neighbours, a singular fit) -- the caller then draws the plain
        polyline, so the points never vanish from view."""
        dim = 3 if np.any(pts[:, 2]) else 2
        try:
            if mode == "nurbs_interp":
                control, knots = nurbs.interp(pts[:, :dim], degree, closed)
                curve = nurbs.curve(control, degree, closed, knots=knots)
            else:
                curve = nurbs.curve(pts[:, :dim], degree, closed)
        except ValueError:
            return None
        if dim == 2:
            curve = np.column_stack([curve, np.zeros(len(curve))])
        return curve.astype(np.float32)

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

    def _marker_faces(self, r):
        return _dodecahedron_faces(r, self._is_2d)

    def _unit_faces_for_point(self, idx: int) -> list:
        if self._bezier and idx % 3 == 0:
            shape_fn = _NODE_TYPE_SHAPE_FN.get(
                self._node_types.get(idx, "disjointed"), _dodecahedron_faces)
            return shape_fn(1.0, self._is_2d)
        return self._marker_faces(1.0)

    def set_show_unselected(self, enabled: bool):
        """Show or hide the markers on every point that isn't selected.

        Selected points keep their own blinking markers regardless -- the
        toggle exists to get the indicators out of the way of the shape
        they sit on, not to lose track of what you picked. Same name and
        behaviour as `_VNFViewport.set_show_unselected`.
        """
        self._show_unselected = enabled
        self.makeCurrent()
        self._build_point_markers()
        self.doneCurrent()
        self.update()

    def _build_point_markers(self):
        self._renderer.clear_points()
        if not self._show_unselected:
            return

        pts = self._path_pts
        if len(pts) == 0 or self._ctx is None:
            return

        unselected_color = np.array(self._renderer.unselected_vertex_color[:3], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            r = _marker_radius_for_point(self, pt)
            marker_tris.extend(_lit_marker_triangles(pt, r, self._unit_faces_for_point(i), unselected_color))

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

        unit_faces = self._marker_faces(1.0)
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
                    tris.extend(_lit_marker_triangles(pt, r, unit_faces, color_val))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._marker_prog,
                    [(vbo, "3f 3f 3f", "in_position", "in_normal", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        vao = self._sel_vao_r if self._blink_red else self._sel_vao_w
        if vao is not None:
            cam = self._renderer.camera
            view = cam.view_matrix()
            light = np.array([0.6, 0.8, 1.0], dtype=np.float32)
            light /= np.linalg.norm(light)
            L_world = (view[:3, :3].T @ light).astype(np.float32)
            L_world /= np.linalg.norm(L_world)
            marker_prog = self._renderer._marker_prog
            marker_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            marker_prog["light_dir"].value = tuple(L_world)
            marker_prog["eye_pos"].value = tuple(cam.eye_position())
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

    def _on_zoom_changed(self):
        # Screen-space marker sizes depend on camera distance/FOV, so they
        # have to be rebuilt whenever those change -- by wheel OR by a
        # pinch gesture, which never reaches wheelEvent. Hooking the base
        # class's notification covers both. Unlike frame_scene above, this
        # runs from a genuine external Qt event, never from inside another
        # makeCurrent'd block, so it does need its own bracket.
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
                self.vertex_drag_started.emit()
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
            self.vertex_drag_finished.emit()
            if not moved:
                # Cmd was held at press time (the only way _drag_vertex_idx
                # gets armed) but nothing actually moved -- toggle, not a
                # plain click.
                self.vertex_clicked.emit(vi, "toggle")
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi, _vertex_click_mode(event.modifiers()))
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
                self.vertex_drag_started.emit()
                for vi in self._selected_indices:
                    if 0 <= vi < len(self._path_pts):
                        new_pt = self._path_pts[vi] + delta
                        for idx, moved_pt in self._resolve_bezier_moves(vi, new_pt):
                            self.vertex_moved.emit(idx, round(float(moved_pt[0]), 3),
                                                    round(float(moved_pt[1]), 3), round(float(moved_pt[2]), 3))
                self.vertex_drag_finished.emit()
                event.accept()
                return
        super().keyPressEvent(event)
