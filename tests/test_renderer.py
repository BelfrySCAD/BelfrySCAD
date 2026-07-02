"""
Tests for `nearest_point_index` in `belfryscad.engine.renderer` — the pure
screen-space nearest-point picking helper shared by `SceneRenderer.
pick_nearest_point` and (once migrated) the data viewers' vertex-click
handlers. Pure math, no GL/Qt dependency.
"""
import numpy as np

from belfryscad.engine.renderer import nearest_point_index


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
