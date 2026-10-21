"""Reference images on planes and a reference model, for the 3D editors (#836)."""
import math

import numpy as np
import pytest

from belfryscad.window.data_viewer_common import _ray_plane, place_on_reference
from belfryscad.window.reference_3d import PLANES, RefImage, RefModel, load_model_triangles


@pytest.mark.parametrize("plane, right, up", [
    ("XY", [1, 0, 0], [0, 1, 0]),
    ("XZ", [1, 0, 0], [0, 0, 1]),     # seen from the front, -Y
    ("YZ", [0, 1, 0], [0, 0, 1]),     # seen from the right, +X
])
def test_preset_planes_are_upright_seen_square_on(plane, right, up):
    img = RefImage("x.png")
    img.set_plane(plane)
    r, u, n = img.basis()
    assert np.allclose(r, right) and np.allclose(u, up)
    assert np.allclose(np.cross(r, u), n), "right-handed: right x up is the normal"


def test_rotation_turns_the_image_counter_clockwise_in_its_plane():
    img = RefImage("x.png", rotation=90)
    r, u, _ = img.basis()
    assert np.allclose(r, [0, 1, 0]) and np.allclose(u, [-1, 0, 0])


def test_a_tilted_plane_keeps_up_as_near_world_up_as_it_can():
    img = RefImage("x.png", normal=(1, 0, 1))
    r, u, n = img.basis()
    assert np.isclose(np.linalg.norm(u), 1) and abs(u @ n) < 1e-12
    assert u[2] > 0 and abs(r[2]) < 1e-12


def test_image_hit_is_only_on_the_image():
    img = RefImage("x.png", aspect=0.5, width=10, center=(0, 0, 5))
    down = np.array([0, 0, -1.0])
    assert np.allclose(img.hit(np.array([4, 2, 20.0]), down), [4, 2, 5])
    assert img.hit(np.array([6, 0, 20.0]), down) is None      # past the side
    assert img.hit(np.array([0, 3, 20.0]), down) is None      # past the top (height 5)
    assert img.hit(np.array([0, 0, -20.0]), down) is None     # behind the ray


def test_fit_covers_the_box_as_seen_square_on():
    img = RefImage("x.png", aspect=1.0)
    img.set_plane("XZ")
    img.fit([0, 0, 0], [20, 99, 10])
    assert np.allclose(img.center, [10, 49.5, 5])
    assert img.width == pytest.approx(22)       # 20 wide, plus a tenth


def _cube_tris():
    v = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], dtype=float)
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v[np.array(faces)]


def test_model_placement_and_hit():
    m = RefModel("cube", _cube_tris(), translate=(10, 0, 0), rotate=(0, 0, 90), scale=2)
    lo, hi = m.bounds()
    assert np.allclose(lo, [8, 0, 0]) and np.allclose(hi, [10, 2, 2])
    hit = m.hit(np.array([9, 1, 50.0]), np.array([0, 0, -1.0]))
    assert np.allclose(hit, [9, 1, 2]), "the near face, not the far one"
    assert m.hit(np.array([0, 0, 50.0]), np.array([0, 0, -1.0])) is None


def test_model_fit_centres_and_scales_into_the_box():
    m = RefModel("cube", _cube_tris())
    m.fit([0, 0, 0], [10, 4, 4])
    lo, hi = m.bounds()
    assert np.allclose((lo + hi) / 2, [5, 2, 2]) and np.isclose(np.max(hi - lo), 10)


def test_ray_plane_any_normal():
    hit = _ray_plane(np.array([0, 0, 10.0]), np.array([0, 0, -1.0]),
                     np.array([0, 0, 1.0]), np.array([1, 0, 1.0]) / math.sqrt(2))
    assert np.allclose(hit, [0, 0, 1])
    assert _ray_plane(np.zeros(3), np.array([1.0, 0, 0]), np.zeros(3), np.array([0, 0, 1.0])) is None


class _FakeCam:
    def __init__(self, eye):
        self.target = np.zeros(3, dtype=np.float32)
        self._eye = np.asarray(eye, dtype=np.float32)

    def eye_position(self):
        return self._eye


class _FakeVP:
    """Just what `place_on_reference` asks a viewport for."""

    def __init__(self, eye, normal=None, model=None):
        self._renderer = type("R", (), {})()
        self._renderer.camera = _FakeCam(eye)
        self._renderer.camera_ray = lambda px, py, w, h: (np.array([px, py, 50.0]), np.array([0, 0, -1.0]))
        self._normal, self._model = normal, model

    def width(self):
        return 1

    def height(self):
        return 1

    def _pick_plane_normal(self):
        return self._normal

    def _reference_hit(self, o, d):
        return None if self._model is None else self._model.hit(o, d)


class _Pos:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


def test_a_click_lands_on_the_model_then_the_faced_plane_then_the_axis_plane():
    through = np.array([0, 0, 3.0], dtype=np.float32)
    cube = RefModel("cube", _cube_tris(), scale=4)
    # On the model: its top face, whatever the planes say.
    assert np.allclose(place_on_reference(_FakeVP([0, 0, 50], model=cube), _Pos(1, 1), through), [1, 1, 4])
    # Off it, the axis plane through `through` (the view looks down Z).
    assert np.allclose(place_on_reference(_FakeVP([0, 0, 50], model=cube), _Pos(9, 9), through), [9, 9, 3])
    # A faced, tilted image's plane through `through` instead.
    n = np.array([0, 1, 1.0]) / math.sqrt(2)
    assert np.allclose(place_on_reference(_FakeVP([0, 0, 50], normal=n), _Pos(2, 1), through), [2, 1, 2])


def test_a_model_file_is_read_through_import(tmp_path):
    stl = tmp_path / "tri.stl"
    stl.write_text("solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
                   "vertex 0 1 0\nendloop\nendfacet\nendsolid t\n")
    tris, warnings = load_model_triangles(str(stl))
    assert tris.shape[1:] == (3, 3) and len(tris) >= 1
    with pytest.raises(Exception):
        load_model_triangles(str(tmp_path / "missing.stl"))
