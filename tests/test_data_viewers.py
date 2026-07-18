"""
Tests for the GridViewer shape-detection and flat-index helpers in
`belfryscad.window.data_viewers` — specifically that a "grid" (list of lists
of points) need not be rectangular: rows may have different lengths (e.g. a
cone's single-point apex row next to a wider base row). Also covers
`_is_matrix`/`_is_affine_matrix`, the shape-detection helpers for
MatrixViewer/AffineMatrixViewer, and the affine-transform math helpers, and
`_is_region` (RegionViewer's shape detection — a list of >= 1 closed 2D
polygon paths under even-odd fill semantics, structurally identical to a 2D
grid whenever there are >= 2 paths, so both interpretations are legitimately
offered together).

These test the pure-Python helpers only (`_is_grid`, `_grid_row_offsets`,
`_grid_flat_to_rc`, `_grid_is_triangular`, `_grid_fan_spec`, `_is_matrix`,
`_is_affine_matrix`, `_affine_reference_shape`, `_affine_shape_edges`,
`_apply_affine`, `_is_region`); the Qt/OpenGL rendering classes (`GridViewer`,
`_GridViewport`, `MatrixViewer`, `AffineMatrixViewer`, `_AffineViewport`,
`RegionViewer`, `_RegionViewport`) aren't covered here, consistent with the
rest of the test suite (no existing tests instantiate Qt widgets).
"""
import numpy as np
from PySide6.QtCore import Qt

from belfryscad.engine.renderer import Camera
from belfryscad.window.data_viewers import (
    _is_grid, _grid_row_offsets, _grid_flat_to_rc, _grid_is_triangular,
    _grid_fan_spec, _is_matrix, _is_path, _is_affine_matrix, _is_region,
    _affine_reference_shape, _affine_shape_edges, _apply_affine,
    _identity_matrix, _translation_matrix, _axis_rotation_matrix,
    _scale_matrix, _shear_matrix, _pivot_about, _compose_after,
    _iter_enclosing_literals, find_editable_literals, find_viewable_literals,
    _key_nudge_magnitude, _key_nudge_delta,
    _classify_node_type, _remap_node_types, _bezier_linked_moves, _decasteljau_split,
    _v0_handle_indices, _snap_handles_to_node_type, _fit_merged_segment,
    _owning_v0_index,
)


class TestIsGrid:
    def test_rectangular_grid_is_grid(self):
        assert _is_grid([[[0, 0], [1, 0]], [[0, 1], [1, 1]]])

    def test_ragged_grid_is_grid(self):
        # row 0 has 3 points, row 1 has 2 points
        assert _is_grid([[[0, 0], [1, 0], [2, 0]], [[0, 1], [1, 1]]])

    def test_single_point_row_is_grid(self):
        # apex row (1 point) next to a wider row
        assert _is_grid([[[0, 0, 5]], [[-1, -1, 0], [1, -1, 0], [0, 1, 0]]])

    def test_single_row_is_not_grid(self):
        assert not _is_grid([[[0, 0], [1, 0]]])

    def test_plain_path_is_not_grid(self):
        # a flat list of points (not a list of lists of points)
        assert not _is_grid([[0, 0], [1, 0], [2, 0]])

    def test_empty_row_is_not_grid(self):
        assert not _is_grid([[[0, 0]], []])

    def test_non_numeric_row_is_not_grid(self):
        assert not _is_grid([["a", "b"], ["c", "d"]])

    def test_3d_points_ragged_is_grid(self):
        assert _is_grid([[[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0]],
                          [[0, 1, 1], [1, 1, 1]]])


class TestIsRegion:
    def test_single_triangle_path_is_region(self):
        # A region may have just 1 path (no holes) -- the one case that
        # doesn't also read as a grid (_is_grid requires >= 2 rows).
        assert _is_region([[[0, 0], [1, 0], [0, 1]]])

    def test_single_path_is_not_grid(self):
        assert not _is_grid([[[0, 0], [1, 0], [0, 1]]])

    def test_concentric_paths_is_region(self):
        # Three concentric triangles -- disc + ring + hole semantics.
        outer = [[10, 0], [-10, 10], [-10, -10]]
        middle = [[6, 0], [-6, 6], [-6, -6]]
        inner = [[3, 0], [-3, 3], [-3, -3]]
        assert _is_region([outer, middle, inner])

    def test_concentric_paths_also_reads_as_grid(self):
        # Structurally identical to a 2D grid whenever there are >= 2
        # paths -- both interpretations are legitimate, so both "Edit as
        # Grid..."/"Edit as Region..." should be offered; not a bug.
        outer = [[10, 0], [-10, 10], [-10, -10]]
        inner = [[3, 0], [-3, 3], [-3, -3]]
        assert _is_region([outer, inner])
        assert _is_grid([outer, inner])

    def test_two_point_path_is_not_region(self):
        # A polygon needs >= 3 points.
        assert not _is_region([[[0, 0], [1, 1]]])

    def test_3d_points_not_a_region(self):
        # No 3D regions.
        assert not _is_region([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]])

    def test_empty_list_is_not_region(self):
        assert not _is_region([])

    def test_non_numeric_path_is_not_region(self):
        assert not _is_region([["a", "b", "c"]])


class TestGridRowOffsets:
    def test_rectangular_offsets(self):
        grid = [[[0, 0], [1, 0]], [[0, 1], [1, 1]]]
        assert _grid_row_offsets(grid) == [0, 2, 4]

    def test_ragged_offsets(self):
        grid = [[[0, 0]], [[0, 1], [1, 1], [2, 1]], [[0, 2], [1, 2]]]
        assert _grid_row_offsets(grid) == [0, 1, 4, 6]

    def test_offsets_total_matches_point_count(self):
        grid = [[[0, 0], [1, 0], [2, 0]], [[0, 1]], [[0, 2], [1, 2]]]
        offsets = _grid_row_offsets(grid)
        total_points = sum(len(row) for row in grid)
        assert offsets[-1] == total_points


