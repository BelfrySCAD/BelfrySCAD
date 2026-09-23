"""RegionViewer: a list of closed polygon paths with even-odd fill -- nested
paths alternate solid and hole."""
from __future__ import annotations

import copy

import numpy as np
import manifold3d as m3d

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QAbstractItemView, QCheckBox, QMenu,
                               QLabel, QPushButton, QSplitter, QWidget, QComboBox)
from PySide6.QtCore import Qt, QPoint, Signal, QTimer
from PySide6.QtGui import QFont, QMouseEvent

from belfryscad.window.viewport import Viewport, _key_nudge_delta, _key_nudge_magnitude
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
                                                  _vertex_click_mode)


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


class RegionViewer(QDialog, _UndoableViewerMixin):
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
        _sync_viewport_to_main_window(self._vp)
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
        # Checked by default here, unlike VNFViewer's opt-in: these
        # shapes carry few enough points to draw them all, and
        # unchecking this is what clears the view.
        self._show_verts_cb = QCheckBox("Show Vertices")
        self._show_verts_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._show_verts_cb.setChecked(True)
        self._show_verts_cb.toggled.connect(self._vp.set_show_unselected)
        btn_row.addWidget(self._show_verts_cb)
        self._fill_combo = QComboBox()
        self._fill_combo.addItem("Region Only")
        self._fill_combo.addItem("Region Filled")
        self._fill_combo.setCurrentIndex(1)
        self._fill_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        _size_combo_to_widest_item(self._fill_combo)
        self._fill_combo.currentIndexChanged.connect(self._rebuild)
        btn_row.addWidget(self._fill_combo)
        btn_row.addSpacing(20)
        if editable:
            self._setup_undo(region_value)
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

        self._on_path_changed(0)
        self._vp.schedule_load(self._do_initial_load)
        if editable:
            self._vert_table.itemChanged.connect(self._on_item_changed)
            self._vp.vertex_moved.connect(self._on_viewport_vertex_moved)
            self._vp.vertex_drag_started.connect(self._begin_live_edit)
            self._vp.vertex_drag_finished.connect(lambda: self._end_live_edit("Move Vertex"))
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
        new_region = copy.deepcopy(self._region)
        new_region.append(new_path)
        insert_at = len(new_region) - 1
        self._commit_value(new_region, "Add Path")
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
        new_region = copy.deepcopy(self._region)
        new_region[path_idx].insert(insert_after + 1, [x, y])
        self._commit_value(new_region, "Add Vertex")
        self._vert_table.selectRow(insert_after + 1)
        self._vp.set_selected_path(path_idx)

    def _delete_path(self):
        """Remove the currently displayed path, refusing if that would
        leave 0 paths (`_is_region`'s minimum) -- silently, matching the
        no-popup convention used for invalid cell edits elsewhere."""
        if self._num_paths <= 1:
            return
        path_idx = self._path_combo.currentIndex()
        new_region = copy.deepcopy(self._region)
        del new_region[path_idx]
        self._commit_value(new_region, "Delete Path")
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
        new_region = copy.deepcopy(self._region)
        new_region[path_idx].insert(insert_at, new_pt)
        self._commit_value(new_region, "Add Vertex")
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
        new_region = copy.deepcopy(self._region)
        del new_region[path_idx][vertex_idx]
        self._commit_value(new_region, "Delete Vertex")
        self._vert_table.clearSelection()
        self._vp.set_selected_path(path_idx)

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        col_indices = sorted(r.row() for r in rows)
        path_idx = self._path_combo.currentIndex()
        path_start = self._path_offsets[path_idx]
        global_indices = [path_start + c for c in col_indices]
        self._vp.set_selected(global_indices)

    def _on_viewport_vertex_clicked(self, vi: int, mode: str):
        if vi < 0:
            _apply_click_selection(self._vert_table, -1, mode)
            return
        path_idx, col_idx = _grid_flat_to_rc(vi, self._path_offsets)
        if path_idx != self._path_combo.currentIndex():
            # The table only ever shows one path's vertices at a time --
            # switching paths repopulates it, so whatever was selected
            # belonged to a different path and no longer exists. "add"/
            # "toggle" can't mean anything across that boundary; fall back
            # to selecting just the clicked vertex in its own path.
            self._path_combo.setCurrentIndex(path_idx)
            mode = "replace"
        _apply_click_selection(self._vert_table, col_idx, mode)

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

    def _get_value(self):
        return self._region

    def _apply_value(self, value):
        self._region = value
        self._vp.sync_path_bookkeeping(self._region)
        self._refresh_path_bookkeeping()
        self._refresh_path_combo_items()
        path_idx = max(0, min(self._path_combo.currentIndex(), self._num_paths - 1))
        self._path_combo.blockSignals(True)
        self._path_combo.setCurrentIndex(path_idx)
        self._path_combo.blockSignals(False)
        self._on_path_changed(path_idx)
        self._rebuild()

    def _on_item_changed(self, item: QTableWidgetItem):
        path_idx = self._path_combo.currentIndex()
        col_idx, j = item.row(), item.column()
        parsed = _parse_number(item.text())
        if parsed is None:
            self._vert_table.blockSignals(True)
            item.setText(f"{self._region[path_idx][col_idx][j]:g}")
            self._vert_table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._region)
        new_value[path_idx][col_idx][j] = parsed
        self._commit_value(new_value, "Edit Vertex")

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

    vertex_clicked = Signal(int, str)  # (flat vertex index, click mode: "replace"/"add"/"toggle" -- see _vertex_click_mode)
    vertex_moved = Signal(int, float, float, float)  # (flat index, new_x, new_y, new_z) -- Cmd+drag, editable only
    # Bracket a continuous vertex_moved sequence (a Cmd+drag gesture, or one
    # keyboard nudge) so the dialog can push exactly one undo step for the
    # whole gesture instead of one per live frame.
    vertex_drag_started = Signal()
    vertex_drag_finished = Signal()
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
        # Vertex markers start on here, unlike _VNFViewport where they are
        # opt-in: a path/grid/region carries few enough points to draw them
        # all by default, but they still hide the shape underneath, so the
        # viewer offers the same "Show Vertices" toggle to clear the view.
        self._show_unselected = True
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

    def _marker_faces(self, r):
        return _dodecahedron_faces(r, True)

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

        keep = [i for i in range(len(pts)) if i not in selected]
        if keep:
            shown = np.asarray(pts, dtype=np.float32)[keep]
            self._renderer.upload_points(_lit_marker_field(
                shown, _marker_radii_for_points(self, shown), unit_faces,
                unselected_color))

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
                self.vertex_drag_started.emit()
                for vi in self._selected_indices:
                    if 0 <= vi < len(self._all_pts):
                        new_pt = self._all_pts[vi] + delta
                        self.vertex_moved.emit(vi, round(float(new_pt[0]), 3),
                                                round(float(new_pt[1]), 3), 0.0)
                self.vertex_drag_finished.emit()
                event.accept()
                return
        super().keyPressEvent(event)
