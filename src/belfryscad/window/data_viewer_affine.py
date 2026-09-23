"""AffineMatrixViewer: a 3x3/4x4 homogeneous affine transform, shown by its
effect on a reference square or cube."""
from __future__ import annotations

import copy
import math

import numpy as np

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
                               QTableWidgetItem, QAbstractItemView, QCheckBox, QLabel,
                               QPushButton, QSplitter, QTabWidget, QWidget, QComboBox,
                               QLineEdit)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from belfryscad.window.viewport import Viewport
from belfryscad.window.data_viewer_common import (_UndoableViewerMixin,
                                                  _dodecahedron_faces, _fmt_short,
                                                  _format_value, _identity_matrix,
                                                  _is_list, _lit_marker_field,
                                                  _marker_radii_for_points,
                                                  _parse_number, _style_table_headers,
                                                  _sync_viewport_to_main_window)


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


def _translation_matrix(delta, n: int) -> np.ndarray:
    """NxN homogeneous translation matrix. len(delta) == n - 1."""
    m = np.eye(n, dtype=np.float64)
    for i, d in enumerate(delta):
        m[i, n - 1] = d
    return m


def _axis_rotation_matrix(n: int, axis, angle_deg: float) -> np.ndarray:
    """NxN homogeneous rotation about the origin. n == 3: axis is ignored
    (single implicit in-plane/Z rotation). n == 4: axis in {0, 1, 2} for
    X/Y/Z, an elementary axis-aligned rotation."""
    m = np.eye(n, dtype=np.float64)
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    if n == 3:
        m[0, 0], m[0, 1] = c, -s
        m[1, 0], m[1, 1] = s, c
        return m
    i, j = {0: (1, 2), 1: (2, 0), 2: (0, 1)}[axis]
    m[i, i], m[i, j] = c, -s
    m[j, i], m[j, j] = s, c
    return m


def _scale_matrix(factors, n: int) -> np.ndarray:
    """NxN homogeneous scale about the origin. len(factors) == n - 1."""
    return np.diag(list(factors) + [1.0]).astype(np.float64)


def _shear_matrix(n: int, axis_a: int, axis_b: int, factor: float) -> np.ndarray:
    """NxN homogeneous shear about the origin: axis_a' += factor * axis_b.
    axis_a != axis_b, both in range(n - 1)."""
    m = np.eye(n, dtype=np.float64)
    m[axis_a, axis_b] = factor
    return m


def _pivot_about(op: np.ndarray, center, n: int) -> np.ndarray:
    """Wrap an origin-based homogeneous op as T(c) @ op @ T(-c), so it
    acts about *center* instead of the origin."""
    t_c = _translation_matrix(center, n)
    t_nc = _translation_matrix([-x for x in center], n)
    return t_c @ op @ t_nc


def _compose_after(op: np.ndarray, current: list) -> list:
    """M_new = op @ current -- op is left-multiplied onto the existing
    matrix, i.e. applied AFTER it. The single boundary function that
    converts the numpy math result back into the nested-list shape a
    viewer's stored value requires."""
    return (op @ np.array(current, dtype=np.float64)).tolist()


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
        # Vertex markers start on here, unlike _VNFViewport where they are
        # opt-in: a path/grid/region carries few enough points to draw them
        # all by default, but they still hide the shape underneath, so the
        # viewer offers the same "Show Vertices" toggle to clear the view.
        self._show_unselected = True
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
        if len(self._corners) == 0 or self._ctx is None:
            return
        unit_faces = _dodecahedron_faces(1.0, self._is_2d)
        first_color = np.array([0.85, 0.15, 0.15], dtype=np.float32)
        rest_color = np.array([0.9, 0.45, 0.1], dtype=np.float32)
        pts = np.asarray(self._corners, dtype=np.float32)
        colors = np.tile(rest_color, (len(pts), 1))
        colors[0] = first_color
        rows = _lit_marker_field(pts, _marker_radii_for_points(self, pts),
                                 unit_faces, colors)
        if len(rows):
            self._renderer.upload_points(rows)


