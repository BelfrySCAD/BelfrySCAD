"""GridViewer: a 3D viewer for (possibly ragged) lists of lists of points."""
from __future__ import annotations

import copy

import numpy as np

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QAbstractItemView, QCheckBox, QMenu,
                               QLabel, QPushButton, QSplitter, QWidget, QComboBox,
                               QSpinBox)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QMouseEvent

from belfryscad import nurbs
from belfryscad.window.viewport import (Viewport, _key_nudge_delta,
                                        _key_nudge_magnitude, _view_locked_axis)
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin,
                                                  _apply_click_selection,
                                                  _dodecahedron_faces, _format_value,
                                                  _grid_flat_to_rc, _grid_row_offsets,
                                                  _is_list, _is_numeric_point,
                                                  _lit_marker_field,
                                                  _lit_marker_triangles,
                                                  _marker_radii_for_points,
                                                  _marker_radius_for_point,
                                                  _parse_number, _ray_plane_axis_locked,
                                                  _size_combo_to_widest_item,
                                                  _style_table_headers,
                                                  _sync_viewport_to_main_window,
                                                  _unlocked_plane_name,
                                                  _vertex_click_mode)


#: A heightfield's own nudge steps, keyed by what `_key_nudge_magnitude`
#: returns. Its 10/1/0.1 are world units, sized for geometry; a heightfield
#: is usually a 0..1 field, where a 1-unit step leaps past the entire range.
#: Same coarse/normal/fine relationship, sized for that range instead --
#: the coarse step covers it in a couple of presses, and the fine one still
#: shows in the table's three decimals.
_HEIGHT_NUDGE_STEPS = {10.0: 0.5, 1.0: 0.05, 0.1: 0.005}


def _is_grid(v) -> bool:
    """A grid is a list of >= 2 rows, each a non-empty list of points. Rows
    need not all be the same length — GridViewer supports ragged/non-
    rectangular grids (e.g. a cone's single-point apex row next to a
    multi-point base row)."""
    return (_is_list(v)
            and len(v) >= 2
            and all(_is_list(row) and len(row) >= 1
                    and all(_is_numeric_point(p) for p in row) for row in v))


def _quad_triangles(style: str, p00, p01, p10, p11, parity: int) -> list:
    """The triangles one grid quad becomes, as point triples.

    Ported from BOSL2's `vnf_vertex_array` (vnf.scad). Its corner names map
    to this grid's as i1=p00, i2=p10, i3=p11, i4=p01, so its two splits are
    the p00-p11 diagonal ("default") and the p01-p10 one ("alt"); every
    data-dependent style is a rule for choosing between exactly those two.
    `parity` is (row + col), which is all flip1/flip2 need.
    """
    diag_00_11 = [(p00, p11, p10), (p00, p01, p11)]
    diag_01_10 = [(p00, p01, p10), (p10, p01, p11)]

    if style == "alt":
        return diag_01_10
    if style == "flip1":
        return diag_01_10 if parity % 2 == 0 else diag_00_11
    if style == "flip2":
        return diag_01_10 if parity % 2 == 1 else diag_00_11
    if style in ("min_edge", "min_area", "convex", "concave"):
        if style == "min_edge":
            take_alt = np.linalg.norm(p01 - p10) < np.linalg.norm(p00 - p11)
        elif style == "min_area":
            area_alt = (np.linalg.norm(np.cross(p10 - p00, p01 - p00))
                        + np.linalg.norm(np.cross(p01 - p11, p10 - p11)))
            area_def = (np.linalg.norm(np.cross(p00 - p01, p11 - p01))
                        + np.linalg.norm(np.cross(p11 - p10, p00 - p10)))
            take_alt = area_alt < area_def
        else:
            # Normal of three corners; the fourth is above or below it.
            n = np.cross(p10 - p00, p11 - p00)
            if not np.any(n):
                return [(p00, p01, p11)]        # degenerate: one triangle
            above = float(n @ p01) > float(n @ p00)
            take_alt = above if style == "convex" else not above
        return diag_01_10 if take_alt else diag_00_11
    if style == "quincunx":
        # The only style that adds a vertex: the quad's centre, joined to
        # all four corners.
        mid = (p00 + p01 + p10 + p11) / 4.0
        return [(p00, mid, p10), (p10, mid, p11), (p11, mid, p01), (p01, mid, p00)]
    return diag_00_11                            # "default"


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
    return _surface_mesh(np.einsum('ia,jb,abk->ijk', basis, basis, cp))