class TestGridFlatToRc:
    def test_round_trips_every_point_in_ragged_grid(self):
        grid = [[[0, 0]], [[0, 1], [1, 1], [2, 1]], [[0, 2], [1, 2]]]
        offsets = _grid_row_offsets(grid)
        expected = []
        for r, row in enumerate(grid):
            for c in range(len(row)):
                expected.append((r, c))
        actual = [_grid_flat_to_rc(vi, offsets) for vi in range(offsets[-1])]
        assert actual == expected

    def test_rectangular_grid_matches_divmod(self):
        grid = [[[0, 0], [1, 0], [2, 0]], [[0, 1], [1, 1], [2, 1]]]
        offsets = _grid_row_offsets(grid)
        cols = 3
        for vi in range(6):
            assert _grid_flat_to_rc(vi, offsets) == (vi // cols, vi % cols)

    def test_first_and_last_point(self):
        grid = [[[0, 0], [1, 0]], [[0, 1], [1, 1], [2, 1]]]
        offsets = _grid_row_offsets(grid)
        assert _grid_flat_to_rc(0, offsets) == (0, 0)
        assert _grid_flat_to_rc(4, offsets) == (1, 2)


class TestGridIsTriangular:
    def test_rectangular_grid_is_not_triangular(self):
        assert not _grid_is_triangular([3, 3, 3])

    def test_apex_to_base_taper_is_triangular(self):
        assert _grid_is_triangular([1, 8])

    def test_triangular_number_progression_is_triangular(self):
        assert _grid_is_triangular([1, 2, 3, 4])

    def test_single_mismatch_among_matching_rows_is_triangular(self):
        assert _grid_is_triangular([3, 3, 4, 4])

    def test_row_wrap_considers_wraparound_pair(self):
        # Without wrap, rows 0 and 2 (the endpoints) aren't adjacent.
        assert not _grid_is_triangular([3, 3, 3], row_wrap=False)
        # With wrap, the last row connects back to the first — a mismatch
        # there also makes the grid triangular.
        assert _grid_is_triangular([3, 3, 4], row_wrap=True)


class TestGridFanSpec:
    def test_equal_lengths_needs_no_fan(self):
        assert _grid_fan_spec(3, 3, col_wrap=False) is None

    def test_apex_to_base_fans_every_base_edge(self):
        # A single apex point (row A) fanning out to an 8-point base row
        # (row B) needs 7 fan triangles/spokes to cover every base edge.
        anchor_in_a, anchor_col, longer_len, ks = _grid_fan_spec(1, 8, col_wrap=False)
        assert anchor_in_a is True
        assert anchor_col == 0
        assert longer_len == 8
        assert list(ks) == [0, 1, 2, 3, 4, 5, 6]

    def test_apex_to_base_col_wrap_closes_the_fan(self):
        anchor_in_a, anchor_col, longer_len, ks = _grid_fan_spec(1, 8, col_wrap=True)
        assert list(ks) == [0, 1, 2, 3, 4, 5, 6, 7]

    def test_off_by_one_growth_needs_one_extra_triangle(self):
        # Rows of length 2 then 3 (a triangular-number step): the shared
        # prefix (columns 0-1) is a plain quad; only the extra column (2)
        # needs a fan triangle.
        anchor_in_a, anchor_col, longer_len, ks = _grid_fan_spec(2, 3, col_wrap=False)
        assert anchor_in_a is True
        assert anchor_col == 1
        assert longer_len == 3
        assert list(ks) == [1]

    def test_direction_reverses_when_second_row_is_shorter(self):
        # Same taper, but row A is now the longer one — the anchor should
        # be identified as belonging to row B instead.
        anchor_in_a, anchor_col, longer_len, ks = _grid_fan_spec(3, 2, col_wrap=False)
        assert anchor_in_a is False
        assert anchor_col == 1
        assert longer_len == 3
        assert list(ks) == [1]


class TestIsMatrix:
    def test_2x2_is_matrix(self):
        assert _is_matrix([[1, 2], [3, 4]])

    def test_5x5_is_matrix(self):
        assert _is_matrix([[i * 5 + j for j in range(5)] for i in range(5)])

    def test_6x6_is_too_big(self):
        assert not _is_matrix([[i * 6 + j for j in range(6)] for i in range(6)])

    def test_1x1_is_too_small(self):
        assert not _is_matrix([[1]])

    def test_non_square_is_not_matrix(self):
        assert not _is_matrix([[1, 2, 3], [4, 5, 6]])

    def test_ragged_rows_not_matrix(self):
        assert not _is_matrix([[1, 2], [3, 4, 5]])

    def test_non_numeric_entry_is_not_matrix(self):
        assert not _is_matrix([[1, "a"], [3, 4]])

    def test_flat_list_is_not_matrix(self):
        assert not _is_matrix([1, 2, 3, 4])

    def test_matrix_never_satisfies_is_grid(self):
        # No overlap with GridViewer: _is_grid expects each row to be a
        # list of *points* (2/3-number lists) — one nesting level deeper
        # than a matrix row, which is a list of plain numbers.
        m = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        assert _is_matrix(m)
        assert not _is_grid(m)

    def test_2x2_and_3x3_matrix_also_satisfies_is_path(self):
        # Real overlap: a 2x2 or 3x3 matrix's rows are themselves valid
        # 2D/3D points, so it's also a valid path of 2 or 3 points.
        assert _is_matrix([[1, 2], [3, 4]])
        assert _is_path([[1, 2], [3, 4]])
        m3 = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        assert _is_matrix(m3)
        assert _is_path(m3)

    def test_4x4_matrix_does_not_satisfy_is_path(self):
        # Rows of length 4 fail _is_path's point-length check (2 or 3).
        m4 = [[i * 4 + j for j in range(4)] for i in range(4)]
        assert _is_matrix(m4)
        assert not _is_path(m4)


class TestIsAffineMatrix:
    def test_3x3_identity_is_affine(self):
        assert _is_affine_matrix([[1, 0, 0], [0, 1, 0], [0, 0, 1]])

    def test_4x4_identity_is_affine(self):
        assert _is_affine_matrix(
            [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        )

    def test_3x3_translation_is_affine(self):
        assert _is_affine_matrix([[1, 0, 5], [0, 1, -2], [0, 0, 1]])

    def test_wrong_bottom_row_is_not_affine(self):
        assert not _is_affine_matrix([[1, 0, 0], [0, 1, 0], [0, 0, 2]])
        assert not _is_affine_matrix([[1, 0, 0], [0, 1, 0], [1, 0, 1]])

    def test_2x2_is_never_affine(self):
        # Too small to be a homogeneous 2D or 3D affine matrix.
        assert not _is_affine_matrix([[1, 0], [0, 1]])

    def test_5x5_is_never_affine(self):
        assert not _is_affine_matrix([[1 if i == j else 0 for j in range(5)]
                                       for i in range(5)])

    def test_non_square_is_not_affine(self):
        assert not _is_affine_matrix([[1, 0, 0], [0, 1, 0]])

    def test_every_affine_matrix_also_satisfies_is_matrix(self):
        m = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        assert _is_affine_matrix(m)
        assert _is_matrix(m)


class TestAffineReferenceShape:
    def test_2d_shape_is_unit_square(self):
        pts = _affine_reference_shape(3)
        assert len(pts) == 4
        for p in pts:
            assert len(p) == 2
            assert abs(abs(p[0]) - 0.5) < 1e-9
            assert abs(abs(p[1]) - 0.5) < 1e-9

    def test_3d_shape_is_unit_cube(self):
        pts = _affine_reference_shape(4)
        assert len(pts) == 8
        for p in pts:
            assert len(p) == 3
            for c in p:
                assert abs(abs(c) - 0.5) < 1e-9

    def test_2d_edges_form_a_closed_loop(self):
        edges = _affine_shape_edges(3)
        assert len(edges) == 4
        touched = sorted(i for e in edges for i in e)
        assert touched == [0, 0, 1, 1, 2, 2, 3, 3]

    def test_3d_edges_form_a_valid_cube_wireframe(self):
        edges = _affine_shape_edges(4)
        assert len(edges) == 12
        # Every corner touches exactly 3 edges in a cube.
        from collections import Counter
        counts = Counter(i for e in edges for i in e)
        assert set(counts.values()) == {3}
        assert set(counts.keys()) == set(range(8))


class TestApplyAffine:
    def test_identity_2d_leaves_points_unchanged(self):
        identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        pts = [[1, 2], [-3, 4]]
        out = _apply_affine(identity, pts)
        assert out == pts

    def test_translation_2d(self):
        m = [[1, 0, 2], [0, 1, 3], [0, 0, 1]]
        out = _apply_affine(m, [[0, 0], [1, 1]])
        assert out == [[2.0, 3.0], [3.0, 4.0]]

    def test_rotation_90deg_2d(self):
        m = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        out = _apply_affine(m, [[1, 0]])
        assert abs(out[0][0] - 0) < 1e-9
        assert abs(out[0][1] - 1) < 1e-9

    def test_scale_3d(self):
        m = [[2, 0, 0, 0], [0, 3, 0, 0], [0, 0, 4, 0], [0, 0, 0, 1]]
        out = _apply_affine(m, [[1, 1, 1]])
        assert out == [[2.0, 3.0, 4.0]]

    def test_mirror_flips_sign(self):
        m = [[-1, 0, 0], [0, 1, 0], [0, 0, 1]]
        out = _apply_affine(m, [[0.5, 0.5]])
        assert out == [[-0.5, 0.5]]

    def test_identity_3d_reference_shape_unchanged(self):
        identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        cube = _affine_reference_shape(4)
        out = _apply_affine(identity, cube)
        for p, o in zip(cube, out):
            assert all(abs(a - b) < 1e-9 for a, b in zip(p, o))


class TestIdentityMatrix:
    def test_3x3(self):
        assert _identity_matrix(3) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

    def test_4x4(self):
        assert _identity_matrix(4) == [
            [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0],
        ]


class TestTranslationMatrix:
    def test_2d(self):
        m = _translation_matrix([2, 3], 3).tolist()
        out = _apply_affine(m, [[0, 0]])
        assert out == [[2.0, 3.0]]

    def test_3d(self):
        m = _translation_matrix([1, 2, 3], 4).tolist()
        out = _apply_affine(m, [[0, 0, 0]])
        assert out == [[1.0, 2.0, 3.0]]


class TestAxisRotationMatrix:
    def test_2d_90deg(self):
        m = _axis_rotation_matrix(3, None, 90).tolist()
        out = _apply_affine(m, [[1, 0]])
        assert abs(out[0][0] - 0) < 1e-9
        assert abs(out[0][1] - 1) < 1e-9

    def test_3d_z_axis_90deg(self):
        m = _axis_rotation_matrix(4, 2, 90).tolist()
        out = _apply_affine(m, [[1, 0, 0]])
        assert abs(out[0][0] - 0) < 1e-9
        assert abs(out[0][1] - 1) < 1e-9
        assert abs(out[0][2] - 0) < 1e-9

    def test_3d_x_axis_90deg(self):
        m = _axis_rotation_matrix(4, 0, 90).tolist()
        out = _apply_affine(m, [[0, 1, 0]])
        assert abs(out[0][0] - 0) < 1e-9
        assert abs(out[0][1] - 0) < 1e-9
        assert abs(out[0][2] - 1) < 1e-9

    def test_3d_y_axis_90deg(self):
        m = _axis_rotation_matrix(4, 1, 90).tolist()
        out = _apply_affine(m, [[0, 0, 1]])
        assert abs(out[0][0] - 1) < 1e-9
        assert abs(out[0][1] - 0) < 1e-9
        assert abs(out[0][2] - 0) < 1e-9


class TestScaleMatrix:
    def test_2d(self):
        m = _scale_matrix([2, 3], 3).tolist()
        out = _apply_affine(m, [[1, 1]])
        assert out == [[2.0, 3.0]]

    def test_3d(self):
        m = _scale_matrix([2, 3, 4], 4).tolist()
        out = _apply_affine(m, [[1, 1, 1]])
        assert out == [[2.0, 3.0, 4.0]]


class TestShearMatrix:
    def test_2d_x_by_y(self):
        m = _shear_matrix(3, 0, 1, 2.0).tolist()
        out = _apply_affine(m, [[1, 3]])
        assert out == [[7.0, 3.0]]

    def test_3d_x_by_z(self):
        m = _shear_matrix(4, 0, 2, 1.5).tolist()
        out = _apply_affine(m, [[1, 0, 2]])
        assert out == [[4.0, 0.0, 2.0]]


class TestPivotAbout:
    def test_180deg_rotation_about_offset_center_2d(self):
        # 180deg rotation about (2, 0) maps (0, 0) -> (4, 0)
        rot = _axis_rotation_matrix(3, None, 180)
        piv = _pivot_about(rot, [2, 0], 3)
        out = _apply_affine(piv.tolist(), [[0, 0]])
        assert abs(out[0][0] - 4) < 1e-9
        assert abs(out[0][1] - 0) < 1e-9

    def test_180deg_rotation_about_offset_center_3d(self):
        rot = _axis_rotation_matrix(4, 2, 180)   # about Z
        piv = _pivot_about(rot, [2, 0, 0], 4)
        out = _apply_affine(piv.tolist(), [[0, 0, 5]])
        assert abs(out[0][0] - 4) < 1e-9
        assert abs(out[0][1] - 0) < 1e-9
        assert abs(out[0][2] - 5) < 1e-9   # Z untouched by a Z-axis rotation

    def test_scale_about_center_leaves_center_fixed(self):
        scale = _scale_matrix([2, 2], 3)
        piv = _pivot_about(scale, [1, 1], 3)
        out = _apply_affine(piv.tolist(), [[1, 1]])
        assert abs(out[0][0] - 1) < 1e-9
        assert abs(out[0][1] - 1) < 1e-9


class TestComposeAfter:
    def test_order_is_new_op_after_existing(self):
        # M = translate(1, 0); N = scale-by-2 about origin.
        # compose_after(N, M) means "N applied after M" -> translate then
        # scale: (0,0) -> (1,0) -> (2,0). The reversed order (scale then
        # translate) would give (1, 0) instead -- this is the
        # discriminating case that actually catches an order regression.
        m = _translation_matrix([1, 0], 3).tolist()
        n = _scale_matrix([2, 2], 3)
        out = _apply_affine(_compose_after(n, m), [[0, 0]])
        assert out == [[2.0, 0.0]]

    def test_identity_op_leaves_matrix_unchanged(self):
        m = [[1, 0, 5], [0, 1, 3], [0, 0, 1]]
        identity = np.array(_identity_matrix(3))
        assert _compose_after(identity, m) == [[1.0, 0.0, 5.0], [0.0, 1.0, 3.0], [0.0, 0.0, 1.0]]


class TestIterEnclosingLiterals:
    def test_plain_list_yields_once(self):
        text = "x = [1, 2, 3];"
        results = list(_iter_enclosing_literals(text, text.index("2")))
        assert results == [(4, 13, [1, 2, 3])]

    def test_nested_list_yields_inner_then_outer(self):
        text = "p = [[0,0],[1,0]];"
        offset = text.index("0,0") + 1  # inside the inner [0,0]
        results = list(_iter_enclosing_literals(text, offset))
        assert results == [(5, 10, [0, 0]), (4, 17, [[0, 0], [1, 0]])]

    def test_identifier_content_is_skipped_but_walk_continues(self):
        text = "f(x, [a, 1, 2]);"
        # cursor inside [a, 1, 2] -- fails literal_eval (identifier 'a'), no
        # enclosing bracket beyond it, so nothing is yielded
        offset = text.index("1")
        assert list(_iter_enclosing_literals(text, offset)) == []

    def test_walks_past_unparseable_levels_without_getting_stuck(self):
        # Both the inner [a, 1] and the outer [[a, 1], [2, 3]] embed the
        # identifier `a`, so neither parses -- the walk should skip both
        # and terminate cleanly (no enclosing bracket left), not raise or hang.
        text = "outer = [[a, 1], [2, 3]];"
        offset = text.index("[a, 1]") + 2
        assert list(_iter_enclosing_literals(text, offset)) == []

    def test_unbalanced_open_bracket_returns_nothing(self):
        text = "x = [1, 2"
        assert list(_iter_enclosing_literals(text, text.index("2"))) == []

    def test_cursor_between_two_literals_returns_nothing(self):
        text = "[1,2]) foo([3,4]"
        offset = text.index(") foo(") + 1
        assert list(_iter_enclosing_literals(text, offset)) == []

    def test_trailing_comma_parses(self):
        text = "[1, 2, 3,]"
        assert list(_iter_enclosing_literals(text, 5)) == [(0, 10, [1, 2, 3])]

    def test_multiline_literal(self):
        text = "path = [\n  [0, 0],\n  [1, 1]\n];"
        offset = text.index("1, 1")
        results = list(_iter_enclosing_literals(text, offset))
        assert results[0][2] == [1, 1]
        assert results[1][2] == [[0, 0], [1, 1]]

    def test_max_levels_exhausted(self):
        # 3 levels of nesting, but max_levels=1 only checks the innermost
        text = "[[[1,2]]]"
        offset = text.index("1")
        results = list(_iter_enclosing_literals(text, offset, max_levels=1))
        assert results == [(2, 7, [1, 2])]

    def test_openscad_range_syntax_fails_to_parse(self):
        text = "for (i = [0:5]) x;"
        offset = text.index("0:5")
        assert list(_iter_enclosing_literals(text, offset)) == []


class TestFindEditableLiterals:
    def test_matches_path(self):
        text = "path = [[0,0],[1,0],[2,1]];"
        offset = text.index("[1,0]") + 2
        result = find_editable_literals(text, offset)
        assert result["path"] == (7, 26, [[0, 0], [1, 0], [2, 1]])

    def test_walks_outward_past_inner_point_to_outer_path(self):
        text = "path = [[0,0],[1,0]];"
        offset = text.index("0,0") + 1  # cursor on the inner point
        result = find_editable_literals(text, offset)
        assert result["path"][2] == [[0, 0], [1, 0]]

    def test_matches_matrix(self):
        text = "m = [[1,0],[0,1]];"
        result = find_editable_literals(text, text.index("1,0"))
        assert result["matrix"][2] == [[1, 0], [0, 1]]

    def test_matches_affine(self):
        text = "m = [[1,0,0],[0,1,0],[0,0,1]];"
        result = find_editable_literals(text, text.index("0,1,0"))
        assert result["affine"][2] == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]

    def test_flat_scalar_list_does_not_match(self):
        text = "sizes = [1, 2, 3, 4];"
        assert find_editable_literals(text, text.index("2")) == {}

    def test_expression_content_does_not_match(self):
        text = "v = [x, 1, 2];"
        assert find_editable_literals(text, text.index("1")) == {}

    def test_grid_row_click_still_finds_outer_grid(self):
        # Regression test: a grid's own row is itself a valid Path (a list
        # of numeric points), so a single shared "first match wins" walk
        # (like the old find_editable_literal) would resolve "path" (the
        # row) before ever reaching "grid" (the whole structure) for any
        # click inside a row. Each shape must search independently.
        text = "grid = [[[0,0],[1,0]],[[0,1],[1,1]]];"
        offset = text.index("1,0")
        result = find_editable_literals(text, offset)
        assert result["path"][2] == [[0, 0], [1, 0]]
        assert result["grid"][2] == [[[0, 0], [1, 0]], [[0, 1], [1, 1]]]

    def test_no_enclosing_bracket_returns_none(self):
        text = "cube(10);"
        assert find_editable_literals(text, text.index("10")) == {}

    def test_matches_region_alongside_grid(self):
        # A 2-path 2D literal is both a valid Grid and a valid Region --
        # both should be offered, same precedent as the row-is-path case.
        text = "region = [[[10,0],[-10,10],[-10,-10]],[[3,0],[-3,3],[-3,-3]]];"
        result = find_editable_literals(text, text.index("10,0"))
        assert result["region"][2] == [[[10, 0], [-10, 10], [-10, -10]], [[3, 0], [-3, 3], [-3, -3]]]
        assert result["grid"][2] == result["region"][2]

    def test_single_path_region_does_not_match_grid(self):
        # The bare path (1 level in) matches "path"; the region as a
        # whole (1 level further out, a list containing that one path)
        # matches "region" but not "grid" (_is_grid needs >= 2 rows).
        text = "region = [[[10,0],[-10,10],[-10,-10]]];"
        result = find_editable_literals(text, text.index("10,0"))
        assert result["path"][2] == [[10, 0], [-10, 10], [-10, -10]]
        assert result["region"][2] == [[[10, 0], [-10, 10], [-10, -10]]]
        assert "grid" not in result


class TestFindViewableLiterals:
    def test_inner_point_click_finds_both_list_and_outer_path(self):
        # Regression test: `_is_list` is trivially true for any list, so a
        # single shared "first match wins" walk would get stuck on the inner
        # point and never reach the enclosing path. Each shape must search
        # independently so "path" is still found even though "list" resolves
        # to the inner point.
        text = "path = [[0,0],[1,0]];"
        offset = text.index("0,0") + 1
        result = find_viewable_literals(text, offset)
        assert result["list"] == (8, 13, [0, 0])
        assert result["path"] == (7, 20, [[0, 0], [1, 0]])
        assert "vnf" not in result
        assert "grid" not in result

    def test_outer_path_when_clicked_between_points(self):
        text = "path = [[0,0], [1,0]];"
        offset = text.index("], [") + 1
        result = find_viewable_literals(text, offset)
        assert result["list"][2] == [[0, 0], [1, 0]]
        assert result["path"][2] == [[0, 0], [1, 0]]

    def test_vnf_shape_matches(self):
        # Click on the top-level comma separating the verts-list from the
        # faces-list -- the innermost enclosing bracket there is the whole
        # VNF pair, not either inner sublist.
        text = "v = [[[0,0,0],[1,0,0],[0,1,0]],[[0,1,2]]];"
        offset = text.index("],[[0,1,2]") + 1
        result = find_viewable_literals(text, offset)
        assert result["vnf"][2] == [[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]]]

    def test_expression_content_finds_nothing(self):
        text = "v = [x, 1, 2];"
        assert find_viewable_literals(text, text.index("1")) == {}

    def test_region_shape_matches_alongside_grid(self):
        text = "region = [[[10,0],[-10,10],[-10,-10]],[[3,0],[-3,3],[-3,-3]]];"
        result = find_viewable_literals(text, text.index("10,0"))
        assert result["region"][2] == [[[10, 0], [-10, 10], [-10, -10]], [[3, 0], [-3, 3], [-3, -3]]]
        assert result["grid"][2] == result["region"][2]


