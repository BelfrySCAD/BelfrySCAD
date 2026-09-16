"""Arrow keys rotate while the Rotate tool is armed.

An angle wants its own step sizes: a 1-unit step is right for a wall
thickness and useless for a rotation, where the numbers people actually
reach for are quarter turns, 15-degree increments and single degrees.
"""
import numpy as np
import pytest
from PySide6.QtCore import Qt

from belfryscad.window.viewport import (ROTATION_NUDGE_STEPS, _key_nudge_axes,
                                        _key_nudge_magnitude, _view_locked_axis)


class _Camera:
    """A camera looking down -Z, so screen-right is +X and screen-up is +Y."""
    def __init__(self, forward=(0, 0, -1)):
        self.target = np.zeros(3)
        self._forward = np.array(forward, dtype=float)

    def eye_position(self):
        return self.target - self._forward * 10.0

    def view_matrix(self):
        m = np.eye(4)
        m[0, :3] = [1.0, 0.0, 0.0]      # screen right -> world +X
        m[1, :3] = [0.0, 1.0, 0.0]      # screen up    -> world +Y
        return m


def test_the_steps_are_quarter_turns_fifteens_and_singles():
    assert ROTATION_NUDGE_STEPS[_key_nudge_magnitude(Qt.KeyboardModifier.ShiftModifier)] == 90.0
    assert ROTATION_NUDGE_STEPS[_key_nudge_magnitude(Qt.KeyboardModifier.NoModifier)] == 15.0
    assert ROTATION_NUDGE_STEPS[_key_nudge_magnitude(Qt.KeyboardModifier.ControlModifier)] == 1.0


def test_every_translate_magnitude_has_a_rotation_step():
    """The tables are keyed by the same magnitudes, so a modifier can never
    land on a missing entry."""
    for mods in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier,
                 Qt.KeyboardModifier.ControlModifier):
        assert _key_nudge_magnitude(mods) in ROTATION_NUDGE_STEPS


def test_the_coarse_fine_relationship_is_preserved():
    fine = ROTATION_NUDGE_STEPS[0.1]
    normal = ROTATION_NUDGE_STEPS[1.0]
    coarse = ROTATION_NUDGE_STEPS[10.0]
    assert fine < normal < coarse


def test_left_right_turns_about_the_screen_up_axis():
    """Not the axis the key moves along: spinning about screen-right would
    tip the object toward the viewer instead of turning it sideways."""
    cam = _Camera()
    lock = _view_locked_axis(cam)
    right_axis, up_axis = _key_nudge_axes(cam, lock)
    assert lock == 2                     # looking down Z, so Z is edge-on
    assert right_axis == 0 and up_axis == 1

    # What keyPressEvent maps: horizontal keys -> up_axis, vertical -> right_axis.
    turn = {Qt.Key.Key_Right: up_axis, Qt.Key.Key_Left: up_axis,
            Qt.Key.Key_Down: right_axis, Qt.Key.Key_Up: right_axis}
    assert turn[Qt.Key.Key_Right] == 1   # Y: a turntable spin
    assert turn[Qt.Key.Key_Up] == 0      # X: a pitch


def test_the_axes_follow_the_camera():
    """Viewed down X instead, the same keys drive different world axes."""
    cam = _Camera(forward=(-1, 0, 0))
    lock = _view_locked_axis(cam)
    assert lock == 0
    right_axis, up_axis = _key_nudge_axes(cam, lock)
    assert lock not in (right_axis, up_axis)
    assert {right_axis, up_axis} == {1, 2}


@pytest.mark.parametrize("forward", [(0, 0, -1), (-1, 0, 0), (0, -1, 0), (-1, -1, -0.5)])
def test_the_two_axes_are_always_distinct_and_never_the_locked_one(forward):
    cam = _Camera(forward=forward)
    lock = _view_locked_axis(cam)
    right_axis, up_axis = _key_nudge_axes(cam, lock)
    assert right_axis != up_axis
    assert lock not in (right_axis, up_axis)
