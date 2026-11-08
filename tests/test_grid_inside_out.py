"""#1081: the Grid editor faces each surface the way BOSL2's own VNF for it
does, and draws the back magenta, as the main viewport does.

BOSL2 (checked by volume sign and face normals): with rows stepping +y and
columns +x, vnf_vertex_array() and nurbs_vnf() face -z, bezier_vnf() +z.
"""
import numpy as np

from belfryscad.window.data_viewer_grid import _INSIDE_OUT, _as_bosl2, _surface_mesh

GRID = np.array([[[x * 10.0, y * 10.0, 0.0] for x in range(3)] for y in range(3)])


def _facing(pos):
    t = np.asarray(pos).reshape(-1, 3, 3)
    return {float(np.sign(np.cross(a[1] - a[0], a[2] - a[0])[2])) for a in t}


def test_built_facing_plus_z_and_bosl2_facing_minus_z():
    pos, norm = _surface_mesh(GRID)
    assert _facing(pos) == {1.0}                       # as built: bezier_vnf's way
    fpos, fnorm = _as_bosl2(pos, norm)
    assert _facing(fpos) == {-1.0}                     # vnf_vertex_array / nurbs_vnf
    assert set(np.sign(fnorm[:, 2])) == {-1.0}         # normals follow the winding


def test_back_faces_are_magenta():
    assert _INSIDE_OUT == (1.0, 0.0, 1.0, 1.0)
