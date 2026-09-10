"""Orientation cube geometry that needs no window: label feet and fit."""
import math

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def cube():
    QApplication.instance() or QApplication([])
    from belfryscad.window.orientation_cube import OrientationCube
    return OrientationCube()


def _rz(d):
    a = math.radians(d)
    return np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1.0]])


def _rx(d):
    a = math.radians(d)
    return np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])


def _views():
    for el in range(-89, 90, 11):
        for az in range(0, 360, 20):
            yield _rx(el) @ _rz(az)


def test_label_foot_is_fixed_in_model_space(cube):
    """Vertical faces keep the label's foot on Z-, the Z faces on Y-, from every view."""
    for rot in _views():
        cube.set_orientation(rot)
        for reg in cube._regions:
            if not reg.label:
                continue
            foot = np.array([0, -1, 0.0]) if abs(reg.normal[2]) > 0.5 else np.array([0, 0, -1.0])
            proj = cube._project(reg.points)
            d = foot @ cube._rot.T
            frame = cube._label_frame(proj, np.array([d[0], -d[1]]))
            if frame is None:       # edge-on
                continue
            o, _x, y = frame
            pts = proj[:, :2]
            i0 = int(np.argmin(np.linalg.norm(pts - o, axis=1)))
            i1 = int(np.argmin(np.linalg.norm(pts - (o + y), axis=1)))
            edge = reg.points[i1] - reg.points[i0]
            assert float(edge @ foot) / np.linalg.norm(edge) > 0.999, (reg.label, rot)


def test_cube_stays_inside_widget_from_every_view(cube):
    for rot in _views():
        cube.set_orientation(rot)
        p = np.vstack([cube._project(r.points) for r in cube._regions])[:, :2]
        assert p.min() >= 1.5 and cube.SIZE - p.max() >= 1.5