def _surface_mesh(surface: np.ndarray, wrap_rows: bool = False,
                  wrap_cols: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Triangulate a sampled surface -- an (m, k, 3) grid of points -- two
    triangles per cell, joining the last row (column) back to the first
    when that direction wraps. Shared by the Bezier patch and the NURBS
    surfaces, which differ only in how they sample."""
    m, k = surface.shape[:2]
    tris_pos = []
    tris_norm = []
    for i in range(m if wrap_rows else m - 1):
        for j in range(k if wrap_cols else k - 1):
            i1, j1 = (i + 1) % m, (j + 1) % k
            p00, p01 = surface[i, j], surface[i, j1]
            p10, p11 = surface[i1, j], surface[i1, j1]
            for a, b, c in [(p00, p01, p11), (p00, p11, p10)]:
                n = np.cross(b - a, c - a)
                ln = np.linalg.norm(n)
                if ln > 0:
                    n = n / ln
                tris_pos.extend([a, b, c])
                tris_norm.extend([n, n, n])
    return np.array(tris_pos, dtype=np.float32), np.array(tris_norm, dtype=np.float32)


# ---------------------------------------------------------------------------
# Grid Viewer
# ---------------------------------------------------------------------------

class GridViewer(QDialog, _UndoableViewerMixin):
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
        _sync_viewport_to_main_window(self._vp)
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
        # Checked by default here, unlike VNFViewer's opt-in: these
        # shapes carry few enough points to draw them all, and
        # unchecking this is what clears the view.
        self._show_verts_cb = QCheckBox("Show Vertices")
        self._show_verts_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._show_verts_cb.setChecked(True)
        self._show_verts_cb.toggled.connect(self._vp.set_show_unselected)
        btn_row.addWidget(self._show_verts_cb)
        self._face_mode_combo = QComboBox()
        self._face_mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._face_mode_combo.currentIndexChanged.connect(self._rebuild)
        btn_row.addWidget(self._face_mode_combo)
        # NURBS degree in each direction, in BOSL2's terms: u runs down the
        # rows (the first index), v along each row.
        self._degree_label = QLabel("Degree u/v")
        self._degree_label.setStyleSheet("QLabel { padding-left: 12px; }")
        btn_row.addWidget(self._degree_label)
        self._degree_spins = []
        for tip in ("Degree in u, across the rows (the first index)",
                    "Degree in v, along each row"):
            spin = QSpinBox()
            spin.setValue(3)
            spin.setToolTip(tip)
            spin.valueChanged.connect(self._rebuild)
            btn_row.addWidget(spin)
            self._degree_spins.append(spin)
        self._sync_face_mode_combo()
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
        if editable:
            self._setup_undo(grid_value)
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

        self._on_row_changed(0)
        self._vp.schedule_load(self._do_initial_load)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.vertex_drag_started.connect(self._begin_live_edit)
            self._vp.vertex_drag_finished.connect(lambda: self._end_live_edit("Move Vertex"))
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
        """Offer exactly the face modes the grid's current shape supports,
        keeping the current one if it still applies (else Grid Faces), and
        cap the NURBS degrees at what the point counts allow. Rerun after
        every edit: adding or deleting a row or column changes all three."""
        rows = len(self._grid)
        lens = {len(row) for row in self._grid}
        modes = ["Grid Only", "Grid Faces"]
        if rows == 4 and lens == {4}:
            modes.append("Bezier Patch")
        if len(lens) == 1 and min(lens) >= 2:
            # BOSL2 wants a rectangular patch; any ragged row rules it out.
            modes += ["NURBS Surface", "NURBS Interpolated Surface"]
        current = self._face_mode_combo.currentText() or "Grid Faces"
        combo = self._face_mode_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(modes)
        combo.setCurrentText(current if current in modes else "Grid Faces")
        combo.blockSignals(False)
        _size_combo_to_widest_item(combo)
        nurbs_mode = combo.currentText().startswith("NURBS")
        self._degree_label.setVisible(nurbs_mode)
        for spin, count in zip(self._degree_spins, (rows, max(lens))):
            spin.setVisible(nurbs_mode)
            spin.blockSignals(True)
            spin.setRange(1, max(1, count - 1))     # BOSL2 needs degree+1 points
            spin.blockSignals(False)

    def _nurbs_surface(self):
        """The NURBS surface to draw, sampled as BOSL2's nurbs_patch_points
        would, or None: not a NURBS mode, or points BOSL2 would refuse (a
        duplicate neighbour, a singular fit). The control net stays drawn
        either way, so nothing vanishes."""
        mode = self._face_mode_combo.currentText()
        if not mode.startswith("NURBS"):
            return None
        grid = np.array([[[*p, 0.0][:3] for p in row] for row in self._grid], dtype=float)
        degree = tuple(spin.value() for spin in self._degree_spins)
        col_wrap, row_wrap = self._wrap_flags()
        closed = (row_wrap, col_wrap)      # u (rows) wraps with the rows
        try:
            if mode == "NURBS Interpolated Surface":
                control, knots = nurbs.interp_surface(grid, degree, closed)
                return nurbs.patch(control, degree, closed, knots=knots)
            return nurbs.patch(grid, degree, closed)
        except ValueError:
            return None

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
        new_grid = copy.deepcopy(self._grid)
        new_grid.insert(insert_at, new_row)
        self._commit_value(new_grid, "Add Row")
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
        new_grid = copy.deepcopy(self._grid)
        del new_grid[row_idx]
        self._commit_value(new_grid, "Delete Row")
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
        new_grid = copy.deepcopy(self._grid)
        for row in new_grid:
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
        self._commit_value(new_grid, "Add Column")
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
        new_grid = copy.deepcopy(self._grid)
        for row in new_grid:
            if col_idx < len(row):
                del row[col_idx]
        self._commit_value(new_grid, "Delete Column")
        self._on_row_changed(row_idx, select_all=False)

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        col_indices = sorted(r.row() for r in rows)
        row_idx = self._row_combo.currentIndex()
        row_start = self._row_offsets[row_idx]
        global_indices = [row_start + c for c in col_indices]
        self._vp.set_selected(global_indices)

    def _on_viewport_vertex_clicked(self, vi: int, mode: str):
        if vi < 0:
            _apply_click_selection(self._vert_table, -1, mode)
            return
        row_idx, col_idx = _grid_flat_to_rc(vi, self._row_offsets)
        if row_idx != self._row_combo.currentIndex():
            # The table only ever shows one row's vertices at a time --
            # switching rows repopulates it, so whatever was selected
            # belonged to a different row and no longer exists. "add"/
            # "toggle" can't mean anything across that boundary; fall back
            # to selecting just the clicked vertex in its own row.
            self._row_combo.setCurrentIndex(row_idx)
            mode = "replace"
        _apply_click_selection(self._vert_table, col_idx, mode)

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
                           bezier_patch=(mode == "Bezier Patch"),
                           surface=self._nurbs_surface())

    def _rebuild(self, _=None, reframe: bool = True):
        self._sync_face_mode_combo()
        if self._vp._ctx is not None:
            mode = self._face_mode_combo.currentText()
            col_wrap, row_wrap = self._wrap_flags()
            self._vp.load_grid(self._grid,
                               col_wrap=col_wrap,
                               row_wrap=row_wrap,
                               draw_faces=(mode == "Grid Faces"),
                               bezier_patch=(mode == "Bezier Patch"),
                               reframe=reframe,
                               surface=self._nurbs_surface())

    def _get_value(self):
        return self._grid

    def _apply_value(self, value):
        self._grid = value
        self._sync_face_mode_combo()
        self._vp.sync_row_bookkeeping(self._grid)
        self._refresh_row_bookkeeping()
        self._refresh_row_combo_items()
        row_idx = max(0, min(self._row_combo.currentIndex(), self._rows - 1))
        self._row_combo.blockSignals(True)
        self._row_combo.setCurrentIndex(row_idx)
        self._row_combo.blockSignals(False)
        self._on_row_changed(row_idx, select_all=False)
        self._rebuild()

    def _on_item_changed(self, item: QTableWidgetItem):
        row_idx = self._row_combo.currentIndex()
        col_idx, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._grid[row_idx][col_idx][j]:g}")
            self._vert_table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._grid)
        new_value[row_idx][col_idx][j] = parsed
        self._commit_value(new_value, "Edit Vertex")

    def _on_save(self):
        self.committed.emit(_format_value(self._grid))
        self.accept()


class _GridViewport(Viewport):
    """Viewport for grid data with quad mesh faces and selectable vertex markers."""
    vertex_clicked = Signal(int, str)  # (flat vertex index, click mode: "replace"/"add"/"toggle" -- see _vertex_click_mode)
    vertex_moved = Signal(int, float, float, float)  # (flat index, new_x, new_y, new_z) -- Cmd+drag, editable only
    # Bracket a continuous vertex_moved sequence (a Cmd+drag gesture, or one
    # keyboard nudge) so the dialog can push exactly one undo step for the
    # whole gesture instead of one per live frame.
    vertex_drag_started = Signal()
    vertex_drag_finished = Signal()
    delete_row_requested = Signal(int)  # (flat index) -- right-click a vertex, editable only
    delete_column_requested = Signal(int)  # (flat index) -- right-click a vertex, editable only

    def __init__(self, grid_value: list, is_2d: bool, parent=None, editable: bool = False):
        super().__init__(parent, selectable=False, pan_speed=2.0)
        #: How each quad splits into triangles -- see `_quad_triangles`.
        self.quad_style = "default"
        #: When set, only these vertex indices get a marker. A heightfield
        #: showing its tiling draws the neighbouring copies for context but
        #: only the middle one is editable, and a marker on a point that
        #: cannot be grabbed is an invitation to try.
        self.marker_indices = None
        #: Draw the row/column/diagonal skeleton over the surface. It is
        #: the whole subject of a control-point grid, but on a heightfield
        #: it only repeats what the edges view already shows -- and with
        #: edges off it has no depth offset to lift it clear, so it
        #: z-fights the surface it lies on and speckles through as a
        #: coloured grid over what should be a clean shape.
        self.show_skeleton = True
        #: World-Z per unit of stored height, so a z_only nudge can be a
        #: step in the value the user is editing rather than in the
        #: exaggerated preview. Kept in step with the Z scale by the
        #: dialog; 1.0 means the two are the same thing.
        self.z_nudge_scale = 1.0
        #: Constrain editing to height alone: a heightfield stores one
        #: number per cell, and x/y ARE the cell's position in the array,
        #: so a moved x or y is not something it can express.
        self.z_only = False
        cam = self._renderer.camera
        cam.fov = 45.0
        self._renderer.depth_test_points = True
        self._renderer.show_edges = True  # enables polygon offset fill so skeleton lines render in front of faces
        self._editable = editable
        # Vertex markers start on here, unlike _VNFViewport where they are
        # opt-in: a path/grid/region carries few enough points to draw them
        # all by default, but they still hide the shape underneath, so the
        # viewer offers the same "Show Vertices" toggle to clear the view.
        self._show_unselected = True
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

    def _emit_moved(self, vi: int, pt) -> None:
        """Emit a moved vertex.

        Rounded to three decimals for geometry, where the number IS the
        coordinate and a tidy one is worth having. Not rounded at all in
        z_only mode: there, world Z is the stored height times the preview
        exaggeration, so rounding here quantises the user's data by an
        amount that depends on the Z scale -- at 25% a 0.001 step became
        0.0008, and a finer one vanished entirely. The heightfield dialog
        divides the exaggeration back out and rounds the stored value
        itself, which is the number that actually needs to be tidy.
        """
        if self.z_only:
            self.vertex_moved.emit(vi, float(pt[0]), float(pt[1]), float(pt[2]))
            return
        self.vertex_moved.emit(vi, round(float(pt[0]), 3),
                               round(float(pt[1]), 3), round(float(pt[2]), 3))

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
                  bezier_patch: bool = False, reframe: bool = True,
                  surface: np.ndarray | None = None):
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
        if line_verts and self.show_skeleton:
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
                        for t0, t1, t2 in _quad_triangles(self.quad_style,
                                                           p00, p01, p10, p11, r + c):
                            _add_tri(t0, t1, t2)
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

        if surface is not None:
            # A NURBS surface, sampled by the dialog (see GridViewer._nurbs_surface).
            tris_pos, tris_norm = _surface_mesh(surface, row_wrap, col_wrap)
            if len(tris_pos):
                self._renderer.upload_mesh(tris_pos, tris_norm, backface_color=(0.9, 0.85, 0.1, 1.0))

        self._build_point_markers()
        if self._selected_indices:
            self._build_sel_markers()
        self.doneCurrent()
        self.update()

    def _marker_faces(self, r):
        return _dodecahedron_faces(r, self._is_2d)

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

        pts = self._all_pts
        if len(pts) == 0 or self._ctx is None:
            return

        unit_faces = self._marker_faces(1.0)
        unselected_color = np.array(self._renderer.unselected_vertex_color[:3], dtype=np.float32)
        selected = set(self._selected_indices)

        allowed = self.marker_indices
        keep = [i for i in range(len(pts))
                if i not in selected and (allowed is None or i in allowed)]
        if keep:
            shown = np.asarray(pts, dtype=np.float32)[keep]
            self._renderer.upload_points(_lit_marker_field(
                shown, _marker_radii_for_points(self, shown), unit_faces,
                unselected_color))

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

        unit_faces = self._marker_faces(1.0)
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

    def _on_zoom_changed(self):
        # Screen-space marker sizes depend on camera distance/FOV, so they
        # have to be rebuilt whenever those change -- by wheel OR by a
        # pinch gesture, which never reaches wheelEvent. Hooking the base
        # class's notification covers both. Unlike frame_scene above, this
        # runs from a genuine external Qt event, never from inside another
        # makeCurrent'd block, so it does need its own bracket.
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
                elif self.z_only:
                    # Never lock Z, or the drag plane would be horizontal
                    # and height could not move at all. Lock whichever
                    # horizontal axis faces the camera most squarely, so
                    # the plane that is left always contains Z.
                    cam = self._renderer.camera
                    fwd = cam.target - cam.eye_position()
                    self._drag_lock_axis = 0 if abs(fwd[0]) >= abs(fwd[1]) else 1
                    self._drag_plane_point = self._all_pts[vi].copy()
                else:
                    self._drag_lock_axis = _view_locked_axis(self._renderer.camera)
                    self._drag_plane_point = self._all_pts[vi].copy()
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
                if self.z_only:
                    # Height follows the cursor; the cell keeps its place.
                    start_pt = self._all_pts[self._drag_vertex_idx]
                    hit = np.array([start_pt[0], start_pt[1], hit[2]], dtype=np.float32)
                self._emit_moved(self._drag_vertex_idx, hit)
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
            if self.z_only:
                # Up/down raise and lower. Left/right would mean moving a
                # cell sideways, which a heightfield cannot represent, so
                # they fall through to the viewport's own key handling.
                #
                # Steps of the STORED height, not of world space: a
                # heightfield is usually a 0..1 field, where the shared
                # 10/1/0.1 would leap clean past the whole range. Scaling
                # by z_nudge_scale also cancels the exaggeration the
                # writeback divides out, so the step is the same 0.1/0.01/
                # 0.001 at every Z scale rather than magnitude/scale.
                stored = _HEIGHT_NUDGE_STEPS[magnitude] * self.z_nudge_scale
                step = {Qt.Key.Key_Up: stored, Qt.Key.Key_Down: -stored}.get(event.key())
                delta = None if step is None else np.array([0.0, 0.0, step], dtype=np.float32)
            else:
                delta = _key_nudge_delta(self._renderer.camera, lock_axis, event.key(), magnitude)
            if delta is not None:
                self.vertex_drag_started.emit()
                for vi in self._selected_indices:
                    if 0 <= vi < len(self._all_pts):
                        new_pt = self._all_pts[vi] + delta
                        self._emit_moved(vi, new_pt)
                self.vertex_drag_finished.emit()
                event.accept()
                return
        super().keyPressEvent(event)
