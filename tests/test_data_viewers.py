"""
Tests for the GridViewer shape-detection and flat-index helpers in
`belfryscad.window.data_viewers` — specifically that a "grid" (list of lists
of points) need not be rectangular: rows may have different lengths (e.g. a
cone's single-point apex row next to a wider base row). Also covers
`_is_matrix`/`_is_affine_matrix`, the shape-detection helpers for
MatrixViewer/AffineMatrixViewer, and the affine-transform math helpers.

These test the pure-Python helpers only (`_is_grid`, `_grid_row_offsets`,
`_grid_flat_to_rc`, `_grid_is_triangular`, `_grid_fan_spec`, `_is_matrix`,
`_is_affine_matrix`, `_affine_reference_shape`, `_affine_shape_edges`,
`_apply_affine`); the Qt/OpenGL rendering classes (`GridViewer`,
`_GridViewport`, `MatrixViewer`, `AffineMatrixViewer`, `_AffineViewport`)
aren't covered here, consistent with the rest of the test suite (no
existing tests instantiate Qt widgets).
"""
from belfryscad.window.data_viewers import (
    _is_grid, _grid_row_offsets, _grid_flat_to_rc, _grid_is_triangular,
    _grid_fan_spec, _is_matrix, _is_path, _is_affine_matrix,
    _affine_reference_shape, _affine_shape_edges, _apply_affine,
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
