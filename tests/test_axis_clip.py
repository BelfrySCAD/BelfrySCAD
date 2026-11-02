"""Axes are trimmed to what is in front of the camera and on screen -- not
to the near plane, which cut an axis pointing at the viewer off mid-screen."""
import numpy as np

from belfryscad.engine.renderer import Camera, _axis_extent, _clip_to_view


def _mvp(cam, aspect=1.5):
    return cam.projection_matrix(aspect) @ cam.view_matrix()


def _ndc(mvp, ai, sign, t):
    p = np.zeros(4)
    p[ai], p[3] = sign * t, 1.0
    c = mvp @ p
    return c[3], c[:3] / c[3]


def test_an_axis_toward_the_viewer_stops_inside_the_view():
    cam = Camera()
    cam.azimuth, cam.elevation, cam.distance = 0.0, 10.0, 100.0   # +X near the eye
    mvp = _mvp(cam)
    L = _axis_extent(cam)
    eye = cam.eye_position()
    ai = int(np.argmax(np.abs(eye)))
    sign = float(np.sign(eye[ai]))
    lo, hi = _clip_to_view(mvp, ai, sign, 0.0, L, 0.1)
    assert hi < L                                   # trimmed, not run past the eye
    for t in (lo, hi):
        w, ndc = _ndc(mvp, ai, sign, t)
        assert w > 0 and np.all(np.abs(ndc[:2]) <= 1.1 + 1e-6)


def test_an_axis_across_the_view_still_reaches_past_the_edge():
    cam = Camera()
    cam.azimuth, cam.elevation, cam.distance = 0.0, 90.0 - 1e-4, 100.0    # top view
    mvp = _mvp(cam)
    for ai in (0, 1):
        lo, hi = _clip_to_view(mvp, ai, 1.0, 0.0, _axis_extent(cam), 0.1)
        w, ndc = _ndc(mvp, ai, 1.0, hi)
        assert lo == 0.0 and np.max(np.abs(ndc[:2])) > 1.0       # off the edge, as drawn


def test_an_axis_wholly_out_of_view_is_dropped():
    cam = Camera()
    cam.azimuth, cam.elevation, cam.distance = 0.0, 90.0 - 1e-4, 1.0
    cam.target = np.array([500.0, 500.0, 0.0])                    # far from the origin
    assert _clip_to_view(_mvp(cam), 0, -1.0, 0.0, 1.0, 0.001) is None


def test_an_axis_toward_the_viewer_is_not_cut_at_the_near_plane():
    # The near plane sits just in front of a small model, so an axis toward
    # the viewer crossed it mid-screen; it must run on to the screen edge.
    from belfryscad.headless_render import _apply_camera
    cam = Camera()
    _apply_camera(cam, "0,0,0,80,0,95,120")
    cam.scene_bounds = (np.zeros(3), np.full(3, 10.0))
    mvp = _mvp(cam, 800 / 600)
    lo, hi = _clip_to_view(mvp, 0, 1.0, 0.0, _axis_extent(cam), 1e-3 * cam.distance)
    w, ndc = _ndc(mvp, 0, 1.0, hi)
    near, _far = cam.clip_planes()
    assert w < near                                           # past the near plane
    assert np.max(np.abs(ndc[:2])) >= 1.1 - 1e-6             # to the widened edge