class TestKeyNudgeMagnitude:
    """Arrow-key vertex nudging's step size, shared by every editable
    viewport's keyPressEvent: 1 unit by default, 0.1 with Cmd (Control on
    macOS) held, 10 with Shift held."""

    def test_no_modifier_is_default_unit(self):
        assert _key_nudge_magnitude(Qt.KeyboardModifier.NoModifier) == 1.0

    def test_control_modifier_is_fine_nudge(self):
        assert _key_nudge_magnitude(Qt.KeyboardModifier.ControlModifier) == 0.1

    def test_shift_modifier_is_coarse_nudge(self):
        assert _key_nudge_magnitude(Qt.KeyboardModifier.ShiftModifier) == 10.0


class TestKeyNudgeDelta:
    def test_default_magnitude_is_one_unit(self):
        cam = Camera()
        delta = _key_nudge_delta(cam, 2, Qt.Key.Key_Right)
        assert delta is not None
        assert np.count_nonzero(delta) == 1
        assert abs(np.abs(delta).max() - 1.0) < 1e-9

    def test_magnitude_param_scales_delta(self):
        cam = Camera()
        fine = _key_nudge_delta(cam, 2, Qt.Key.Key_Right, magnitude=0.1)
        coarse = _key_nudge_delta(cam, 2, Qt.Key.Key_Right, magnitude=10.0)
        assert abs(np.abs(fine).max() - 0.1) < 1e-9
        assert abs(np.abs(coarse).max() - 10.0) < 1e-9

    def test_unrecognized_key_returns_none_regardless_of_magnitude(self):
        cam = Camera()
        assert _key_nudge_delta(cam, 2, Qt.Key.Key_A, magnitude=0.1) is None


