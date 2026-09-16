"""Arrow keys rotate while the Rotate tool is armed.

An angle wants its own step sizes: a 1-unit step is right for a wall
thickness and useless for a rotation, where the numbers people actually
reach for are quarter turns, 15-degree increments and single degrees.
"""
import numpy as np
import pytest
from PySide6.QtCore import Qt

from belfryscad.window.viewport import (ROTATION_NUDGE_STEPS, _key_nudge_axes,
                                        _key_nudge_magnitude, _screen_roll_axis,
                                        _view_locked_axis)


class _Camera:
    """A camera at `eye` looking at the origin."""
    def __init__(self, forward=(0, 0, -1), eye=None):
        self.target = np.zeros(3)
        self._eye = (np.array(eye, dtype=float) if eye is not None
                     else self.target - np.array(forward, dtype=float) * 10.0)

    def eye_position(self):
        return self._eye

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


def _rot(axis, deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    m = np.eye(3)
    i, j = [(1, 2), (2, 0), (0, 1)][axis]
    m[i, i] = c; m[i, j] = -s; m[j, i] = s; m[j, j] = c
    return m


def _screen_basis(cam):
    fwd = cam.target - cam.eye_position()
    fwd = fwd / np.linalg.norm(fwd)
    up0 = np.array([0, 1, 0.0]) if abs(fwd[1]) < 0.9 else np.array([0, 0, 1.0])
    right = np.cross(fwd, up0); right /= np.linalg.norm(right)
    return right, np.cross(right, fwd)


@pytest.mark.parametrize("eye", [(0, 0, 10), (0, 0, -10), (10, 0, 0),
                                  (0, 10, 0), (7, 7, 7)])
def test_left_is_counter_clockwise_from_the_viewers_side(eye):
    """The stated convention: Left CCW, Right CW, as the viewer sees it.

    Checked by rotating a marker rather than by reasoning about the
    right-hand rule -- including from BEHIND, where the axis points the
    other way through the screen and the sign has to flip to compensate.
    """
    cam = _Camera(eye=eye)
    axis, sign = _screen_roll_axis(cam)
    right, up = _screen_basis(cam)

    turned_left = _rot(axis, sign * 15) @ right
    assert np.dot(turned_left, up) > 0, "Left must sweep screen-right toward screen-up"

    turned_right = _rot(axis, -sign * 15) @ right
    assert np.dot(turned_right, up) < 0, "Right must sweep it the other way"


def test_the_roll_axis_is_the_one_dragging_cannot_use():
    """It is `_view_locked_axis`: foreshortened to a point for a drag, and
    for exactly that reason the natural one for keys."""
    for eye in ((0, 0, 10), (10, 0, 0), (0, 10, 0)):
        cam = _Camera(eye=eye)
        assert _screen_roll_axis(cam)[0] == _view_locked_axis(cam)


def test_up_down_pitch_about_the_screen_right_axis():
    cam = _Camera()
    lock = _view_locked_axis(cam)
    right_axis, _up = _key_nudge_axes(cam, lock)
    assert right_axis != lock
