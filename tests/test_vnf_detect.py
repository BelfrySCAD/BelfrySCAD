"""#1081: a two-row grid of points also has a VNF's shape, [list, list];
read as one, its second row's coordinates became face indices past the
end of the vertex list, and opening it crashed the app."""
from belfryscad.window.data_viewer_vnf import _is_vnf

TWO_ROW_GRID = [[[10, 0, 0], [0, 10, 0], [-10, 0, 0], [0, -10, 0]],
                [[10, 0, 20], [0, 10, 20], [-10, 0, 20], [0, -10, 20]]]


def test_a_two_row_grid_is_not_a_vnf():
    assert not _is_vnf(TWO_ROW_GRID)


def test_a_real_vnf_still_is():
    verts = [[0, 0, 0], [10, 0, 0], [0, 10, 0]]
    assert _is_vnf([verts, [[0, 1, 2]]])
    assert _is_vnf([verts, [[0.0, 1.0, 2.0]]])        # integral floats count


def test_faces_must_index_real_vertices():
    verts = [[0, 0, 0], [10, 0, 0], [0, 10, 0]]
    assert not _is_vnf([verts, [[0, 1, 3]]])
    assert not _is_vnf([verts, [[0, 1, -1]]])
    assert not _is_vnf([verts, [[0, 1, 1.5]]])