class TestClassifyNodeType:
    """PathViewer's bezier node-type auto-detect classifier: "symmetric" if
    both handles are opposite-direction and equidistant from v0, "same_angle"
    if opposite-direction but different distance, else "disjointed"."""

    def test_symmetric_equidistant_opposite(self):
        v0 = np.array([0.0, 0.0, 0.0])
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([-1.0, 0.0, 0.0])
        assert _classify_node_type(v0, a, b) == "symmetric"

    def test_same_angle_opposite_different_distance(self):
        v0 = np.array([0.0, 0.0, 0.0])
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([-2.0, 0.0, 0.0])
        assert _classify_node_type(v0, a, b) == "same_angle"

    def test_disjointed_not_opposite_direction(self):
        v0 = np.array([0.0, 0.0, 0.0])
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([0.0, 1.0, 0.0])
        assert _classify_node_type(v0, a, b) == "disjointed"

    def test_disjointed_missing_handle(self):
        v0 = np.array([0.0, 0.0, 0.0])
        a = np.array([1.0, 0.0, 0.0])
        assert _classify_node_type(v0, None, a) == "disjointed"
        assert _classify_node_type(v0, a, None) == "disjointed"
        assert _classify_node_type(v0, None, None) == "disjointed"

    def test_disjointed_zero_length_handle(self):
        v0 = np.array([0.0, 0.0, 0.0])
        a = np.array([0.0, 0.0, 0.0])  # coincident with v0
        b = np.array([-1.0, 0.0, 0.0])
        assert _classify_node_type(v0, a, b) == "disjointed"


