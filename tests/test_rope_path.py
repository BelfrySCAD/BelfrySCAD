"""Fixed Length path editing (#947): the drag solve."""
import numpy as np

from belfryscad.window.data_viewer_path import _rope_moves, _segment_lengths, _slide_moves

# Five points, every segment 10 long.
ZIGZAG = np.array([[0, 0, 0], [6, -8, 0], [0, -16, 0], [6, -24, 0], [0, -32, 0]], float)
L = _segment_lengths(ZIGZAG)


def drag(pins, i, target, pts=ZIGZAG):
    out = pts.copy()
    for k, p in _rope_moves(pts, L, pins, i, np.array(target, float)):
        out[k] = p
    assert np.allclose(_segment_lengths(out), L, atol=1e-7)
    return out


def test_unpinned_tail_trails_behind():
    out = drag(set(), 2, [10, -14, 0])
    assert np.allclose(out[0], ZIGZAG[0])               # the first point never moves
    assert np.allclose(out[2], [10, -14, 0], atol=1e-6)


def test_points_beyond_a_pin_stay_put():
    out = drag({4}, 2, [3, -16, 0])
    assert np.allclose(out[2], [3, -16, 0], atol=1e-6)
    out = drag({3}, 1, [2, -6, 0])
    assert np.allclose(out[3:], ZIGZAG[3:])


def test_one_link_to_a_pin_keeps_its_length():
    # Point 2 is one link from pin 3, so it can only be 10 from it: it
    # lands as near the target as that allows.
    out = drag({3}, 2, [3, -16, 0])
    assert np.isclose(np.linalg.norm(out[2] - out[3]), 10)


def test_pulled_past_straight_stops_there():
    out = drag({4}, 2, [40, -16, 0])
    assert np.allclose(out[2], [12, -16, 0], atol=1e-6)   # both halves pulled straight


def test_pinned_and_first_points_do_not_drag():
    assert _rope_moves(ZIGZAG, L, {3}, 3, [1, 1, 0]) == []
    assert _rope_moves(ZIGZAG, L, set(), 0, [1, 1, 0]) == []


def test_slide_moves_a_point_along_a_straight_path():
    line = np.array([[0, 0, 0], [0, -10, 0], [0, -30, 0]], float)
    lengths = _segment_lengths(line)
    [(i, p)] = _slide_moves(line, lengths, set(), 1, [4, -22, 0])
    assert i == 1 and np.allclose(p, [0, -22, 0])        # straight on, 22 along
    [(_, p)] = _slide_moves(line, lengths, set(), 1, [0, -50, 0])
    assert np.allclose(p, [0, -30, 0])                   # no further than its neighbour


def test_slide_keeps_the_total_on_a_bend():
    [(i, p)] = _slide_moves(ZIGZAG, L, set(), 2, [2, -20, 0])
    out = ZIGZAG.copy()
    out[i] = p
    assert np.isclose(_segment_lengths(out).sum(), L.sum())
    assert np.allclose(np.delete(out, 2, 0), np.delete(ZIGZAG, 2, 0))


def test_slide_leaves_ends_and_pins_alone():
    assert _slide_moves(ZIGZAG, L, set(), 0, [1, 1, 0]) == []
    assert _slide_moves(ZIGZAG, L, set(), 4, [1, 1, 0]) == []
    assert _slide_moves(ZIGZAG, L, {2}, 2, [1, 1, 0]) == []
