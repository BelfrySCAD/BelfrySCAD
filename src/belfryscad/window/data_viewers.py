"""
Data viewer windows launched from the debugger's variable context menu.

- ListViewer: scrollable table for lists and OscObject values
- VNFViewer: 3D mesh viewer for [vertices, faces] structures
- PathViewer: 2D/3D path viewer with point markers and connecting lines
"""
from __future__ import annotations
import bisect
import math
import numpy as np

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QCheckBox, QMenu, QLabel, QPushButton,
    QSplitter, QTabWidget, QWidget, QComboBox,
)
from PySide6.QtCore import Qt, QPoint, Signal, QTimer
from PySide6.QtGui import QFont, QMouseEvent

from belfryscad.window.viewport import Viewport


def _fmt_short(v) -> str:
    from belfryscad.window.debugger import _fmt
    return _fmt(v)


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


# ---------------------------------------------------------------------------
# List / Object Viewer
# ---------------------------------------------------------------------------

class ListViewer(QDialog):
    """Scrollable table displaying a list (with indices) or OscObject (with keys)."""

    def __init__(self, title: str, value, parent=None):
        super().__init__(parent)
        self._title = title
        self.setWindowTitle(f"List Viewer: {title}")
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
            menu.addAction("View in List...", lambda: _open_list_viewer(
                sub_title, val, self))
        if _is_vnf(val):
            menu.addAction("View as VNF...", lambda: _open_vnf_viewer(
                sub_title, val, self))
        if _is_path(val):
            menu.addAction("View as Path...", lambda: _open_path_viewer(
                sub_title, val, self))
        if menu.isEmpty():
            return
        menu.exec(self._table.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------------------
# VNF Viewport (adds face picking and highlight)
# ---------------------------------------------------------------------------

class _VNFViewport(Viewport):
    face_clicked = Signal(int)
    def __init__(self, parent=None):
        super().__init__(parent, selectable=False)
        self._renderer.camera.fov = 45.0
        self._renderer.depth_test_points = True
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

    def set_face_data(self, cpu_positions: np.ndarray, tri_to_face: np.ndarray,
                      verts_3d: np.ndarray | None = None):
        self._cpu_positions = cpu_positions
        self._tri_to_face = tri_to_face
        if verts_3d is not None:
            self._verts_3d = verts_3d

    def highlight_face(self, face_idx: int):
        if face_idx == self._selected_face:
            return
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
        self._release_vert_markers()
        self._vert_indices = []

        if not indices or self._ctx is None or len(self._verts_3d) == 0:
            self._vert_blink_timer.stop()
            self.update()
            return

        valid_indices = [vi for vi in indices if 0 <= vi < len(self._verts_3d)]
        self._vert_indices = valid_indices
        if valid_indices:
            self.scroll_to_visible(self._verts_3d[valid_indices[0]])
        self._vert_blink_red = True
        self._vert_blink_timer.start()
        self._build_vert_markers()
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

        cam = self._renderer.camera
        vh = self.height()
        if vh > 0:
            world_per_px = 2.0 * cam.distance * math.tan(math.radians(cam.fov / 2)) / vh
        else:
            world_per_px = cam.distance * 0.003
        r = world_per_px * 3.5
        px = np.array([r, 0, 0])
        nx = np.array([-r, 0, 0])
        py = np.array([0, r, 0])
        ny = np.array([0, -r, 0])
        pz = np.array([0, 0, r])
        nz = np.array([0, 0, -r])
        octa_faces = [
            (pz, px, py), (pz, py, nx), (pz, nx, ny), (pz, ny, px),
            (nz, py, px), (nz, nx, py), (nz, ny, nx), (nz, px, ny),
        ]

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_vert_marker_vao_r", "_vert_marker_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_vert_marker_vao_w", "_vert_marker_vbo_w"),
        ]:
            tris = []
            for vi in self._vert_indices:
                pt = self._verts_3d[vi]
                for v0, v1, v2 in octa_faces:
                    tris.append(np.concatenate([pt + v0, color_val]))
                    tris.append(np.concatenate([pt + v1, color_val]))
                    tris.append(np.concatenate([pt + v2, color_val]))
            if tris:
                data = np.array(tris, dtype=np.float32)
                vbo = self._ctx.buffer(data.tobytes())
                vao = self._ctx.vertex_array(
                    self._renderer._gizmo_prog,
                    [(vbo, "3f 3f", "in_position", "in_color")],
                )
                setattr(self, vao_attr, vao)
                setattr(self, vbo_attr, vbo)

    def frame_scene(self, bb_min, bb_max):
        super().frame_scene(bb_min, bb_max)
        if self._vert_indices:
            self._build_vert_markers()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if self._vert_indices:
            self._build_vert_markers()

    def _paint_extra(self, mvp: np.ndarray):
        import moderngl as mgl
        # Vertex markers (swap red/white)
        vao = self._vert_marker_vao_r if self._vert_blink_red else self._vert_marker_vao_w
        if vao is not None:
            self._renderer._gizmo_prog["mvp"].write(mvp.T.astype(np.float32).tobytes())
            vao.render(mgl.TRIANGLES)

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

    def mousePressEvent(self, event: QMouseEvent):
        self._press_pos = event.position().toPoint()
        self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None and self._last_mouse is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
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
    """3D mesh viewer for VNF [vertices, faces] structures with vertex/face tables."""

    def __init__(self, title: str, vnf_value: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"VNF Viewer: {title}")
        self.resize(900, 560)
        self._vnf = vnf_value
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Viewport — match main window's perspective mode
        self._vp = _VNFViewport(splitter)
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            if hasattr(w, '_viewport'):
                self._vp._renderer.camera.orthographic = w._viewport._renderer.camera.orthographic
                break
        self._vp.face_clicked.connect(self._on_viewport_face_clicked)
        splitter.addWidget(self._vp)

        # Tables in a tab widget
        self._tab_widget = QTabWidget(splitter)

        self._vert_table = self._make_vert_table(vnf_value[0])
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
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._load_mesh)

    @staticmethod
    def _make_vert_table(verts) -> QTableWidget:
        t = QTableWidget(len(verts), 3)
        t.setFont(QFont("Menlo", 11))
        t.setHorizontalHeaderLabels(["X", "Y", "Z"])
        t.setVerticalHeaderLabels([str(i) for i in range(len(verts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, v in enumerate(verts):
            for j in range(3):
                item = QTableWidgetItem(f"{v[j]:g}")
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

    def _load_mesh(self):
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
        self._vp.frame_scene(bb_min, bb_max)
        self._vp.update()

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
    """2D/3D path viewer with vertex table, selectable markers, and hover tooltips."""

    def __init__(self, title: str, path_value: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Path Viewer: {title}")
        self.resize(900, 520)

        self._path = path_value
        self._is_2d = all(len(p) == 2 for p in path_value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _PathViewport(path_value, self._is_2d, self)
        splitter.addWidget(self._vp)

        self._vert_table = self._make_vert_table(path_value, self._is_2d)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.addWidget(QLabel(f"Path Points ({len(path_value)})"))
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
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._vp.schedule_load(self._do_initial_load)

    @staticmethod
    def _make_vert_table(path_value: list, is_2d: bool) -> QTableWidget:
        cols = 2 if is_2d else 3
        t = QTableWidget(len(path_value), cols)
        t.setFont(QFont("Menlo", 11))
        headers = ["X", "Y"] if is_2d else ["X", "Y", "Z"]
        t.setHorizontalHeaderLabels(headers)
        t.setVerticalHeaderLabels([str(i) for i in range(len(path_value))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, p in enumerate(path_value):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(cols):
            t.setColumnWidth(j, min_w)
        return t

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        indices = sorted(r.row() for r in rows)
        self._vp.set_selected(indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        self._vert_table.clearSelection()
        if 0 <= vi < self._vert_table.rowCount():
            self._vert_table.selectRow(vi)

    def _do_initial_load(self):
        self._vp.load_path(self._path, self._closed_cb.isChecked(),
                           self._bezier_cb.isChecked())

    def _rebuild(self, _=None):
        if self._vp._ctx is not None:
            self._vp.load_path(self._path, self._closed_cb.isChecked(),
                               self._bezier_cb.isChecked())



class _PathViewport(Viewport):
    """Viewport subclass with selectable vertex markers and hover tooltips."""
    vertex_clicked = Signal(int)

    def __init__(self, path_value: list, is_2d: bool, parent=None):
        super().__init__(parent, selectable=False)
        cam = self._renderer.camera
        cam.fov = 45.0
        self._renderer.line_width = 2.0
        self._press_pos = None
        self._drag_started = False
        self._path_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
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
        if is_2d:
            cam.azimuth = 0.0
            cam.elevation = 90.0
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

    def load_path(self, path_value: list, closed: bool, bezier: bool = False):
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

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_scene(bb_min, bb_max)

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
        self.update()

    def _octa_faces(self, r):
        px = np.array([r, 0, 0])
        nx = np.array([-r, 0, 0])
        py = np.array([0, r, 0])
        ny = np.array([0, -r, 0])
        pz = np.array([0, 0, r])
        nz = np.array([0, 0, -r])
        if self._is_2d:
            return [
                (py, px, ny), (ny, px, py),
                (py, nx, ny), (ny, nx, py),
            ]
        return [
            (pz, px, py), (pz, py, nx), (pz, nx, ny), (pz, ny, px),
            (nz, py, px), (nz, nx, py), (nz, ny, nx), (nz, px, ny),
        ]

    def _world_per_px_radius(self):
        cam = self._renderer.camera
        vh = self.height()
        if vh > 0:
            world_per_px = 2.0 * cam.distance * math.tan(math.radians(cam.fov / 2)) / vh
        else:
            world_per_px = cam.distance * 0.003
        return world_per_px * 3.5

    def _build_point_markers(self):
        self._renderer.clear_points()

        pts = self._path_pts
        if len(pts) == 0 or self._ctx is None:
            return

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            for v0, v1, v2 in faces:
                marker_tris.append(np.concatenate([pt + v0, green]))
                marker_tris.append(np.concatenate([pt + v1, green]))
                marker_tris.append(np.concatenate([pt + v2, green]))

        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected(self, indices: list[int]):
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.update()
            return

        if 0 <= indices[0] < len(self._path_pts):
            self.scroll_to_visible(self._path_pts[indices[0]])

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
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

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        pts = self._path_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    for v0, v1, v2 in faces:
                        tris.append(np.concatenate([pt + v0, color_val]))
                        tris.append(np.concatenate([pt + v1, color_val]))
                        tris.append(np.concatenate([pt + v2, color_val]))
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

    def frame_scene(self, bb_min, bb_max):
        super().frame_scene(bb_min, bb_max)
        if len(self._path_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if len(self._path_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
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
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None and self._last_mouse is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
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
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Grid Viewer
# ---------------------------------------------------------------------------

class GridViewer(QDialog):
    """3D grid viewer for lists of lists of points with quad mesh faces."""

    def __init__(self, title: str, grid_value: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Grid Viewer: {title}")
        self.resize(900, 520)

        self._grid = grid_value
        self._rows = len(grid_value)
        self._row_offsets = _grid_row_offsets(grid_value)
        all_pts = [p for row in grid_value for p in row]
        self._is_2d = all(len(p) == 2 for p in all_pts)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._vp = _GridViewport(grid_value, self._is_2d, self)
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

        self._vert_table = self._make_vert_table(grid_value[0], self._is_2d)
        self._vert_table.itemSelectionChanged.connect(self._on_vert_table_selection)
        self._vp.vertex_clicked.connect(self._on_viewport_vertex_clicked)
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
        self._faces_cb = QCheckBox("Faces")
        self._faces_cb.setChecked(True)
        self._faces_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._faces_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._faces_cb)
        self._col_wrap_cb = QCheckBox("Col Wrap")
        self._col_wrap_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._col_wrap_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._col_wrap_cb)
        self._row_wrap_cb = QCheckBox("Row Wrap")
        self._row_wrap_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._row_wrap_cb.toggled.connect(self._rebuild)
        btn_row.addWidget(self._row_wrap_cb)
        btn_row.addStretch()
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.close)
        btn_row.addWidget(dismiss)
        layout.addLayout(btn_row)

        self._on_row_changed(0)
        self._vp.schedule_load(self._do_initial_load)

    @staticmethod
    def _make_vert_table(row_pts: list, is_2d: bool) -> QTableWidget:
        cols = 2 if is_2d else 3
        t = QTableWidget(len(row_pts), cols)
        t.setFont(QFont("Menlo", 11))
        headers = ["X", "Y"] if is_2d else ["X", "Y", "Z"]
        t.setHorizontalHeaderLabels(headers)
        t.setVerticalHeaderLabels([str(i) for i in range(len(row_pts))])
        _style_table_headers(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                t.setItem(i, j, item)
        fm = t.fontMetrics()
        min_w = fm.horizontalAdvance("-00000.0") + 16
        for j in range(cols):
            t.setColumnWidth(j, min_w)
        return t

    def _populate_table(self, row_pts: list):
        cols = 2 if self._is_2d else 3
        self._vert_table.setRowCount(len(row_pts))
        self._vert_table.setVerticalHeaderLabels(
            [str(i) for i in range(len(row_pts))])
        for i, p in enumerate(row_pts):
            for j in range(cols):
                item = QTableWidgetItem(f"{p[j]:g}")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._vert_table.setItem(i, j, item)

    def _on_row_changed(self, row_idx: int):
        if row_idx < 0 or row_idx >= self._rows:
            return
        row_pts = self._grid[row_idx]
        self._pts_label.setText(f"Row Points ({len(row_pts)})")
        self._populate_table(row_pts)
        self._vert_table.selectAll()
        self._vp.set_selected_row(row_idx)

    def _on_vert_table_selection(self):
        rows = self._vert_table.selectionModel().selectedRows()
        col_indices = sorted(r.row() for r in rows)
        row_idx = self._row_combo.currentIndex()
        row_start = self._row_offsets[row_idx]
        global_indices = [row_start + c for c in col_indices]
        self._vp.set_selected(global_indices)

    def _on_viewport_vertex_clicked(self, vi: int):
        if vi < 0:
            return
        row_idx, col_idx = _grid_flat_to_rc(vi, self._row_offsets)
        if row_idx != self._row_combo.currentIndex():
            self._row_combo.setCurrentIndex(row_idx)
        self._vert_table.clearSelection()
        if 0 <= col_idx < self._vert_table.rowCount():
            self._vert_table.selectRow(col_idx)

    def _do_initial_load(self):
        self._vp.load_grid(self._grid,
                           col_wrap=self._col_wrap_cb.isChecked(),
                           row_wrap=self._row_wrap_cb.isChecked(),
                           draw_faces=self._faces_cb.isChecked())

    def _rebuild(self, _=None):
        if self._vp._ctx is not None:
            self._vp.load_grid(self._grid,
                               col_wrap=self._col_wrap_cb.isChecked(),
                               row_wrap=self._row_wrap_cb.isChecked(),
                               draw_faces=self._faces_cb.isChecked())


class _GridViewport(Viewport):
    """Viewport for grid data with quad mesh faces and selectable vertex markers."""
    vertex_clicked = Signal(int)

    def __init__(self, grid_value: list, is_2d: bool, parent=None):
        super().__init__(parent, selectable=False)
        cam = self._renderer.camera
        cam.fov = 45.0
        self._renderer.depth_test_points = True
        self._renderer.show_edges = True  # enables polygon offset fill so skeleton lines render in front of faces
        self._press_pos = None
        self._drag_started = False
        self._all_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
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
        if is_2d:
            cam.azimuth = 0.0
            cam.elevation = 90.0
            cam.orthographic = True
        else:
            cam.orthographic = False

    def _blink_tick(self):
        self._blink_red = not self._blink_red
        self.update()

    def load_grid(self, grid_value: list, col_wrap: bool = False,
                  row_wrap: bool = False, draw_faces: bool = True):
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
        rows = self._grid_rows
        row_offsets = self._row_offsets
        row_lens = [len(row) for row in grid_value]

        bb_min = pts.min(axis=0)
        bb_max = pts.max(axis=0)
        self.frame_scene(bb_min, bb_max)

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

        self._build_point_markers()
        if self._selected_indices:
            self._build_sel_markers()
        self.update()

    def _octa_faces(self, r):
        px = np.array([r, 0, 0])
        nx = np.array([-r, 0, 0])
        py = np.array([0, r, 0])
        ny = np.array([0, -r, 0])
        pz = np.array([0, 0, r])
        nz = np.array([0, 0, -r])
        if self._is_2d:
            return [
                (py, px, ny), (ny, px, py),
                (py, nx, ny), (ny, nx, py),
            ]
        return [
            (pz, px, py), (pz, py, nx), (pz, nx, ny), (pz, ny, px),
            (nz, py, px), (nz, nx, py), (nz, ny, nx), (nz, px, ny),
        ]

    def _world_per_px_radius(self):
        cam = self._renderer.camera
        vh = self.height()
        if vh > 0:
            world_per_px = 2.0 * cam.distance * math.tan(math.radians(cam.fov / 2)) / vh
        else:
            world_per_px = cam.distance * 0.003
        return world_per_px * 3.5

    def _build_point_markers(self):
        self._renderer.clear_points()

        pts = self._all_pts
        if len(pts) == 0 or self._ctx is None:
            return

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        green = np.array([0.0, 0.8, 0.2], dtype=np.float32)
        selected = set(self._selected_indices)

        marker_tris = []
        for i, pt in enumerate(pts):
            if i in selected:
                continue
            for v0, v1, v2 in faces:
                marker_tris.append(np.concatenate([pt + v0, green]))
                marker_tris.append(np.concatenate([pt + v1, green]))
                marker_tris.append(np.concatenate([pt + v2, green]))

        if marker_tris:
            self._renderer.upload_points(np.array(marker_tris, dtype=np.float32))

    def set_selected_row(self, row_idx: int):
        self._selected_row = row_idx
        start, end = self._row_offsets[row_idx], self._row_offsets[row_idx + 1]
        self.set_selected(list(range(start, end)))

    def set_selected(self, indices: list[int]):
        self._selected_indices = indices
        self._release_sel_markers()
        self._build_point_markers()

        if not indices:
            self._blink_timer.stop()
            self.update()
            return

        if 0 <= indices[0] < len(self._all_pts):
            self.scroll_to_visible(self._all_pts[indices[0]])

        self._blink_red = True
        self._blink_timer.start()
        self._build_sel_markers()
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

        r = self._world_per_px_radius()
        faces = self._octa_faces(r)
        pts = self._all_pts

        for color_val, vao_attr, vbo_attr in [
            (np.array([1.0, 0.0, 0.0], dtype=np.float32), "_sel_vao_r", "_sel_vbo_r"),
            (np.array([1.0, 1.0, 1.0], dtype=np.float32), "_sel_vao_w", "_sel_vbo_w"),
        ]:
            tris = []
            for vi in self._selected_indices:
                if 0 <= vi < len(pts):
                    pt = pts[vi]
                    for v0, v1, v2 in faces:
                        tris.append(np.concatenate([pt + v0, color_val]))
                        tris.append(np.concatenate([pt + v1, color_val]))
                        tris.append(np.concatenate([pt + v2, color_val]))
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
            vao.render(mgl.TRIANGLES)

    def frame_scene(self, bb_min, bb_max):
        super().frame_scene(bb_min, bb_max)
        if len(self._all_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if len(self._all_pts) > 0:
            self._build_point_markers()
            if self._selected_indices:
                self._build_sel_markers()
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
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None and self._last_mouse is not None:
            pos = event.position().toPoint()
            dx = abs(pos.x() - self._press_pos.x())
            dy = abs(pos.y() - self._press_pos.y())
            if dx > 3 or dy > 3:
                self._drag_started = True
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
        if (event.button() == Qt.MouseButton.LeftButton
                and not self._drag_started
                and self._press_pos is not None):
            pos = event.position().toPoint()
            vi = self._pick_vertex(pos.x(), pos.y())
            self.vertex_clicked.emit(vi)
        self._press_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)


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


def build_viewer_menu(menu: QMenu, name: str, value, parent=None):
    """Add viewer actions to a QMenu based on the value's type."""
    if _is_list(value) or _is_oscobject(value):
        menu.addAction("View in List...", lambda: _open_list_viewer(name, value, parent))
    if _is_vnf(value):
        menu.addAction("View as VNF...", lambda: _open_vnf_viewer(name, value, parent))
    if _is_grid(value):
        menu.addAction("View as Grid...", lambda: _open_grid_viewer(name, value, parent))
    if _is_path(value):
        menu.addAction("View as Path...", lambda: _open_path_viewer(name, value, parent))