class TestRemapNodeTypes:
    def test_shifts_surviving_indices(self):
        result = _remap_node_types({0: "a", 3: "b", 6: "c"}, {0: 0, 6: 3})
        assert result == {0: "a", 3: "c"}

    def test_empty_map_drops_everything(self):
        assert _remap_node_types({0: "a", 3: "b"}, {}) == {}

    def test_empty_types_stays_empty(self):
        assert _remap_node_types({}, {0: 0}) == {}


class TestBezierLinkedMoves:
    """Unified drag+nudge linking: dragging/nudging a v0 always
    rigid-translates its adjacent handles; dragging/nudging a v1/v2 links
    its opposite-side handle sibling per the owning v0's node type."""

    # Open path, 2 segments: v0=0, C1=1, C2=2, v0=3, C1=4, C2=5, v0=6
    OPEN_PTS = np.array([
        [0.0, 0.0, 0.0], [1.0, 1.0, 0.0], [2.0, -1.0, 0.0],
        [3.0, 0.0, 0.0], [4.0, 1.0, 0.0], [5.0, -1.0, 0.0], [6.0, 0.0, 0.0],
    ])

    def test_v0_drag_rigidly_moves_both_neighbors(self):
        new_pos = self.OPEN_PTS[3] + np.array([0.5, 0.5, 0.0])
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 3, new_pos, "disjointed")
        moved = dict(moves)
        assert set(moved) == {2, 3, 4}
        delta = new_pos - self.OPEN_PTS[3]
        assert np.allclose(moved[2], self.OPEN_PTS[2] + delta)
        assert np.allclose(moved[4], self.OPEN_PTS[4] + delta)

    def test_v0_at_open_path_start_has_only_forward_neighbor(self):
        new_pos = self.OPEN_PTS[0] + np.array([1.0, 0.0, 0.0])
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 0, new_pos, "disjointed")
        assert set(dict(moves)) == {0, 1}

    def test_v0_at_open_path_end_has_only_preceding_neighbor(self):
        new_pos = self.OPEN_PTS[6] + np.array([1.0, 0.0, 0.0])
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 6, new_pos, "disjointed")
        assert set(dict(moves)) == {5, 6}

    def test_v0_rigid_link_wraps_on_closed_path(self):
        # Closed, 1 segment: v0=0, C1=1, C2=2 (wraps back to v0=0)
        pts = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.0], [-1.0, 1.0, 0.0]])
        new_pos = pts[0] + np.array([0.0, 1.0, 0.0])
        moves = _bezier_linked_moves(pts, True, 0, new_pos, "disjointed")
        assert set(dict(moves)) == {0, 1, 2}

    def test_v1_symmetric_mirrors_new_distance(self):
        new_v1 = np.array([4.5, 2.0, 0.0])
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 4, new_v1, "symmetric")
        moved = dict(moves)
        v0 = self.OPEN_PTS[3]
        assert np.allclose(moved[2], v0 + (v0 - new_v1))

    def test_v1_same_angle_preserves_partners_own_distance(self):
        new_v1 = np.array([4.5, 2.0, 0.0])
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 4, new_v1, "same_angle")
        moved = dict(moves)
        v0 = self.OPEN_PTS[3]
        partner_old = self.OPEN_PTS[2]
        expected_dist = np.linalg.norm(partner_old - v0)
        assert np.isclose(np.linalg.norm(moved[2] - v0), expected_dist)
        # mirrored direction: (moved[2] - v0) should point opposite to (new_v1 - v0)
        assert np.dot(moved[2] - v0, new_v1 - v0) < 0

    def test_v1_disjointed_moves_only_itself(self):
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 4, np.array([4.5, 2.0, 0.0]), "disjointed")
        assert set(dict(moves)) == {4}

    def test_v1_at_open_path_start_has_no_partner(self):
        # index 1 is the first segment's C1; its "p2" partner (index -1) doesn't exist
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 1, np.array([1.1, 1.1, 0.0]), "symmetric")
        assert set(dict(moves)) == {1}

    def test_v2_at_open_path_end_has_no_partner(self):
        # index 5 is the last segment's C2; its "n1" partner (index 7) doesn't exist
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 5, np.array([5.1, -1.1, 0.0]), "symmetric")
        assert set(dict(moves)) == {5}

    def test_v2_symmetric_mirrors_through_next_v0(self):
        new_v2 = np.array([2.5, -2.0, 0.0])
        moves = _bezier_linked_moves(self.OPEN_PTS, False, 2, new_v2, "symmetric")
        moved = dict(moves)
        n0 = self.OPEN_PTS[3]  # v2's owning "next v0"
        assert np.allclose(moved[4], n0 + (n0 - new_v2))

    def test_partner_wraps_on_closed_path(self):
        # Closed, 1 segment: v0=0, C1=1, C2=2 -- v1(idx1)'s partner is idx2.
        pts = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.0], [-1.0, 1.0, 0.0]])
        new_v1 = np.array([1.5, 1.5, 0.0])
        moves = _bezier_linked_moves(pts, True, 1, new_v1, "symmetric")
        assert set(dict(moves)) == {1, 2}


