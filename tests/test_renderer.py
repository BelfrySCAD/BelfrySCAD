"""
Tests for `nearest_point_index` in `belfryscad.engine.renderer` — the pure
screen-space nearest-point picking helper shared by `SceneRenderer.
pick_nearest_point` and (once migrated) the data viewers' vertex-click
handlers. Pure math, no GL/Qt dependency. Also covers `nearest_segment_index`
(and its `_closest_point_on_segment_to_ray` building block), the analogous
line-segment picking helper behind `SceneRenderer.pick_nearest_segment` and
the Path/Region editors' right-click-on-a-line "Add Vertex" feature.
"""
import math
import numpy as np
from pytest import approx

from belfryscad.engine.renderer import (
    nearest_point_index, nearest_segment_index, _closest_point_on_segment_to_ray, Camera,
    _axis_density, _tick_is_drawn,
)


class TestNearestPointIndex:
    def test_exact_hit_at_center(self):
        # identity mvp: clip == [x, y, z, 1], so w_clip == 1 always;
        # (0, 0, 0) maps to the exact center of a w x h viewport.
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0]])
        assert nearest_point_index(points, mvp, 50, 50, 100, 100) == 0

    def test_picks_nearest_of_several(self):
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0], [0.1, 0.1, 0.0], [5.0, 5.0, 0.0]])
        # (0.1, 0.1) is closer to screen center than (0, 0) after projection
        # scaling; verify by construction rather than assuming index order.
        idx = nearest_point_index(points, mvp, 55, 45, 100, 100)
        assert idx == 1

    def test_miss_beyond_threshold(self):
        mvp = np.eye(4)
        points = np.array([[100.0, 0.0, 0.0]])
        assert nearest_point_index(points, mvp, 50, 50, 100, 100) == -1

    def test_empty_points_returns_minus_one(self):
        mvp = np.eye(4)
        points = np.zeros((0, 3))
        assert nearest_point_index(points, mvp, 50, 50, 100, 100) == -1

    def test_degenerate_viewport_returns_minus_one(self):
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0]])
        assert nearest_point_index(points, mvp, 50, 50, 0, 100) == -1
        assert nearest_point_index(points, mvp, 50, 50, 100, 0) == -1

    def test_point_behind_camera_excluded(self):
        # Perspective-like mvp: w_clip = -z, so z > 0 is "behind" the camera.
        mvp = np.eye(4)
        mvp[3] = [0, 0, -1, 0]
        points = np.array([
            [0.0, 0.0, 5.0],   # behind camera (w_clip = -5) — must be excluded
            [0.0, 0.0, -5.0],  # in front (w_clip = 5) — projects to screen center
        ])
        idx = nearest_point_index(points, mvp, 50, 50, 100, 100)
        assert idx == 1

    def test_custom_threshold_respected(self):
        mvp = np.eye(4)
        points = np.array([[0.2, 0.0, 0.0]])  # a few px off center
        # default threshold (12px) should hit; a very tight threshold should miss
        assert nearest_point_index(points, mvp, 50, 50, 100, 100) != -1
        assert nearest_point_index(points, mvp, 50, 50, 100, 100, threshold_px=0.5) == -1


