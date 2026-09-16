"""Every rotation ring measures its drag angle right-handed about its own axis.

The Y ring used `X, Z` as its plane frame, and `X x Z` is *minus* Y, so
dragging it turned the model backwards while X and Z were correct. A sign
error in one of three is exactly what a shared invariant catches and three
hand-written frames do not.
"""
import numpy as np
import pytest

from belfryscad.window.viewport import _RING_PERP1, _RING_PERP2


@pytest.mark.parametrize("ai, name", [(0, "X"), (1, "Y"), (2, "Z")])
def test_the_ring_frame_is_right_handed_about_its_axis(ai, name):
    """perp1 x perp2 == the axis. That is what makes a positive drag angle a
    positive `rotate()` -- the number written into the source."""
    axis = np.eye(3)[ai]
    assert np.allclose(np.cross(_RING_PERP1[ai], _RING_PERP2[ai]), axis), name


@pytest.mark.parametrize("ai", [0, 1, 2])
def test_the_frame_is_orthonormal_and_in_the_ring_s_plane(ai):
    p1, p2, axis = _RING_PERP1[ai], _RING_PERP2[ai], np.eye(3)[ai]
    assert np.isclose(np.linalg.norm(p1), 1.0)
    assert np.isclose(np.linalg.norm(p2), 1.0)
    assert np.isclose(np.dot(p1, p2), 0.0)
    assert np.isclose(np.dot(p1, axis), 0.0), "perp1 must lie in the ring's plane"
    assert np.isclose(np.dot(p2, axis), 0.0), "perp2 must lie in the ring's plane"


@pytest.mark.parametrize("ai, name", [(0, "X"), (1, "Y"), (2, "Z")])
def test_a_quarter_turn_the_right_handed_way_reads_as_plus_90(ai, name):
    """The claim from the far end: take a point on the ring, turn it 90
    degrees the way `rotate(90)` about this axis would, and the angle the
    drag reports must have gone *up* by 90, not down."""
    p1, p2, axis = _RING_PERP1[ai], _RING_PERP2[ai], np.eye(3)[ai]

    def reported(radial):
        return np.degrees(np.arctan2(np.dot(radial, p2), np.dot(radial, p1)))

    # Rodrigues about `axis` by +90, for a vector perpendicular to it.
    turned = np.cross(axis, p1)
    assert np.isclose(reported(p1), 0.0)
    assert np.isclose(reported(turned), 90.0), (
        f"{name} ring reports {reported(turned)} for a +90 turn")