class TestDecasteljauSplit:
    """De Casteljau curve-preserving split, used by bezier-mode Add Vertex
    -- the new on-curve vertex f must land exactly on the original curve,
    and both resulting half-segments must retrace it exactly."""

    P0 = np.array([0.0, 0.0, 0.0])
    C1 = np.array([1.0, 2.0, 0.0])
    C2 = np.array([3.0, 2.0, 0.0])
    P3 = np.array([4.0, 0.0, 0.0])

    @staticmethod
    def _bernstein(p0, c1, c2, p3, t):
        omt = 1 - t
        return omt**3 * p0 + 3 * omt**2 * t * c1 + 3 * omt * t**2 * c2 + t**3 * p3

    def test_f_lands_exactly_on_original_curve(self):
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            _, _, f, _, _ = _decasteljau_split(self.P0, self.C1, self.C2, self.P3, t)
            assert np.allclose(f, self._bernstein(self.P0, self.C1, self.C2, self.P3, t))

    def test_first_half_retraces_original_curve(self):
        t_split = 0.5
        a, d, f, e, c = _decasteljau_split(self.P0, self.C1, self.C2, self.P3, t_split)
        for s in np.linspace(0.0, 1.0, 11):
            got = self._bernstein(self.P0, a, d, f, s)
            expected = self._bernstein(self.P0, self.C1, self.C2, self.P3, s * t_split)
            assert np.allclose(got, expected, atol=1e-9)

    def test_second_half_retraces_original_curve(self):
        t_split = 0.5
        a, d, f, e, c = _decasteljau_split(self.P0, self.C1, self.C2, self.P3, t_split)
        for s in np.linspace(0.0, 1.0, 11):
            got = self._bernstein(f, e, c, self.P3, s)
            expected = self._bernstein(self.P0, self.C1, self.C2, self.P3, t_split + s * (1 - t_split))
            assert np.allclose(got, expected, atol=1e-9)

    def test_t_zero_degenerates_to_p0(self):
        a, d, f, e, c = _decasteljau_split(self.P0, self.C1, self.C2, self.P3, 0.0)
        assert np.allclose(a, self.P0)
        assert np.allclose(d, self.P0)
        assert np.allclose(f, self.P0)

    def test_t_one_degenerates_to_p3(self):
        a, d, f, e, c = _decasteljau_split(self.P0, self.C1, self.C2, self.P3, 1.0)
        assert np.allclose(f, self.P3)
        assert np.allclose(e, self.P3)
        assert np.allclose(c, self.P3)


