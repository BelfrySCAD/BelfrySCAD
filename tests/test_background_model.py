"""#882: a model behind the 2D editors, cut along the axis it is seen down."""
import numpy as np
import pytest

from belfryscad.window.background_image import BackgroundModel, _same_model


def _box(lo, hi):
    """A closed box's 12 triangles."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], dtype=float)
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v[np.array(faces)]


def test_each_view_maps_the_mesh_onto_x_and_y():
    m = BackgroundModel("b", _box((1, 2, 3), (4, 6, 9)))
    assert m.bounds() == pytest.approx((1, 2, 4, 6))
    m.plane = "XZ"            # from the front: X right, Z up
    assert m.bounds() == pytest.approx((1, 3, 4, 9))
    m.plane = "YZ"            # from the right: Y right, Z up
    assert m.bounds() == pytest.approx((2, 3, 6, 9))


def test_centre_cut_goes_through_the_middle_of_the_view_axis():
    m = BackgroundModel("b", _box((0, 0, 0), (10, 20, 30)), plane="XZ")
    m.centre_cut()
    assert m.axis == 1 and m.depth == pytest.approx(10)


def test_outline_is_the_cut_at_z_zero_with_placement_applied():
    m = BackgroundModel("b", _box((-1, -2, -3), (1, 2, 3)), plane="XZ", x=5, y=7, scale=2)
    seg = m.outline()
    pts = seg.reshape(-1, 3)
    assert len(seg) and np.all(pts[:, 2] == 0)
    # The box's front-view outline, doubled and moved to (5, 7).
    assert pts[:, 0].min() == pytest.approx(3) and pts[:, 0].max() == pytest.approx(7)
    assert pts[:, 1].min() == pytest.approx(1) and pts[:, 1].max() == pytest.approx(13)


def test_a_cut_outside_the_mesh_draws_no_outline():
    m = BackgroundModel("b", _box((0, 0, 0), (1, 1, 1)), depth=5)
    assert len(m.outline()) == 0


def test_same_model_compares_triangles_by_identity():
    tris = _box((0, 0, 0), (1, 1, 1))
    a = BackgroundModel("b", tris)
    assert _same_model(a, BackgroundModel("b", tris))
    assert not _same_model(a, BackgroundModel("b", tris.copy()))
    assert not _same_model(a, BackgroundModel("b", tris, depth=1))
    assert _same_model(None, None) and not _same_model(a, None)
