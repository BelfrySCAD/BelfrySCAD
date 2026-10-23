"""Bezier mode's Degree is bezier_path()'s N: a quadratic is drawn as
quadratic segments (only cubic gets Bezier editing; see load_path)."""
import numpy as np

from belfryscad.window.data_viewer_path import _PathViewport


def test_quadratic_segments_pass_through_every_second_point():
    pts = np.array([[0, 0, 0], [1, 2, 0], [2, 0, 0], [3, -2, 0], [4, 0, 0]], dtype=np.float32)
    pairs = _PathViewport._tessellate_bezier(pts, False, steps=4, deg=2)
    assert len(pairs) == 2 * 4 * 2                     # two segments, four steps each
    assert np.allclose(pairs[0], pts[0]) and np.allclose(pairs[7], pts[2])
    assert np.allclose(pairs[-1], pts[4])
    # The midpoint of a quadratic (P0, C, P2) is P0/4 + C/2 + P2/4.
    assert np.allclose(pairs[3], pts[0] / 4 + pts[1] / 2 + pts[2] / 4)


def test_cubic_is_unchanged():
    pts = np.array([[0, 0, 0], [1, 2, 0], [3, 2, 0], [4, 0, 0]], dtype=np.float32)
    pairs = _PathViewport._tessellate_bezier(pts, False, steps=2)
    assert np.allclose(pairs[1], 0.125 * pts[0] + 0.375 * pts[1] + 0.375 * pts[2] + 0.125 * pts[3])


def _unit(v):
    return v / np.linalg.norm(v)


def test_degree4_collinear_lock_turns_the_partner_and_keeps_its_distance():
    from belfryscad.window.data_viewer_path import _collinear_moves
    pts = np.array([[i, 0, 0] for i in range(9)], dtype=np.float64)   # on-curve at 0, 4, 8
    moves = dict(_collinear_moves(pts, False, 3, np.array([3.0, 1.0, 0.0]), 4, {4}))
    assert set(moves) == {3, 5}
    assert np.allclose(_unit(moves[5] - pts[4]), _unit(pts[4] - moves[3]))   # collinear through 4
    assert np.isclose(np.linalg.norm(moves[5] - pts[4]), 1.0)                 # own distance kept


def test_degree2_partner_meets_the_next_points_tangent():
    from belfryscad.window.data_viewer_path import _collinear_moves
    # On-curve 0, 2, 4, 6; controls 1, 3, 5. Points 2 and 4 locked; 4's
    # tangent tilts up-left, so the new tangent at 2 meets it at (3, 1.5).
    pts = np.array([[0, 0, 0], [1, 1, 0], [2, 1, 0], [3, 1, 0], [4, 1, 0], [5, 0.5, 0], [6, 0, 0]],
                   dtype=np.float64)
    moves = dict(_collinear_moves(pts, False, 1, np.array([1.0, 0.5, 0.0]), 2, {2, 4}))
    p = moves[3]
    assert np.allclose(_unit(p - pts[2]), _unit(pts[2] - moves[1]))          # collinear through 2
    assert np.allclose(_unit(pts[4] - p), _unit(pts[5] - pts[4]))            # still collinear through 4
    assert np.allclose(p, [3.0, 1.5, 0.0])
    assert 4 not in moves and 5 not in moves                                 # nothing further moved


def test_degree2_falls_back_to_keeping_distance_when_the_tangents_cannot_meet():
    from belfryscad.window.data_viewer_path import _collinear_moves
    pts = np.array([[0, 0, 0], [1, 1, 0], [2, 1, 0], [3, 1, 0], [4, 1, 0], [5, 1, 0], [6, 0, 0]], dtype=np.float64)
    # Dragged handle on the far side makes the new tangent point away from 4's.
    moves = dict(_collinear_moves(pts, False, 1, np.array([1.0, -1.0, 0.0]), 2, {2, 4}))
    assert np.isclose(np.linalg.norm(moves[3] - pts[2]), 1.0)