class TestV0HandleIndices:
    def test_open_path_interior_has_both(self):
        assert _v0_handle_indices(3, 7, False) == (2, 4)

    def test_open_path_start_has_no_preceding(self):
        assert _v0_handle_indices(0, 7, False) == (None, 1)

    def test_open_path_end_has_no_following(self):
        assert _v0_handle_indices(6, 7, False) == (5, None)

    def test_closed_path_wraps(self):
        assert _v0_handle_indices(0, 6, True) == (5, 1)


class TestSnapHandlesToNodeType:
    """Setting a node type via the context menu immediately brings both
    handles into line with it, rather than waiting for the next drag."""

    V0 = np.array([0.0, 0.0, 0.0])
    HANDLE_A = np.array([2.0, 1.0, 0.0])    # len ~2.236
    HANDLE_B = np.array([-3.0, -0.5, 0.0])  # len ~3.041

    def test_disjointed_returns_none(self):
        assert _snap_handles_to_node_type(self.V0, self.HANDLE_A, self.HANDLE_B, "disjointed") is None

    def test_missing_handle_returns_none(self):
        assert _snap_handles_to_node_type(self.V0, self.HANDLE_A, None, "symmetric") is None
        assert _snap_handles_to_node_type(self.V0, None, self.HANDLE_B, "symmetric") is None

    def test_symmetric_averages_angle_and_distance(self):
        new_a, new_b = _snap_handles_to_node_type(self.V0, self.HANDLE_A, self.HANDLE_B, "symmetric")
        len_a, len_b = np.linalg.norm(new_a - self.V0), np.linalg.norm(new_b - self.V0)
        assert np.isclose(len_a, len_b)
        assert np.allclose((new_a - self.V0) + (new_b - self.V0), 0.0)

    def test_same_angle_averages_angle_only_preserves_distances(self):
        new_a, new_b = _snap_handles_to_node_type(self.V0, self.HANDLE_A, self.HANDLE_B, "same_angle")
        assert np.isclose(np.linalg.norm(new_a - self.V0), np.linalg.norm(self.HANDLE_A - self.V0))
        assert np.isclose(np.linalg.norm(new_b - self.V0), np.linalg.norm(self.HANDLE_B - self.V0))
        dir_a = (new_a - self.V0) / np.linalg.norm(new_a - self.V0)
        dir_b = (new_b - self.V0) / np.linalg.norm(new_b - self.V0)
        assert np.allclose(dir_a + dir_b, 0.0)

    def test_already_opposite_pair_is_idempotent(self):
        new_a, new_b = _snap_handles_to_node_type(self.V0, self.HANDLE_A, self.HANDLE_B, "symmetric")
        new_a2, new_b2 = _snap_handles_to_node_type(self.V0, new_a, new_b, "symmetric")
        assert np.allclose(new_a2, new_a)
        assert np.allclose(new_b2, new_b)

    def test_zero_length_handle_returns_none(self):
        assert _snap_handles_to_node_type(self.V0, self.V0.copy(), self.HANDLE_B, "symmetric") is None

    def test_same_direction_handles_returns_none(self):
        # Both handles on the same side of v0 -- opposing average is
        # ill-defined (degenerate), so this must no-op rather than guess.
        same_dir_b = np.array([4.0, 2.0, 0.0])  # same direction as HANDLE_A, different length
        assert _snap_handles_to_node_type(self.V0, self.HANDLE_A, same_dir_b, "symmetric") is None


