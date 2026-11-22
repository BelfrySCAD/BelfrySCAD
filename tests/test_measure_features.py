"""#1131: edges and faces recovered from triangle soup, and what two of them measure."""
import numpy as np
import manifold3d as m

from belfryscad.engine.mesh_features import Feature, MeshTopology, describe, measure
from belfryscad.window.viewport import Measurement


def _soup(man):
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    t = np.asarray(mesh.tri_verts)
    return v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]


def _tri_on(topo, normal):
    return int(np.nonzero(np.all(np.isclose(topo.normal, normal), axis=1))[0][0])


def test_a_cube_side_is_one_face_and_its_diagonal_is_not_an_edge():
    topo = MeshTopology(*_soup(m.Manifold.cube([10, 10, 10])))
    assert topo.feature.sum() == 12                  # 18 edges, 6 of them diagonals
    face = topo.face_of(_tri_on(topo, [0, 0, 1]))
    assert len(face.tris) == 2 and face.area == 100
    assert np.allclose(face.centroid, [5, 5, 10]) and len(face.outline) == 4


def test_an_edge_split_by_a_vertex_measures_its_whole_length():
    # Two boxes side by side: the long top edge is cut where they meet.
    soup = _soup(m.Manifold.cube([10, 10, 10]) + m.Manifold.cube([10, 10, 10]).translate([10, 0, 0]))
    topo = MeshTopology(*soup)
    tri = _tri_on(topo, [0, 0, 1])
    lengths = sorted(float(np.linalg.norm(b - a)) for a, b in
                     (topo.edge_chain(e) for e in topo.feature_edges_of(tri)))
    assert np.isclose(lengths[-1], 20)
    assert np.isclose(topo.face_of(tri).area, 200)


def test_each_facet_of_a_cylinder_is_its_own_face():
    topo = MeshTopology(*_soup(m.Manifold.cylinder(10, 5, 5, 8)))
    side = [t for t in range(len(topo.vid)) if abs(topo.normal[t, 2]) < 1e-9][0]
    assert len(topo.face_of(side).tris) == 2


def _edge(a, b):
    a, b = np.array(a, float), np.array(b, float)
    return Feature("edge", (a + b) / 2, a=a, b=b)


def _point(p, tag="vertex"):
    return Feature("point", np.array(p, float), tag)


def test_point_point_and_the_issues_two_parallel_edges():
    lines, head, p, q = measure(_point([0, 0, 0]), _point([10, 0, 0]))
    assert head == "10" and lines[0].startswith("distance: 10  (dx 10, dy 0, dz 0)")
    lines, head, p, q = measure(_edge([0, -5, 0], [0, 5, 0]), _edge([10, -5, 0], [10, 5, 0]))
    assert lines == ["minimum distance from edge1 to edge2: 10", "angle from edge1 to edge2: 0"]
    assert np.isclose(np.linalg.norm(q - p), 10)


def test_point_to_edge_gives_the_segment_and_the_line():
    lines, head, p, q = measure(_edge([0, 0, 0], [10, 0, 0]), _point([13, 4, 0]))
    assert lines == ["minimum distance from point2 to edge1: 5",
                     "distance from point2 to the line of edge1: 4"]
    assert np.allclose(q, [10, 0, 0])


def test_face_measurements_use_its_plane():
    topo = MeshTopology(*_soup(m.Manifold.cube([10, 10, 10])))
    top = topo.face_of(_tri_on(topo, [0, 0, 1]))
    bottom = topo.face_of(_tri_on(topo, [0, 0, -1]))
    side = topo.face_of(_tri_on(topo, [1, 0, 0]))
    ftop, fbot, fside = (Feature("face", f.centroid, face=f) for f in (top, bottom, side))
    assert measure(_point([3, 3, 4]), ftop)[0] == ["distance from point1 to the plane of face2: -6"]
    lines, head, p, q = measure(ftop, fbot)
    assert lines == ["angle between the normals of face1 and face2: 180",
                     "distance between the planes of face1 and face2: 10"]
    assert measure(ftop, fside)[1] == "90°"
    lines, head, _, _ = measure(_edge([0, 0, 12], [10, 0, 12]), ftop)
    assert lines == ["angle from edge1 to the plane of face2: 0",
                     "distance from edge1 to the plane of face2: 2"]
    assert measure(_edge([0, 0, 0], [0, 10, 10]), ftop)[1] == "45°"
    assert describe(ftop, 1) == "selection 1 face normal [0, 0, 1] area 100 centroid [5, 5, 10]"


def test_a_features_measurement_labels_with_its_headline():
    a, b = _point([0, 0, 0]), _point([3, 4, 0], "edge cp")
    lines, head, p, q = measure(a, b)
    meas = Measurement("features", [p, q], ["vertex", "edge cp"], features=[a, b], lines=lines, head=head)
    assert meas.label() == "5" and meas.where() == ""
    assert describe(b, 2) == "selection 2 point [3, 4, 0] [edge cp]"
