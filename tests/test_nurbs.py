"""`belfryscad.nurbs` against BOSL2's own output.

Golden values are BOSL2 @ 9f1fa5c7's `nurbs_curve(...)` echoed through
this project's evaluator (so 6 significant digits). A wider sweep --
degrees 1-4, clamped and closed, 2D and 3D -- agreed to that rounding
when the port was written.
"""

import numpy as np
import pytest

from belfryscad import nurbs

PTS = [[5, 0], [0, 20], [33, 43], [37, 88], [60, 62], [44, 22], [77, 44], [79, 22], [44, 3], [22, 7]]
DATA = [[0, 0], [0, 10], [-5, 20], [5, 30], [15, 20], [10, 10], [10, 0]]


def test_closed_curve_matches_bosl2():
    # nurbs_curve(PTS, 3, 8, type="closed")
    got = nurbs.curve(PTS, 3, closed=True, splinesteps=8)
    assert len(got) == 80
    np.testing.assert_allclose(got[:3], [[6.33333, 20.5], [8.3584, 23.2171], [10.8464, 26.0182]], atol=1e-4)


def test_clamped_curve_ends_on_its_end_points():
    got = nurbs.curve(PTS, 3, splinesteps=8)
    np.testing.assert_allclose(got[[0, -1]], [PTS[0], PTS[-1]], atol=1e-12)


def test_clamped_interp_matches_bosl2():
    # nurbs_curve(nurbs_interp(DATA, 3), splinesteps=8)
    control, knots = nurbs.interp(DATA, 3)
    got = nurbs.curve(control, 3, splinesteps=8, knots=knots)
    assert len(got) == 33
    np.testing.assert_allclose(got[1:3], [[1.69955, 2.95639], [1.95185, 5.62533]], atol=1e-4)


def test_closed_interp_matches_bosl2():
    # nurbs_curve(nurbs_interp(DATA, 3, closed=true), splinesteps=8) --
    # starts at DATA[1], not DATA[0]: BOSL2 rotates the seam.
    control, knots = nurbs.interp(DATA, 3, closed=True)
    got = nurbs.curve(control, 3, closed=True, splinesteps=8, knots=knots)
    assert len(got) == 56
    np.testing.assert_allclose(got[:3], [[0, 10], [-0.378542, 11.2065], [-0.955562, 12.3361]], atol=1e-4)


def test_interp_passes_through_every_point():
    control, knots = nurbs.interp(DATA, 3)
    got = nurbs.curve(control, 3, splinesteps=2000, knots=knots)
    for p in DATA:
        assert np.min(np.linalg.norm(got - p, axis=1)) < 0.01


@pytest.mark.parametrize("points, degree", [
    (DATA[:3], 3),            # too few points for the degree
    (DATA, 1),                # BOSL2's smooth=3 needs degree >= 2
    ([[0, 0], [0, 0], [1, 1], [2, 0]], 2),   # duplicate neighbours
])
def test_interp_refuses_what_bosl2_refuses(points, degree):
    with pytest.raises(ValueError):
        nurbs.interp(points, degree)


# -- Surfaces ---------------------------------------------------------------
# Golden values: BOSL2 @ 9f1fa5c7, echoed. A wider sweep -- every
# clamped/closed pairing, of both the patch and the interpolation, with
# degree [3,2] -- agreed to that rounding when the port was written.

def _sd(a):
    return np.sin(np.radians(a))


# P = [for (i=[0:4]) [for (j=[0:5]) [i*10 + (j%2)*2, j*8 + i*1.5, sin(i*50+j*70)*6 + i*j*0.3]]]
SHEET = [[[i * 10 + (j % 2) * 2, j * 8 + i * 1.5, _sd(i * 50 + j * 70) * 6 + i * j * 0.3]
          for j in range(6)] for i in range(5)]
# Q = [for (i=[0:5]) [for (j=[0:6]) [(20+i*3)*cos(j*360/7), (20+i*3)*sin(j*360/7) + i, i*6 + 2*sin(j*90)]]]
TUBE = [[[(20 + i * 3) * np.cos(np.radians(j * 360 / 7)), (20 + i * 3) * _sd(j * 360 / 7) + i,
          i * 6 + 2 * _sd(j * 90)] for j in range(7)] for i in range(6)]


def test_patch_matches_bosl2():
    # nurbs_patch_points(P, [3,2], 3, type=["closed","clamped"])
    got = nurbs.patch(SHEET, (3, 2), (True, False), 3)
    assert got.shape == (15, 13, 3)
    np.testing.assert_allclose(got[0, :2], [[10, 1.5, 4.04899], [11, 6.38889, 4.22061]], atol=1e-4)
    np.testing.assert_allclose(got[1, 1], [14.3333, 6.88889, 4.07828], atol=1e-4)


def test_interp_surface_matches_bosl2():
    # nurbs_patch_points(nurbs_interp_surface(Q, [3,2], row_wrap=true, col_wrap=true), splinesteps=3)
    control, knots = nurbs.interp_surface(TUBE, (3, 2), (True, True))
    got = nurbs.patch(control, (3, 2), (True, True), 3, knots)
    assert got.shape == (18, 21, 3)
    np.testing.assert_allclose(got[0, :2], [[19.9987, -0.192613, -0.0388187], [19.2239, 5.84226, 1.05438]], atol=1e-4)
    np.testing.assert_allclose(got[1, 1], [20.6601, 6.7768, 4.04278], atol=1e-4)


def test_interp_surface_passes_through_every_point():
    control, knots = nurbs.interp_surface(SHEET, (3, 3))
    got = nurbs.patch(control, (3, 3), knots=knots, splinesteps=200).reshape(-1, 3)
    for p in np.reshape(SHEET, (-1, 3)):
        assert np.min(np.linalg.norm(got - p, axis=1)) < 0.05


def test_interp_surface_needs_degree_plus_one_rows_and_columns():
    with pytest.raises(ValueError):
        nurbs.interp_surface(SHEET, (5, 3))       # only 5 rows