class TestFitMergedSegment:
    """Deleting an on-curve bezier vertex merges its two adjacent segments
    into one, least-squares fit to approximate the shape of both."""

    @staticmethod
    def _bernstein(p0, c1, c2, p3, t):
        omt = 1 - t
        return omt**3 * p0 + 3 * omt**2 * t * c1 + 3 * omt * t**2 * c2 + t**3 * p3

    def test_exact_recovery_when_segments_came_from_one_cubic(self):
        # Split a single cubic in two (De Casteljau), then fit a merged
        # segment back from the two halves -- since an exact single-cubic
        # representation exists, least-squares must recover it exactly.
        p0 = np.array([0.0, 0.0, 0.0])
        orig_c1 = np.array([1.0, 3.0, 0.0])
        orig_c2 = np.array([4.0, 3.0, 0.0])
        p3 = np.array([5.0, 0.0, 0.0])
        a, d, f, e, c = _decasteljau_split(p0, orig_c1, orig_c2, p3, 0.5)
        new_c1, new_c2 = _fit_merged_segment(p0, a, d, f, e, c, p3, samples=64)
        assert np.allclose(new_c1, orig_c1, atol=1e-6)
        assert np.allclose(new_c2, orig_c2, atol=1e-6)

    def test_fit_beats_naive_outer_handle_fallback(self):
        # Two segments that DON'T come from one cubic -- the least-squares
        # fit should still approximate the combined curve much better than
        # just keeping the two outer (unrelated) handles unchanged.
        p0 = np.array([0.0, 0.0, 0.0])
        c1 = np.array([1.0, 2.0, 0.0])
        c2 = np.array([2.0, 2.0, 0.0])
        v0 = np.array([3.0, 0.0, 0.0])
        c3 = np.array([4.0, -2.0, 0.0])
        c4 = np.array([5.0, -2.0, 0.0])
        p3 = np.array([6.0, 0.0, 0.0])
        new_c1, new_c2 = _fit_merged_segment(p0, c1, c2, v0, c3, c4, p3)

        def orig_curve(t):
            return (self._bernstein(p0, c1, c2, v0, t * 2) if t <= 0.5
                    else self._bernstein(v0, c3, c4, p3, (t - 0.5) * 2))

        ts = np.linspace(0.0, 1.0, 50)
        fit_resid = sum(np.linalg.norm(self._bernstein(p0, new_c1, new_c2, p3, t) - orig_curve(t)) ** 2 for t in ts)
        naive_resid = sum(np.linalg.norm(self._bernstein(p0, c1, c4, p3, t) - orig_curve(t)) ** 2 for t in ts)
        assert fit_resid < naive_resid

    def test_endpoints_unchanged(self):
        p0 = np.array([0.0, 0.0, 0.0])
        c1 = np.array([1.0, 2.0, 0.0])
        c2 = np.array([2.0, 2.0, 0.0])
        v0 = np.array([3.0, 0.0, 0.0])
        c3 = np.array([4.0, -2.0, 0.0])
        c4 = np.array([5.0, -2.0, 0.0])
        p3 = np.array([6.0, 0.0, 0.0])
        new_c1, new_c2 = _fit_merged_segment(p0, c1, c2, v0, c3, c4, p3)
        # The fitted segment must still start at p0 and end at p3 exactly
        # (endpoints are never solved for, only C1/C2).
        assert np.allclose(self._bernstein(p0, new_c1, new_c2, p3, 0.0), p0)
        assert np.allclose(self._bernstein(p0, new_c1, new_c2, p3, 1.0), p3)


class TestOwningV0Index:
    """Regression coverage for a real bug: the control point *before* an
    on-curve vertex (v2, idx % 3 == 2) belongs to the *following* v0 for
    node-type/linking purposes, not the v0 its own segment starts from --
    a caller computing this via a uniform `idx - idx % 3` formula (correct
    for v0 and v1, wrong for v2) silently looked up the wrong v0's type,
    making a v2 drag always behave as "disjointed" regardless of its
    actual owning v0's type."""

    # Open path, 2 segments: v0=0, C1=1, C2=2, v0=3, C1=4, C2=5, v0=6
    N = 7

    def test_v0_owns_itself(self):
        assert _owning_v0_index(0, self.N, False) == 0
        assert _owning_v0_index(3, self.N, False) == 3

    def test_v1_owned_by_preceding_v0(self):
        assert _owning_v0_index(1, self.N, False) == 0
        assert _owning_v0_index(4, self.N, False) == 3

    def test_v2_owned_by_following_v0_not_preceding(self):
        # This is the exact case that was broken: idx=2's naive
        # `idx - idx % 3` would give 0 (wrong); the correct owner is 3.
        assert _owning_v0_index(2, self.N, False) == 3
        assert _owning_v0_index(5, self.N, False) == 6

    def test_v2_wraps_on_closed_path(self):
        # Closed, 2 segments: v0=0, C1=1, C2=2, v0=3, C1=4, C2=5 (wraps to v0=0)
        assert _owning_v0_index(5, 6, True) == 0
