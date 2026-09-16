"""A gizmo drag lands on the same numbers a keyboard nudge would.

Before this, a drag rounded to a fixed 0.1 (or 1 degree) whatever you held,
so dragging a part into place left 3.7 in the source where nudging it would
have left 4, and the modifiers meant nothing at all while dragging.
"""
import pytest

from belfryscad.window.viewport import (ROTATION_NUDGE_STEPS, _SCALE_DRAG_STEPS,
                                        _key_nudge_magnitude, _quantize)
from PySide6.QtCore import Qt

SHIFT = Qt.KeyboardModifier.ShiftModifier
CMD = Qt.KeyboardModifier.ControlModifier      # Cmd on macOS
NONE = Qt.KeyboardModifier.NoModifier


@pytest.mark.parametrize("value, step, want", [
    (3.7, 1.0, 4.0), (-3.2, 1.0, -3.0), (0.4, 1.0, 0.0),
    (3.7, 10.0, 0.0), (16.0, 10.0, 20.0), (-24.0, 10.0, -20.0),
    (3.74, 0.1, 3.7), (0.35, 0.1, 0.3), (1.234, 0.01, 1.23),
])
def test_quantize_snaps_to_the_nearest_multiple(value, step, want):
    assert _quantize(value, step) == want


def test_quantize_does_not_leave_binary_float_dust():
    """3 * 0.1 is not 0.3, and a source file should not say so."""
    assert repr(_quantize(0.3000001, 0.1)) == "0.3"
    assert repr(_quantize(0.7, 0.1)) == "0.7"


@pytest.mark.parametrize("mods, want", [(NONE, 1.0), (SHIFT, 10.0), (CMD, 0.1)])
def test_translate_drag_uses_the_nudge_magnitudes(mods, want):
    assert _key_nudge_magnitude(mods) == want


@pytest.mark.parametrize("mods, want", [(NONE, 15.0), (SHIFT, 90.0), (CMD, 1.0)])
def test_rotate_drag_uses_the_rotate_nudge_steps(mods, want):
    """The same 90/15/1 the Rotate tool's arrow keys use."""
    assert ROTATION_NUDGE_STEPS[_key_nudge_magnitude(mods)] == want


def test_a_rotate_drag_can_still_reach_a_right_angle_unheld():
    """15 divides 90, so the common angles are reachable without Shift."""
    assert _quantize(88.0, ROTATION_NUDGE_STEPS[1.0]) == 90.0
    assert _quantize(43.0, ROTATION_NUDGE_STEPS[1.0]) == 45.0


def test_scale_keeps_shift_for_uniform_so_it_has_no_coarse_step():
    """A modifier cannot mean two things at once: on the scale gizmo Shift
    already means all three axes, so only Cmd changes the quantum there."""
    assert _SCALE_DRAG_STEPS[_key_nudge_magnitude(SHIFT)] == 0.1
    assert _SCALE_DRAG_STEPS[_key_nudge_magnitude(NONE)] == 0.1
    assert _SCALE_DRAG_STEPS[_key_nudge_magnitude(CMD)] == 0.01


def test_the_scale_commit_threshold_admits_the_finest_step():
    """0.05 was hard-coded and silently threw away every 0.01 Cmd-drag."""
    import inspect
    from belfryscad.window.viewport import Viewport
    src = inspect.getsource(Viewport._commit_gizmo_drag)
    assert "_SCALE_DRAG_STEPS" in src, "the threshold must track the step table"
    assert min(_SCALE_DRAG_STEPS.values()) / 2 < 0.01


def test_the_translate_commit_does_not_re_round_away_a_fine_drag():
    import inspect
    from belfryscad.window.viewport import Viewport
    src = inspect.getsource(Viewport._commit_gizmo_drag)
    assert "round(float(offset[0]), 1)" not in src, "0.1 re-round loses 0.01"


def test_the_drag_reads_modifiers_live_not_from_the_press():
    """A drag is long enough to change your mind about how fine you want it."""
    import inspect
    from belfryscad.window.viewport import Viewport
    src = inspect.getsource(Viewport._update_gizmo_drag)
    assert "QApplication.keyboardModifiers()" in src
    assert src.count("_key_nudge_magnitude") == 1, "one magnitude for all tools"