class AffineMatrixViewer(QDialog, _UndoableViewerMixin):
    """Visualizes a 3x3 (2D) or 4x4 (3D) homogeneous affine transform
    matrix by showing a reference unit square/cube next to its image
    under the matrix, alongside the matrix's numbers. Read-only by default;
    pass `editable=True` for a Save/Cancel editing mode (see `MatrixViewer`
    for the shared editing convention). Editable mode additionally offers
    Translate/Rotate/Scale/Skew tools that compose a new operation onto
    the current matrix (M_new = N . M -- see _compose_after) rather than
    requiring the user to hand-edit numbers."""

    committed = Signal(str)

    _AXIS_NAMES = ["X", "Y", "Z"]

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
        _sync_viewport_to_main_window(self._vp)
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

        if editable:
            self._setup_undo(value)
            layout.addWidget(self._build_tools_tabs(n))

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 20, 0)
        # Checked by default here, unlike VNFViewer's opt-in: these
        # shapes carry few enough points to draw them all, and
        # unchecking this is what clears the view.
        self._show_verts_cb = QCheckBox("Show Vertices")
        self._show_verts_cb.setStyleSheet("QCheckBox { padding-right: 20px; }")
        self._show_verts_cb.setChecked(True)
        self._show_verts_cb.toggled.connect(self._vp.set_show_unselected)
        btn_row.addWidget(self._show_verts_cb)
        if editable:
            reset_id = QPushButton("Reset to Identity")
            reset_id.clicked.connect(self._on_reset_identity)
            btn_row.addWidget(reset_id)
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

        self.resize(600 + table_w, 480 + (90 if editable else 0))

    def _get_value(self):
        return self._value

    def _apply_value(self, value):
        self._value = value
        n = len(value)
        self._table.blockSignals(True)
        for r in range(n):
            for c in range(n):
                self._table.item(r, c).setText(_fmt_short(value[r][c]))
        self._table.blockSignals(False)
        if self._vp._ctx is not None:
            self._vp.load_matrix(self._value)

    def _on_reset_identity(self):
        self._commit_value(_identity_matrix(len(self._value)), "Reset to Identity")

    def _on_item_changed(self, item: QTableWidgetItem):
        parsed = _parse_number(item.text())
        if parsed is None:
            self._table.blockSignals(True)
            item.setText(_fmt_short(self._value[item.row()][item.column()]))
            self._table.blockSignals(False)
            return
        new_value = copy.deepcopy(self._value)
        new_value[item.row()][item.column()] = parsed
        self._commit_value(new_value, "Edit Cell")

    def _on_save(self):
        self.committed.emit(_format_value(self._value))
        self.accept()

    # ------------------------------------------------------------------
    # Translate/Rotate/Scale/Skew tools
    # ------------------------------------------------------------------

    def _build_tools_tabs(self, n: int) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setMaximumHeight(80)
        tabs.addTab(self._build_translate_tab(n), "Translate")
        tabs.addTab(self._build_rotate_tab(n), "Rotate")
        tabs.addTab(self._build_scale_tab(n), "Scale")
        tabs.addTab(self._build_skew_tab(n), "Skew")
        return tabs

    def _current_translation(self, n: int) -> list:
        """The matrix's current translation column -- used as the default
        centerpoint for Rotate/Scale ("rotate/scale in place")."""
        return [self._value[i][n - 1] for i in range(n - 1)]

    def _make_vector_fields(self, n: int, default: list = None) -> list:
        fields = []
        for i in range(n - 1):
            e = QLineEdit(_fmt_short(default[i]) if default is not None else "0")
            e.setMaximumWidth(60)
            fields.append(e)
        return fields

    def _make_center_fields(self, n: int) -> list:
        return self._make_vector_fields(n, self._current_translation(n))

    @staticmethod
    def _read_fields(fields: list) -> list | None:
        values = [_parse_number(f.text()) for f in fields]
        return None if any(v is None for v in values) else values

    def _build_translate_tab(self, n: int) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        fields = self._make_vector_fields(n)
        for axis, e in zip(self._AXIS_NAMES, fields):
            row.addWidget(QLabel(axis + ":"))
            row.addWidget(e)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(lambda: self._on_apply_translate(fields))
        row.addWidget(apply_btn)
        row.addStretch()
        return w

    def _on_apply_translate(self, fields: list):
        delta = self._read_fields(fields)
        if delta is None:
            return
        n = len(self._value)
        op = _translation_matrix(delta, n)
        self._commit_value(_compose_after(op, self._value), "Translate")

    def _build_rotate_tab(self, n: int) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        axis_combo = None
        if n == 4:
            row.addWidget(QLabel("Axis:"))
            axis_combo = QComboBox()
            axis_combo.addItems(self._AXIS_NAMES)
            row.addWidget(axis_combo)
        row.addWidget(QLabel("Angle:"))
        angle_field = QLineEdit("0")
        angle_field.setMaximumWidth(60)
        row.addWidget(angle_field)
        row.addWidget(QLabel("Center:"))
        center_fields = self._make_center_fields(n)
        for e in center_fields:
            row.addWidget(e)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(lambda: self._on_apply_rotate(axis_combo, angle_field, center_fields))
        row.addWidget(apply_btn)
        row.addStretch()
        return w

    def _on_apply_rotate(self, axis_combo, angle_field, center_fields: list):
        angle = _parse_number(angle_field.text())
        center = self._read_fields(center_fields)
        if angle is None or center is None:
            return
        n = len(self._value)
        axis = axis_combo.currentIndex() if axis_combo is not None else None
        op = _pivot_about(_axis_rotation_matrix(n, axis, angle), center, n)
        self._commit_value(_compose_after(op, self._value), "Rotate")

    def _build_scale_tab(self, n: int) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        factor_fields = self._make_vector_fields(n, [1] * (n - 1))
        for axis, e in zip(self._AXIS_NAMES, factor_fields):
            row.addWidget(QLabel(axis + ":"))
            row.addWidget(e)
        uniform_cb = QCheckBox("Uniform")

        def _on_uniform_toggled(checked):
            for e in factor_fields[1:]:
                e.setEnabled(not checked)
        uniform_cb.toggled.connect(_on_uniform_toggled)
        row.addWidget(uniform_cb)
        row.addWidget(QLabel("Center:"))
        center_fields = self._make_center_fields(n)
        for e in center_fields:
            row.addWidget(e)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(
            lambda: self._on_apply_scale(factor_fields, uniform_cb, center_fields))
        row.addWidget(apply_btn)
        row.addStretch()
        return w

    def _on_apply_scale(self, factor_fields: list, uniform_cb, center_fields: list):
        factors = self._read_fields(factor_fields)
        center = self._read_fields(center_fields)
        if factors is None or center is None:
            return
        if uniform_cb.isChecked():
            factors = [factors[0]] * len(factors)
        n = len(self._value)
        op = _pivot_about(_scale_matrix(factors, n), center, n)
        self._commit_value(_compose_after(op, self._value), "Scale")

    def _build_skew_tab(self, n: int) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.addWidget(QLabel("Shear axis:"))
        axis_a_combo = QComboBox()
        axis_a_combo.addItems(self._AXIS_NAMES[:n - 1])
        row.addWidget(axis_a_combo)
        row.addWidget(QLabel("along:"))
        axis_b_combo = QComboBox()
        row.addWidget(axis_b_combo)

        def _refresh_axis_b():
            a_idx = axis_a_combo.currentIndex()
            axis_b_combo.blockSignals(True)
            axis_b_combo.clear()
            for i, name in enumerate(self._AXIS_NAMES[:n - 1]):
                if i != a_idx:
                    axis_b_combo.addItem(name, i)
            axis_b_combo.blockSignals(False)
        axis_a_combo.currentIndexChanged.connect(_refresh_axis_b)
        _refresh_axis_b()

        row.addWidget(QLabel("Factor:"))
        factor_field = QLineEdit("0")
        factor_field.setMaximumWidth(60)
        row.addWidget(factor_field)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(
            lambda: self._on_apply_skew(axis_a_combo, axis_b_combo, factor_field))
        row.addWidget(apply_btn)
        row.addStretch()
        return w

    def _on_apply_skew(self, axis_a_combo, axis_b_combo, factor_field):
        factor = _parse_number(factor_field.text())
        if factor is None or axis_b_combo.currentData() is None:
            return
        n = len(self._value)
        axis_a = axis_a_combo.currentIndex()
        axis_b = axis_b_combo.currentData()
        op = _shear_matrix(n, axis_a, axis_b, factor)
        self._commit_value(_compose_after(op, self._value), "Skew")
