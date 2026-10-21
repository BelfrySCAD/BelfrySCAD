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
    img = RefImage("x.png", aspect=0.5, width=10, position=(0, 0, 5))
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
    m = RefModel("cube", _cube_tris(), position=(9, 1, 1), rotate=(0, 0, 90), scale=2)
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
    cube = RefModel("cube", _cube_tris(), scale=4, anchor=(0, 0, 0))   # 0..4 on each axis
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


# -- Moving an image with the mouse, and nudging along a faced one ------------

from dataclasses import replace

from belfryscad.window.reference_3d import drag_ref_image


def _down(x, y):
    return np.array([x, y, 50.0]), np.array([0, 0, -1.0])


def test_drag_moves_an_image_across_its_plane():
    img = RefImage("x.png", width=10, position=(0, 0, 2))
    start = replace(img)
    o, d = _down(1, 1)
    drag_ref_image(img, "plane", start, np.array([1, 1, 2.0]), (o, d), *_down(4, -1))
    assert np.allclose(img.center, [3, -2, 2])


def test_shift_drag_moves_an_image_along_its_normal():
    """Seen from the side, the pointer's travel up the screen is the
    image's travel along its normal."""
    img = RefImage("x.png", width=10, position=(0, 0, 2))      # normal +Z
    start = replace(img)
    side = lambda z: (np.array([0, -50, z]), np.array([0, 1.0, 0]))
    drag_ref_image(img, "normal", start, None, side(2), *side(7))
    assert np.allclose(img.center, [0, 0, 7])


def test_option_drag_turns_an_image_about_its_centre():
    img = RefImage("x.png", width=10, position=(0, 0, 0))
    start = replace(img)
    drag_ref_image(img, "turn", start, np.array([3, 0, 0.0]), _down(3, 0), *_down(0, 3))
    assert img.rotation == pytest.approx(90)
    assert np.allclose(img.center, [0, 0, 0])


def test_arrow_keys_follow_a_faced_image():
    from PySide6.QtCore import Qt
    from belfryscad.engine.renderer import Camera
    from belfryscad.window.reference_3d import Reference3DViewportMixin

    class VP(Reference3DViewportMixin):
        pass
    vp = VP()
    vp._renderer = type("R", (), {"camera": Camera()})()
    img = RefImage("x.png", normal=(1, 0, 1), position=(0, 0, 0))
    vp.ref_images = [img]
    cam = vp._renderer.camera
    right, up, n = img.basis()
    cam._set_from_eye_and_up(n * 50, up)            # square-on, upright
    assert np.allclose(vp._reference_nudge(Qt.Key.Key_Right, 2), 2 * right, atol=1e-5)
    assert np.allclose(vp._reference_nudge(Qt.Key.Key_Down, 1), -up, atol=1e-5)
    cam._set_from_eye_and_up(np.array([0, 0, 50.0]), np.array([0, 1.0, 0]))   # 45 degrees off
    assert vp._reference_nudge(Qt.Key.Key_Right, 1) is None


# -- Anchors and alignment, as the 2D background image has them --------------

from belfryscad.window.reference_3d import model_anchor_name


def test_an_image_sits_turns_and_grows_about_its_anchor():
    img = RefImage("x.png", width=10, aspect=0.5, anchor=(0.0, 0.0))
    img.set_plane("XZ")              # keeps the centre, so placed after
    img.position = (0, 0, 0)
    assert np.allclose(img.corners()[0], [0, 0, 0]), "bottom-left corner on the position"
    assert np.allclose(img.center, [5, 0, 2.5])
    img.rotation = 90
    assert np.allclose(img.corners()[0], [0, 0, 0]), "turned about the anchor"
    img.width = 20
    assert np.allclose(img.corners()[0], [0, 0, 0]), "grown about the anchor"
    img.anchor = (1.0, 1.0)          # the image moves so its top right is at the position
    assert np.allclose(img.point_at(1, 1), [0, 0, 0])


def test_image_anchor_to_the_paths_same_point_stays_on_its_plane():
    img = RefImage("x.png", width=10, anchor=(0.0, 1.0))
    img.set_plane("XZ")
    img.position = (0, 7, 0)                            # its plane is y = 7
    img.anchor_to_box([2, 0, 3], [12, 4, 9])
    assert np.allclose(img.position, [2, 7, 9]), "top left of the box as seen from the front"


def test_a_model_turns_and_scales_about_its_anchor():
    m = RefModel("cube", _cube_tris(), anchor=(0.5, 0.5, 0.0), position=(5, 5, 0))
    lo, hi = m.bounds()
    assert np.allclose(lo, [4.5, 4.5, 0]) and np.allclose(hi, [5.5, 5.5, 1])
    m.scale, m.rotate = 4, (0, 0, 45)
    lo, hi = m.bounds()
    assert np.isclose(lo[2], 0), "the bottom stays down"
    assert np.allclose((lo + hi)[:2] / 2, [5, 5])
    m.anchor_to_box([0, 0, 10], [20, 20, 30])
    assert np.allclose(m.position, [10, 10, 10]), "bottom centre on the box's"


@pytest.mark.parametrize("anchor, name", [
    ((0.5, 0.5, 0.5), "Center"), ((0.5, 0.5, 1.0), "Top"), ((0.0, 1.0, 0.0), "Bottom Back Left"),
    ((1.0, 0.0, 0.5), "Front Right"),
])
def test_model_anchor_names(anchor, name):
    assert model_anchor_name(anchor) == name
