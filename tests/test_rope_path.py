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


# --- Bezier mode: every segment keeps its ARC length -------------------------

from belfryscad.window.data_viewer_path import (  # noqa: E402
    _bezier_arc_lengths, _bezier_rope_moves, _cubic_arc_length, _fit_handles_to_length)

# Three curved cubic segments, open; on-curve points 0, 3, 6, 9.
BEZ = np.array([[0, 0, 0], [4, 3, 0], [8, 3, 0], [12, 0, 0],
                [16, -3, 0], [20, -3, 0], [24, 0, 0],
                [28, 3, 0], [32, 3, 0], [36, 0, 0]], float)
ARCS = _bezier_arc_lengths(BEZ)
CHORDS = _segment_lengths(BEZ[0::3])
SMOOTH = {3, 6}


def bez_drag(pins, i, target, smooth=SMOOTH):
    out = BEZ.copy()
    for k, p in _bezier_rope_moves(BEZ, CHORDS, ARCS, pins, smooth, i, np.array(target, float)):
        out[k] = p
    assert np.allclose(_bezier_arc_lengths(out), ARCS, atol=1e-7)
    return out


def tangent_turn(pts, v):
    a, b = pts[v] - pts[v - 1], pts[v + 1] - pts[v]
    return abs(a[0] * b[1] - a[1] * b[0]) / (np.linalg.norm(a) * np.linalg.norm(b))


def test_arc_length_of_a_straight_cubic_is_its_chord():
    p = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0]], float)
    assert np.isclose(_cubic_arc_length(*p), 3)


def test_fit_handles_reaches_a_longer_and_a_shorter_length():
    p0, c1, c2, p3 = BEZ[:4]
    for want in (ARCS[0] * 1.2, (ARCS[0] + 12) / 2):
        a, b = _fit_handles_to_length(p0, c1, c2, p3, want)
        assert np.isclose(_cubic_arc_length(p0, a, b, p3), want)


def test_on_curve_drag_keeps_every_arc_length_and_stays_smooth():
    out = bez_drag(set(), 6, [20, 6, 0])
    assert np.allclose(out[0], BEZ[0])                       # point 0 never moves
    assert np.allclose(out[6], [20, 6, 0], atol=1e-6)
    for v in SMOOTH:
        assert tangent_turn(out, v) < 1e-9


def test_beyond_a_pin_stays_put():
    out = bez_drag({6}, 3, [12, 4, 0])
    assert np.allclose(out[6], BEZ[6])
    assert np.allclose(out[8:], BEZ[8:])


def test_a_corner_may_kink():
    out = bez_drag(set(), 6, [20, 6, 0], smooth=set())
    assert tangent_turn(out, 3) > 1e-3


def test_handle_drag_turns_it_and_its_partner_without_moving_points():
    out = bez_drag(set(), 4, [16, 0, 0])
    assert np.allclose(out[0::3], BEZ[0::3])                 # on-curve points stay
    assert tangent_turn(out, 3) < 1e-9                       # partner followed
    d = out[4] - out[3]
    assert np.allclose(d / np.linalg.norm(d), [1, 0, 0])     # the cursor's direction


def test_handle_drag_at_a_corner_leaves_the_partner():
    out = bez_drag(set(), 4, [16, 0, 0], smooth=set())
    assert np.allclose(out[2], BEZ[2])


def test_pinned_and_first_points_do_not_drag_in_bezier_mode():
    assert _bezier_rope_moves(BEZ, CHORDS, ARCS, {6}, SMOOTH, 6, [1, 1, 0]) == []
    assert _bezier_rope_moves(BEZ, CHORDS, ARCS, set(), SMOOTH, 0, [1, 1, 0]) == []


# --- Bezier mode: pulled past straight chords, and Option-drag slides ---------

from belfryscad.window.data_viewer_path import _bezier_slide_moves  # noqa: E402


def test_pulled_past_straight_chords_the_curves_straighten():
    # The chords alone reach 36 along X; the arcs reach further.
    far = [ARCS.sum() + 5, 0, 0]
    out = bez_drag(set(), 9, far)
    reach = float(np.linalg.norm(out[9] - out[0]))
    assert reach > CHORDS.sum() + 1                       # past chord reach
    assert reach < ARCS.sum() + 1e-6                      # never past the arcs
    assert np.allclose(out[0::3, 1], 0, atol=1e-3)        # in a line


def test_straightening_only_takes_what_reaching_needs():
    want = (CHORDS.sum() + ARCS.sum()) / 2
    out = bez_drag(set(), 9, [want, 0, 0])
    assert np.allclose(out[9], [want, 0, 0], atol=1e-6)


def test_a_straightened_curve_bows_again_when_slack():
    taut = bez_drag(set(), 9, [ARCS.sum() + 5, 0, 0])
    out = taut.copy()      # off the line: a dead-straight chain can't fold (FABRIK)
    for k, p in _bezier_rope_moves(taut, CHORDS, ARCS, set(), SMOOTH, 9, np.array([30.0, 2, 0])):
        out[k] = p
    assert np.allclose(_bezier_arc_lengths(out), ARCS, atol=1e-7)
    assert np.allclose(out[9], [30, 2, 0], atol=1e-6)


def slide(i, target, pins=frozenset()):
    out = BEZ.copy()
    for k, p in _bezier_slide_moves(BEZ, ARCS, set(pins), SMOOTH, i, np.array(target, float)):
        out[k] = p
    return out


def test_slide_trades_arc_length_and_keeps_the_total():
    out = slide(3, [18, -2, 0])
    arcs = _bezier_arc_lengths(out)
    assert np.isclose(arcs.sum(), ARCS.sum(), atol=1e-7)
    assert arcs[0] > ARCS[0] + 1 and np.isclose(arcs[2], ARCS[2], atol=1e-7)
    assert np.allclose(out[[0, 6, 9]], BEZ[[0, 6, 9]])     # other on-curve points stay
    assert np.allclose(out[7:], BEZ[7:])                   # and the segment beyond
    for v in SMOOTH:
        assert tangent_turn(out, v) < 1e-9


def test_slide_leaves_ends_handles_and_pins_alone():
    for i, pins in ((0, ()), (9, ()), (4, ()), (3, (3,))):
        assert _bezier_slide_moves(BEZ, ARCS, set(pins), SMOOTH, i, np.array([5.0, 5, 0])) == []
