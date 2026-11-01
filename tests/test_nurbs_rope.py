"""Fixed Length in the path editor's NURBS modes (#947)."""
import numpy as np
import pytest

from belfryscad import nurbs
from belfryscad.nurbs_rope import _deriv_basis, _deriv_matrix, arc_lengths, rope_moves, slide_target

WAVE = np.array([[0, 0], [5, 4], [10, 0], [15, 4], [20, 0], [25, 4], [30, 0]], float)
CASES = [("nurbs", 3, ()), ("nurbs_interp", 3, ()), ("nurbs_interp", 3, (3,)), ("nurbs_interp", 2, (1, 5))]


def drag(mode, deg, corners, pins, i, target, pts=WAVE):
    L = arc_lengths(pts, mode, deg, corners)
    out = pts.copy()
    for k, q in rope_moves(pts, mode, deg, corners, L, pins, i, target):
        out[k] = q[:2]
    assert np.allclose(arc_lengths(out, mode, deg, corners), L, atol=1e-8)
    return out


def drawn_length(mode, deg, corners, pts=WAVE):
    c = (nurbs.curve(pts, deg, splinesteps=4000) if mode == "nurbs"
         else nurbs.interp_curve(pts, deg, corners=corners, splinesteps=4000))
    return np.linalg.norm(np.diff(c, axis=0), axis=1).sum()


@pytest.mark.parametrize("mode,deg,corners", CASES)
def test_lengths_add_up_to_the_curve_bosl2_draws(mode, deg, corners):
    assert np.isclose(arc_lengths(WAVE, mode, deg, corners).sum(), drawn_length(mode, deg, corners), atol=1e-5)


def test_interpolated_lengths_are_between_the_points():
    L = arc_lengths(WAVE, "nurbs_interp", 3)
    assert len(L) == len(WAVE) - 1
    assert np.all(L > np.linalg.norm(np.diff(WAVE, axis=0), axis=1))     # curves beat chords


def test_vectorised_basis_derivatives_match_the_scalar_ones():
    for p in (1, 2, 3, 4):
        n = 9
        mult = [p + 1] + [1] * (n - p - 1) + [p + 1]
        U = [i / (len(mult) - 1) for i, k in enumerate(mult) for _ in range(k)]
        ts = np.random.default_rng(p).uniform(0.001, 0.999, 40)
        ref = np.zeros((len(ts), n))
        for k, t in enumerate(ts):
            s = nurbs._span(t, U, p, n - 1)
            ref[k, s - p:s + 1] = _deriv_basis(s, t, p, U)
        assert np.allclose(_deriv_matrix(U, p, n, ts), ref, atol=1e-12)


@pytest.mark.parametrize("mode,deg,corners", CASES)
def test_a_reachable_drag_lands_on_the_cursor_keeping_every_length(mode, deg, corners):
    out = drag(mode, deg, corners, set(), 3, [15, 9])
    assert np.allclose(out[3], [15, 9], atol=1e-9)
    assert np.allclose(out[0], WAVE[0])


@pytest.mark.parametrize("mode,deg,corners", CASES)
def test_pinned_and_first_points_hold(mode, deg, corners):
    out = drag(mode, deg, corners, {6}, 3, [15, -3])
    assert np.allclose(out[[0, 6]], WAVE[[0, 6]])
    assert rope_moves(WAVE, mode, deg, corners, arc_lengths(WAVE, mode, deg, corners), {6}, 6, [1, 1]) == []
    assert rope_moves(WAVE, mode, deg, corners, arc_lengths(WAVE, mode, deg, corners), set(), 0, [1, 1]) == []


@pytest.mark.parametrize("mode,deg,corners", CASES)
def test_pulled_too_far_the_curve_goes_straight_and_stops(mode, deg, corners):
    total = arc_lengths(WAVE, mode, deg, corners).sum()
    out = drag(mode, deg, corners, set(), 6, [200, 0])
    reach = float(np.linalg.norm(out[6]))
    assert total - 0.01 < reach <= total + 1e-9


def test_a_drag_past_the_curves_reach_stops_short_of_it():
    # NURBS Path's curve is 31.7 long; (33, 3) is 33.1 from point 0.
    out = drag("nurbs", 3, (), set(), 6, [33, 3])
    assert np.linalg.norm(out[6]) <= arc_lengths(WAVE, "nurbs", 3).sum() + 1e-9


def test_slide_trades_the_two_lengths_and_keeps_the_rest():
    L = arc_lengths(WAVE, "nurbs_interp", 3)
    target = slide_target(WAVE, 3, (), 3, [17, 1])
    out = WAVE.copy()
    for k, q in rope_moves(WAVE, "nurbs_interp", 3, (), L, set(), 3, target, together=2):
        out[k] = q[:2]
    got = arc_lengths(out, "nurbs_interp", 3)
    assert np.allclose(out[3], target, atol=1e-9)
    assert got[2] > L[2] + 1 and np.isclose(got[2] + got[3], L[2] + L[3], atol=1e-8)
    assert np.allclose(np.delete(got, [2, 3]), np.delete(L, [2, 3]), atol=1e-8)


def test_ends_and_corners_do_not_slide():
    for v, corners in ((0, ()), (6, ()), (3, (3,))):
        assert slide_target(WAVE, 3, corners, v, [17, 1]) is None
