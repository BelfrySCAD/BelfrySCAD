"""Each condition gets a mesh that fails it and nothing else.

A validator that flags a broken mesh proves little -- a broken mesh
usually breaks several ways at once. So every case here starts from a
sound cube and introduces exactly one defect, and asserts both that the
right condition fires and that the others stay quiet.
"""
import numpy as np

from belfryscad.vnf_validate import validate_vnf

# A closed, correctly wound cube: 8 corners, 12 triangles, outward normals.
CUBE_PTS = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],
            [0, 0, 10], [10, 0, 10], [10, 10, 10], [0, 10, 10]]
CUBE_TRIS = [
    [0, 2, 1], [0, 3, 2],        # bottom (-Z)
    [4, 5, 6], [4, 6, 7],        # top (+Z)
    [0, 1, 5], [0, 5, 4],        # front (-Y)
    [1, 2, 6], [1, 6, 5],        # right (+X)
    [2, 3, 7], [2, 7, 6],        # back (+Y)
    [3, 0, 4], [3, 4, 7],        # left (-X)
]


def cube():
    return [[p[:] for p in CUBE_PTS], [t[:] for t in CUBE_TRIS]]


# --- the control -------------------------------------------------------
def test_a_sound_cube_reports_nothing():
    r = validate_vnf(cube())
    assert r.ok, r.summary()
    assert r.hole_edges == [] and r.flipped_edges == []
    assert r.t_joints == [] and r.intersecting == [] and r.overlapping == []
    assert "No problems found" in r.summary()


def test_the_control_is_a_real_closed_solid():
    # If the cube were not actually closed, every "and nothing else"
    # assertion below would be vacuous.
    r = validate_vnf(cube())
    assert r.nonmanifold_edges == []
    assert len(r.welded_points) == 8


# --- holes -------------------------------------------------------------
def test_a_removed_face_leaves_hole_edges():
    v, f = cube()
    del f[0]                       # one half of the bottom
    r = validate_vnf([v, f])
    assert len(r.hole_edges) == 3, r.summary()
    assert not r.flipped_edges and not r.intersecting
    assert "hole edge" in r.summary()


def test_hole_edges_name_the_actual_boundary():
    # Indices in the report are post-weld, so the expectation goes through
    # remap rather than assuming the input numbering survived.
    v, f = cube()
    f.pop(0)                       # [0, 2, 1]
    r = validate_vnf([v, f])
    m = r.remap
    got = {tuple(sorted(e)) for e in r.hole_edges}
    want = {tuple(sorted((int(m[a]), int(m[b])))) for a, b in ((0, 2), (1, 2), (0, 1))}
    assert got == want, (got, want)


# --- flipped normals ---------------------------------------------------
def test_a_reversed_face_is_reported_as_flipped():
    v, f = cube()
    f[0] = list(reversed(f[0]))    # same triangle, opposite winding
    r = validate_vnf([v, f])
    assert r.flipped_edges, r.summary()
    assert not r.hole_edges        # still closed, just inconsistent
    assert "flipped-normal edge" in r.summary()


def test_flipped_edges_are_the_ones_the_reversed_face_touches():
    v, f = cube()
    f[0] = list(reversed(f[0]))
    r = validate_vnf([v, f])
    m = r.remap
    touched = {tuple(sorted((int(m[a]), int(m[b])))) for a, b in ((0, 2), (1, 2), (0, 1))}
    assert {tuple(sorted(e)) for e in r.flipped_edges} <= touched


# --- T-joints ----------------------------------------------------------
def test_a_vertex_in_the_middle_of_an_edge_is_a_t_joint():
    v, f = cube()
    v.append([5, 0, 0])            # midpoint of the 0-1 edge
    mid = len(v) - 1
    # A face using the midpoint, so the vertex is real geometry rather
    # than an unreferenced point.
    f.append([mid, 1, 5])
    r = validate_vnf([v, f])
    assert r.t_joints, r.summary()
    m = r.remap
    vert, edge = r.t_joints[0]
    assert vert == int(m[mid])
    assert tuple(sorted(edge)) == tuple(sorted((int(m[0]), int(m[1]))))
    assert "T-joint" in r.summary()


