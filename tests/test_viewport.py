"""Tests for belfryscad.window.viewport's pure (Qt-free) helper functions.

Doesn't import anything requiring a live GL context or widget instantiation
-- just the module-level math helpers -- so this is safe under pytest even
though driving the real Viewport widget itself is not (see
feedback_gl_qt_tests_crash_pytest memory).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pytest import approx

from belfryscad.window.viewport import _outer_ring_roll_delta_deg


class TestOuterRingRollDelta:
    """Shift+drag "Orbit" mode's outer-20%-ring roll gesture: dragging near
    the rim of the viewport rolls the view like a dial instead of tilting
    it. See project_orbit_rotation_mode memory."""

    def test_center_drag_is_not_in_the_ring(self):
        # 100x100 viewport, center (50,50): dead center, tiny move.
        assert _outer_ring_roll_delta_deg(51, 50, 1, 0, 100, 100) is None

    def test_edge_drag_is_in_the_ring(self):
        # radius=50, 0.8*50=40 -> 41px from center is inside the outer ring.
        assert _outer_ring_roll_delta_deg(50, 9, 0, -1, 100, 100) is not None

    def test_boundary_just_inside_vs_outside(self):
        # 100x100 -> center (50,50), radius 50, ring starts at 0.8*50=40px.
        assert _outer_ring_roll_delta_deg(50, 50 - 39, 0, -1, 100, 100) is None
        assert _outer_ring_roll_delta_deg(50, 50 - 41, 0, -1, 100, 100) is not None

    def test_left_edge_moving_up_is_clockwise_positive(self):
        # Mouse on the left rim (9 o'clock), moving up (screen y decreases)
        # -- user's own spec: this is clockwise.
        delta = _outer_ring_roll_delta_deg(10, 50, 0, -2, 100, 100)
        assert delta > 0

    def test_top_edge_moving_right_is_clockwise_positive(self):
        # Mouse on the top rim (12 o'clock), moving right (screen x
        # increases) -- user's own spec: this is also clockwise.
        delta = _outer_ring_roll_delta_deg(50, 10, 2, 0, 100, 100)
        assert delta > 0

    def test_left_edge_moving_down_is_counterclockwise(self):
        delta = _outer_ring_roll_delta_deg(10, 50, 0, 2, 100, 100)
        assert delta < 0

    def test_zero_movement_gives_zero_delta(self):
        assert _outer_ring_roll_delta_deg(10, 50, 0, 0, 100, 100) == approx(0.0, abs=1e-9)

    def test_degenerate_zero_size_viewport_returns_none(self):
        assert _outer_ring_roll_delta_deg(0, 0, 1, 1, 0, 0) is None
