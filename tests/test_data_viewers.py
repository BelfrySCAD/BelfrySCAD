"""
Tests for the GridViewer shape-detection and flat-index helpers in
`belfryscad.window.data_viewers` — specifically that a "grid" (list of lists
of points) need not be rectangular: rows may have different lengths (e.g. a
cone's single-point apex row next to a wider base row).

These test the pure-Python helpers only (`_is_grid`, `_grid_row_offsets`,
`_grid_flat_to_rc`, `_grid_is_triangular`); the Qt/OpenGL rendering classes
(`GridViewer`, `_GridViewport`) aren't covered here, consistent with the
rest of the test suite (no existing tests instantiate Qt widgets).
"""
from belfryscad.window.data_viewers import (
    _is_grid, _grid_row_offsets, _grid_flat_to_rc, _grid_is_triangular,
    _grid_fan_spec,
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