class TestClosestPointOnSegmentToRay:
    def test_perpendicular_ray_through_midpoint(self):
        a, b = np.array([0.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0])
        ray_o, ray_d = np.array([5.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        pt = _closest_point_on_segment_to_ray(a, b, ray_o, ray_d)
        assert np.allclose(pt, [5.0, 0.0, 0.0], atol=1e-6)

    def test_clamped_to_segment_endpoint(self):
        # Ray passes the segment's line well beyond point b -- the closest
        # point must clamp to b, not extrapolate past it.
        a, b = np.array([0.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0])
        ray_o, ray_d = np.array([25.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        pt = _closest_point_on_segment_to_ray(a, b, ray_o, ray_d)
        assert np.allclose(pt, [10.0, 0.0, 0.0], atol=1e-6)

    def test_clamped_to_segment_start(self):
        a, b = np.array([0.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0])
        ray_o, ray_d = np.array([-25.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        pt = _closest_point_on_segment_to_ray(a, b, ray_o, ray_d)
        assert np.allclose(pt, [0.0, 0.0, 0.0], atol=1e-6)


class TestNearestSegmentIndex:
    def test_picks_segment_under_cursor(self):
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.0]])
        segments = [(0, 1), (1, 2), (2, 3), (3, 0)]
        ray_o, ray_d = np.array([5.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        # world (5,0,0) with identity mvp projects to screen (300, 50) in a 100x100 viewport
        result = nearest_segment_index(points, segments, ray_o, ray_d, mvp, 300, 50, 100, 100)
        assert result is not None
        idx, pt = result
        assert idx == 0
        assert np.allclose(pt, [5.0, 0.0, 0.0], atol=1e-6)

    def test_miss_beyond_threshold(self):
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        segments = [(0, 1)]
        ray_o, ray_d = np.array([5.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        assert nearest_segment_index(points, segments, ray_o, ray_d, mvp, 999, 999, 100, 100) is None

    def test_empty_segments_returns_none(self):
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0]])
        ray_o, ray_d = np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        assert nearest_segment_index(points, [], ray_o, ray_d, mvp, 50, 50, 100, 100) is None

    def test_degenerate_viewport_returns_none(self):
        mvp = np.eye(4)
        points = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        segments = [(0, 1)]
        ray_o, ray_d = np.array([5.0, 0.0, 5.0]), np.array([0.0, 0.0, -1.0])
        assert nearest_segment_index(points, segments, ray_o, ray_d, mvp, 50, 50, 0, 100) is None


class TestCameraViewMatrixContinuity:
    """Regression test: `_look_at`'s gimbal-lock fallback (used whenever the
    forward vector is parallel to world-up, i.e. exactly elevation +-90)
    hardcodes an arbitrary +X "right" vector, which doesn't match the
    azimuth-dependent basis the camera continuously converges to just off
    the pole. Landing exactly on elevation=90 (the old "Top" view preset)
    made the very first orbit-drag frame snap to whatever that arbitrary
    fallback implied — up to a 90-degree jump."""

    def test_right_vector_is_continuous_approaching_the_pole(self):
        cam = Camera()
        cam.azimuth, cam.distance = 0, 50.0

        cam.elevation = 89.9999
        near_pole = cam.view_matrix()[0, :3]

        cam.elevation = 89.0
        after_drag = cam.view_matrix()[0, :3]

        assert np.linalg.norm(near_pole - after_drag) < 0.02

    def test_exact_pole_fallback_does_not_match_the_continuous_limit(self):
        # Documents *why* the fix avoids landing on exactly elevation=90:
        # the degenerate fallback used there disagrees with the value the
        # continuous formula converges to from either side.
        cam = Camera()
        cam.azimuth, cam.distance = 0, 50.0

        cam.elevation = 90.0
        exact_pole = cam.view_matrix()[0, :3]

        cam.elevation = 89.0
        just_off_pole = cam.view_matrix()[0, :3]

        assert np.linalg.norm(exact_pole - just_off_pole) > 1.0

    def test_continuity_holds_at_other_azimuths_too(self):
        cam = Camera()
        cam.distance = 50.0
        for az in (37.0, 120.0, 210.0, 295.0):
            cam.azimuth = az
            cam.elevation = 89.9999
            near_pole = cam.view_matrix()[0, :3]
            cam.elevation = 89.0
            after_drag = cam.view_matrix()[0, :3]
            assert np.linalg.norm(near_pole - after_drag) < 0.02, f"discontinuity at azimuth={az}"


def _forward(cam: Camera) -> np.ndarray:
    az, el = math.radians(cam.azimuth), math.radians(cam.elevation)
    return -np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)],
                      dtype=np.float32)


def _hit_on_focal_plane(cam: Camera, ray_origin: np.ndarray, ray_dir: np.ndarray) -> np.ndarray:
    """Reproduces zoom_to_point's own ray/focal-plane intersection, for
    assertions independent of its internal state changes."""
    forward = _forward(cam)
    t = float(np.dot(cam.target - ray_origin, forward)) / float(np.dot(ray_dir, forward))
    return ray_origin + ray_dir * t


class TestCameraZoomToPoint:
    """Scroll-wheel zoom should keep the world point under the cursor fixed
    on screen (dolly toward/away from that point) rather than always
    dollying toward cam.target."""

    def test_center_ray_hit_stays_on_target(self):
        cam = Camera()
        cam.azimuth, cam.elevation, cam.distance = 295.0, 35.0, 50.0
        cam.target = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        ray_origin = cam.eye_position()
        ray_dir = (cam.target - ray_origin)
        ray_dir /= np.linalg.norm(ray_dir)
        cam.zoom_to_point(ray_origin, ray_dir, 0.5)
        assert cam.distance == approx(25.0)
        assert cam.target == approx(np.array([1.0, 2.0, 3.0]), abs=1e-4)

    def test_off_center_perspective_ray_hit_point_is_preserved(self):
        # A ray from the eye, perturbed off dead-center (as a perspective
        # camera_ray() for an off-center pixel would be) -- the point it
        # hits on the focal plane must land in the same place before and
        # after zooming, not drift toward cam.target.
        cam = Camera()
        cam.azimuth, cam.elevation, cam.distance = 295.0, 35.0, 50.0
        cam.target = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        eye = cam.eye_position()
        ray_dir = (cam.target - eye) + np.array([3.0, -2.0, 1.0], dtype=np.float32)
        ray_dir /= np.linalg.norm(ray_dir)

        before = _hit_on_focal_plane(cam, eye, ray_dir)
        cam.zoom_to_point(eye, ray_dir, 0.5)
        after = _hit_on_focal_plane(cam, eye, ray_dir)

        assert after == approx(before, abs=1e-3)

    def test_orthographic_style_parallel_ray_hit_point_is_preserved(self):
        # Orthographic rays are parallel (same direction for every pixel)
        # but originate from different points across the frustum, not a
        # single eye. A version of this method that assumed a single
        # eye_position() origin collapsed every such ray's hit point onto
        # cam.target regardless of the ray's actual lateral offset -- this
        # is the regression this test guards against.
        cam = Camera()
        cam.azimuth, cam.elevation, cam.distance = 295.0, 35.0, 50.0
        cam.target = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        forward = _forward(cam)
        # a "right"-ish vector, not parallel to forward
        lateral = np.cross(forward, np.array([0.0, 0.0, 1.0], dtype=np.float32))
        lateral /= np.linalg.norm(lateral)
        ray_origin = cam.eye_position() + lateral * 7.0  # off to the side, like an ortho ray
        ray_dir = forward.copy()

        before = _hit_on_focal_plane(cam, ray_origin, ray_dir)
        assert not np.allclose(before, cam.target, atol=1e-3)  # sanity: genuinely off-target

        cam.zoom_to_point(ray_origin, ray_dir, 0.5)
        after = _hit_on_focal_plane(cam, ray_origin, ray_dir)

        assert after == approx(before, abs=1e-3)

    def test_zoom_out_factor_greater_than_one_also_preserves_hit(self):
        cam = Camera()
        cam.azimuth, cam.elevation, cam.distance = 295.0, 35.0, 50.0
        cam.target = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        eye = cam.eye_position()
        ray_dir = (cam.target - eye) + np.array([-2.0, 4.0, -1.0], dtype=np.float32)
        ray_dir /= np.linalg.norm(ray_dir)

        before = _hit_on_focal_plane(cam, eye, ray_dir)
        cam.zoom_to_point(eye, ray_dir, 1.5)
        after = _hit_on_focal_plane(cam, eye, ray_dir)

        assert cam.distance == approx(75.0)
        assert after == approx(before, abs=1e-3)

    def test_respects_min_distance_clamp(self):
        cam = Camera()
        cam.distance = 0.15
        cam.target = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        eye = cam.eye_position()
        ray_dir = (cam.target - eye)
        ray_dir /= np.linalg.norm(ray_dir)
        cam.zoom_to_point(eye, ray_dir, 0.5, min_distance=0.1)
        assert cam.distance == approx(0.1)


class TestAxisDensity:
    """Axis tick/label thinning: as an axis swings toward end-on (pointing
    at/away from the camera), evenly-spaced-in-world ticks crowd into an
    ever-smaller on-screen span. _axis_density should grow stride
    (skip-N-between-each) smoothly before the existing hard cutoff (5
    degrees from end-on) fully suppresses that axis -- see the screenshot
    in the PR/commit this covers, where labels overlapped illegibly well
    before the cutoff kicked in."""

    def test_broadside_axes_are_full_density(self):
        cam = Camera()
        cam.azimuth, cam.elevation = 45.0, 45.0  # no axis anywhere near end-on
        end_on, stride = _axis_density(cam)
        assert end_on == [False, False, False]
        assert stride == [1, 1, 1]

    def test_stride_grows_as_axis_approaches_end_on(self):
        cam = Camera()
        cam.azimuth = 85.0  # keeps Y axis increasingly end-on as elevation -> 0
        strides = []
        for el in (60.0, 30.0, 10.0, 2.0):
            cam.elevation = el
            end_on, stride = _axis_density(cam)
            assert not end_on[1], f"elevation={el} should not hit the hard cutoff yet"
            strides.append(stride[1])
        # non-decreasing as the axis gets closer to end-on
        assert strides == sorted(strides)
        assert strides[-1] > strides[0]

    def test_stride_is_a_power_of_two(self):
        cam = Camera()
        cam.azimuth = 85.0
        for el in (60.0, 30.0, 10.0, 6.0):
            cam.elevation = el
            _, stride = _axis_density(cam)
            s = stride[1]
            assert s & (s - 1) == 0, f"stride {s} at elevation={el} is not a power of two"

    def test_hard_cutoff_still_applies_within_five_degrees(self):
        cam = Camera()
        cam.azimuth, cam.elevation = 0.0, 0.0  # X axis dead-on
        end_on, _ = _axis_density(cam)
        assert end_on == [True, False, False]

    def test_other_axes_unaffected_by_one_axis_going_end_on(self):
        cam = Camera()
        cam.azimuth, cam.elevation = 0.0, 0.0
        end_on, stride = _axis_density(cam)
        assert end_on[1] is False and end_on[2] is False
        assert stride[1] == 1 and stride[2] == 1


class TestTickIsDrawn:
    """Regression: thinning by stride used to exempt major ticks (always
    shown, matching the pre-existing hard-cutoff behavior), but major_steps
    and stride aren't related, so a kept major tick could land right next
    to a kept minor tick while a stride-length gap opened up elsewhere --
    visibly inconsistent spacing. Below the hard cutoff, stride must apply
    uniformly to every tick so the drawn set is a strict arithmetic
    subsequence (constant gap)."""

    def test_drawn_ticks_form_a_constant_gap_sequence(self):
        for major_steps, stride in [(5, 4), (5, 8), (10, 4), (4, 2), (3, 4), (7, 3)]:
            drawn = [k for k in range(1, 60)
                     if _tick_is_drawn(k, major_steps, False, stride)]
            gaps = {b - a for a, b in zip(drawn, drawn[1:])}
            assert gaps == {stride}, f"major_steps={major_steps} stride={stride}: gaps={gaps}"

    def test_stride_one_draws_every_tick(self):
        drawn = [k for k in range(1, 10) if _tick_is_drawn(k, 5, False, 1)]
        assert drawn == list(range(1, 10))

    def test_hard_cutoff_only_draws_majors(self):
        drawn_major = [k for k in range(1, 21) if _tick_is_drawn(k, 5, True, 4)]
        assert drawn_major == [5, 10, 15, 20]

    def test_hard_cutoff_ignores_stride(self):
        # Majors always show at the hard cutoff regardless of stride.
        for stride in (1, 2, 4, 8):
            drawn = [k for k in range(1, 21) if _tick_is_drawn(k, 5, True, stride)]
            assert drawn == [5, 10, 15, 20]


class TestCameraClipPlanes:
    """Regression: near/far used to be fixed constants (0.1, 10000), so
    frame_bounds() auto-positioning the camera far enough away to fit a
    large or elongated object (e.g. cylinder(h=3500, d=1000), which needs
    distance~10438 to fit at the default fov=22.5) silently clipped
    whichever end of the object was farther from the eye -- and varied
    with zoom, since that changes distance."""

    def test_small_scene_unaffected(self):
        # Floors at the original constants, so typical/small scenes see no
        # behavior change at all.
        cam = Camera()
        cam.distance = 50.0
        near, far = cam.clip_planes()
        assert near == approx(0.1)
        assert far == approx(10000.0)

    def test_far_grows_with_distance(self):
        cam = Camera()
        cam.distance = 10438.0
        near, far = cam.clip_planes()
        assert far > cam.distance
        assert far == approx(cam.distance * 3.0)

    def test_near_far_ratio_matches_original_constants(self):
        # near must grow proportionally with far -- holding it fixed while
        # far grows would only worsen depth-buffer precision for large
        # scenes on top of the clipping bug.
        cam = Camera()
        cam.distance = 50000.0
        near, far = cam.clip_planes()
        assert far / near == approx(10000.0 / 0.1)

    def test_reported_cylinder_case_fits_within_far(self):
        # The exact repro: cylinder(h=3500, d=1000), auto-framed.
        cam = Camera()
        bb_min = np.array([-500.0, -500.0, 0.0])
        bb_max = np.array([500.0, 500.0, 3500.0])
        cam.frame_bounds(bb_min, bb_max)
        near, far = cam.clip_planes()

        eye = cam.eye_position()
        view_dir = (cam.target - eye)
        view_dir /= np.linalg.norm(view_dir)
        for z in (0.0, 3500.0):
            point = np.array([0.0, 0.0, z])
            depth = float(np.dot(point - eye, view_dir))
            assert near < depth < far, f"z={z} at depth={depth} falls outside [{near}, {far}]"
