"""`%` bodies are scenery, and scenery does not go in the exported file.

The reference leaves a background object out of the model entirely:
`%cube(10);` on its own exports nothing at all. Ours wrote the cube, and
`difference() { cylinder(); %cube(); }` wrote the cube unioned onto the
cylinder -- geometry no boolean had ever accounted for, since `%` is
excluded from the booleans upstream.

Triangle counts here are the reference's own, measured against
OpenSCAD 2021.01 on the same source.
"""
from belfryscad import exporters
from openscad_cpp_evaluator import Evaluator


def bodies_for(src, tmp_path):
    path = tmp_path / "t.scad"
    path.write_text(src)
    bodies, _ = Evaluator(echo_fn=lambda _m: None).evaluate(str(path))
    return bodies


def triangles(src, tmp_path):
    mesh = exporters.merge_bodies_to_mesh(bodies_for(src, tmp_path))
    return 0 if mesh is None else len(mesh.tri_verts)


# The controls: without a modifier, our tessellation already matches the
# reference exactly, so any difference below is the modifier's doing and
# not a facet-count coincidence.
def test_plain_solids_match_the_reference_triangle_for_triangle(tmp_path):
    assert triangles("cylinder(h=10,d=10);", tmp_path) == 60
    assert triangles("cylinder(h=10,d=6);", tmp_path) == 36
    assert triangles("cube(10);", tmp_path) == 12


def test_a_background_body_is_not_exported_at_all(tmp_path):
    # The reference reports "Current top level object is empty" and writes
    # no file; there is nothing here to put in one.
    assert triangles("%cube(10);", tmp_path) == 0


def test_background_is_left_out_of_a_difference(tmp_path):
    # 60 = the cylinder untouched: `%` is not subtracted, and not added.
    assert triangles("difference(){ cylinder(h=10,d=10); %cube(10); }", tmp_path) == 60


def test_background_is_left_out_of_a_union(tmp_path):
    assert triangles("union(){ cylinder(h=10,d=6); %cube(10); }", tmp_path) == 36


def test_the_evaluator_still_hands_the_background_body_over(tmp_path):
    # It is filtered at the export, not dropped earlier -- the viewport
    # needs it to draw the ghost.
    roles = [b.role for b in bodies_for(
        "difference(){ cylinder(h=10,d=10); %cube(10); }", tmp_path)]
    assert sorted(roles) == ["background", "normal"]


def test_a_highlighted_body_is_still_exported(tmp_path):
    # `#` does not change the model, so a highlighted object at top level
    # is real geometry. Both of these match the reference.
    assert triangles("#cube(10);", tmp_path) == 12
    assert triangles("!cube(10);\ncylinder(h=10,d=20);", tmp_path) == 12