def test_a_corner_is_not_a_t_joint():
    # Endpoints must not count, or every edge would report two.
    r = validate_vnf(cube())
    assert r.t_joints == []


# --- intersecting faces ------------------------------------------------
def test_two_triangles_passing_through_each_other():
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0],        # in the z=0 plane
         [5, -5, -5], [5, 5, -5], [5, 0, 5]]       # standing across it
    f = [[0, 1, 2], [3, 4, 5]]
    r = validate_vnf([v, f])
    assert r.intersecting == [(0, 1)], r.summary()
    assert "intersecting face pair" in r.summary()


def test_triangles_that_merely_share_an_edge_do_not_intersect():
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0], [10, 10, 0]]
    f = [[0, 1, 2], [1, 3, 2]]
    r = validate_vnf([v, f])
    assert r.intersecting == []


def test_separated_triangles_do_not_intersect():
    v = [[0, 0, 0], [1, 0, 0], [0, 1, 0],
         [0, 0, 50], [1, 0, 50], [0, 1, 50]]
    f = [[0, 1, 2], [3, 4, 5]]
    r = validate_vnf([v, f])
    assert r.intersecting == []


# --- coplanar overlap --------------------------------------------------
def test_two_coplanar_triangles_covering_the_same_area():
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0],
         [1, 1, 0], [11, 1, 0], [1, 11, 0]]
    f = [[0, 1, 2], [3, 4, 5]]
    r = validate_vnf([v, f])
    assert r.overlapping == [(0, 1)], r.summary()
    assert not r.intersecting          # coplanar is its own category
    assert "overlapping coplanar pair" in r.summary()


def test_coplanar_triangles_side_by_side_do_not_overlap():
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0],
         [20, 0, 0], [30, 0, 0], [20, 10, 0]]
    f = [[0, 1, 2], [3, 4, 5]]
    r = validate_vnf([v, f])
    assert r.overlapping == []


def test_parallel_but_separated_planes_do_not_overlap():
    v = [[0, 0, 0], [10, 0, 0], [0, 10, 0],
         [0, 0, 5], [10, 0, 5], [0, 10, 5]]
    f = [[0, 1, 2], [3, 4, 5]]
    r = validate_vnf([v, f])
    assert r.overlapping == [] and r.intersecting == []


# --- welding -----------------------------------------------------------
def test_coincident_vertices_are_welded_before_checking():
    # Two separately indexed copies of the same corner look like a hole
    # to an index-based check; welding by position is what a slicer does.
    v, f = cube()
    v.append(list(v[0]))               # duplicate of corner 0
    dup = len(v) - 1
    f[0] = [dup, 2, 1]                 # one face uses the copy
    r = validate_vnf([v, f])
    assert r.ok, r.summary()
    assert any("welded" in n for n in r.notes)


# --- polygons ----------------------------------------------------------
def test_quad_faces_are_accepted():
    v = CUBE_PTS[:]
    quads = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4],
             [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    r = validate_vnf([v, quads])
    assert r.ok, r.summary()


def test_a_quad_mesh_with_a_missing_face_still_reports_holes():
    v = CUBE_PTS[:]
    quads = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4],
             [1, 2, 6, 5], [2, 3, 7, 6]]        # left face gone
    r = validate_vnf([v, quads])
    assert len(r.hole_edges) == 4, r.summary()


# --- degenerate input --------------------------------------------------
def test_empty_vnf_is_handled():
    r = validate_vnf([[], []])
    assert not r.hole_edges
    assert "empty" in " ".join(r.notes)


def test_garbage_input_does_not_raise():
    r = validate_vnf(None)
    assert "not a VNF" in " ".join(r.notes)


def test_report_points_survive_for_the_viewer_to_draw():
    r = validate_vnf(cube())
    assert isinstance(r.welded_points, np.ndarray)
    assert r.welded_points.shape == (8, 3)
