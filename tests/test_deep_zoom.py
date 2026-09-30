"""#568: the viewport zooms to a 1e-12 model, as OpenSCAD does.

Every piece of the pipeline had an absolute length in it -- a 0.1 zoom and
near-plane floor, a 1.0 fit floor, 1e-9 guards, a 1e-9 tick-spacing floor,
float32 normals -- each harmless at millimetre scale and each breaking the
view somewhere below it. These pin each at 1e-12.
"""
import math

import numpy as np
import pytest

from belfryscad.engine.renderer import (Camera, _axis_spans, _drawn_spacings, _fmt_tick,
                                        _unit_normals)

TINY = 1e-12


def test_a_tiny_model_is_framed_not_left_as_a_speck():
    cam = Camera()
    cam.frame_bounds(np.zeros(3), np.full(3, TINY), aspect=1.0)
    assert cam.distance < 10 * TINY


def test_the_clip_planes_bracket_a_tiny_model():
    cam = Camera()
    cam.frame_bounds(np.zeros(3), np.full(3, TINY), aspect=1.0)
    near, far = cam.clip_planes()
    eye = cam.eye_position().astype(np.float64)
    fwd = (cam.target - eye) / np.linalg.norm(cam.target - eye)
    for corner in ([0, 0, 0], [TINY] * 3, [TINY, 0, 0], [0, TINY, TINY]):
        depth = float(np.dot(np.array(corner) - eye, fwd))
        assert near < depth < far, (corner, depth, near, far)


def test_ordinary_models_keep_a_usable_depth_range():
    cam = Camera()
    cam.frame_bounds(np.array([0.0, 0, 0]), np.array([30.0, 10, 10]), aspect=1.0)
    near, far = cam.clip_planes()
    assert 0 < near < cam.distance < far and far / near < 1e7


def test_the_camera_can_zoom_below_the_old_floor():
    cam = Camera()
    cam.zoom_to_point(cam.eye_position(), (cam.target - cam.eye_position()).astype(np.float64), 1e-15)
    assert cam.distance == pytest.approx(Camera.MIN_DISTANCE)


def test_orbiting_still_turns_a_camera_this_close():
    cam = Camera()
    cam.distance = 5 * TINY
    before = cam.azimuth
    cam.orbit_free(10.0, 0.0)
    assert cam.azimuth != pytest.approx(before)


def test_axes_and_ticks_exist_at_tiny_lengths():
    L = 1.5e-11
    label, major, minor = _drawn_spacings(L)
    assert 0 < minor < major <= label <= L
    spans = _axis_spans(L, minor)
    assert spans and spans[-1][1] == pytest.approx(L)


@pytest.mark.parametrize("val, spacing, text", [
    (1e-12, 5e-13, "1e-12"), (1.5e-12, 5e-13, "1.5e-12"), (3.0000000000000004e-12, 1e-12, "3e-12"),
    (0.0, 1e-12, "0"), (0.25, 0.05, "0.25"), (12.0, 2.0, "12"),
])
def test_tick_labels_read_well_at_any_scale(val, spacing, text):
    assert _fmt_tick(val, spacing) == text


def test_normals_of_a_tiny_face_are_unit_length():
    v0 = np.zeros((1, 3), np.float32)
    v1 = np.array([[TINY, 0, 0]], np.float32)
    v2 = np.array([[0, TINY, 0]], np.float32)
    n = _unit_normals(v0, v1, v2)
    assert n.dtype == np.float32 and np.allclose(n, [[0, 0, 1]])


def test_a_camera_inside_a_large_scene_keeps_depth_precision():
    # #663: a 550mm square under a 50mm model puts the eye inside the scene's
    # bounding sphere when zoomed in; near fell to far * 1e-7 and the model's
    # faces lost their depth order.
    cam = Camera()
    cam.scene_bounds = (np.array([-275.0, -275, -30]), np.array([275.0, 275, 25]))
    cam.distance = 250.0
    near, far = cam.clip_planes()
    assert cam.distance ** 2 / (near * 2 ** 24) < 0.01  # depth step at the target, mm
    assert near < cam.distance < far
