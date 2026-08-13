"""Top level is an implicit union, and the formats that can hold more than
one object get that union cut into non-overlapping pieces.

`cube(100); cube(100, center=true);` used to export as two overlapping
objects. Real OpenSCAD 2022.08.22 writes ONE object of 20 vertices and 36
triangles for that script -- checked directly -- which is the reference the
union half of this is measured against. The colour half goes beyond
OpenSCAD, which drops colour on 3MF export entirely.
"""
import itertools
import os
import struct

import numpy as np
import pytest

from belfryscad import exporters
from openscad_cpp_evaluator import Evaluator, to_renderable_bodies

manifold3d = pytest.importorskip("manifold3d")


def objects_for(src, tmp_path):
    path = tmp_path / "t.scad"
    path.write_text(src)
    bodies, _ = Evaluator(echo_fn=lambda _m: None).evaluate(str(path))
    return exporters.split_bodies_for_export(to_renderable_bodies(bodies))


def as_manifold(verts, tris):
    # Copies, deliberately: the arrays come back as read-only views of
    # evaluator-owned buffers, which manifold3d's binding refuses.
    return manifold3d.Manifold(manifold3d.Mesh(
        np.array(verts, dtype=np.float32), np.array(tris, dtype=np.uint32)))


def volumes(objects):
    return [as_manifold(v, t).volume() for v, t, _c in objects]


# --- rule 1: the implicit union ---------------------------------------
def test_two_overlapping_bodies_become_one_object(tmp_path):
    objects = objects_for("cube(100, center=false); cube(100, center=true);", tmp_path)
    assert len(objects) == 1
    verts, tris, _rgba = objects[0]
    # Exactly what real OpenSCAD writes for this script.
    assert len(verts) == 20
    assert len(tris) == 36


def test_the_union_keeps_the_right_volume(tmp_path):
    # 100^3 twice, less the 50^3 they share.
    objects = objects_for("cube(100, center=false); cube(100, center=true);", tmp_path)
    assert volumes(objects)[0] == pytest.approx(2 * 100**3 - 50**3, rel=1e-6)


# --- rule 2: one object per colour, none overlapping -------------------
def test_each_colour_becomes_its_own_object(tmp_path):
    objects = objects_for('color("red") cube(100);\n'
                          'color("blue") translate([50,50,50]) cube(100);', tmp_path)
    assert len(objects) == 2
    assert [tuple(o[2]) for o in objects] == [(1.0, 0.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0)]


def test_the_later_colour_wins_the_shared_volume(tmp_path):
    # blue is declared second, so it keeps its whole 100^3 and red is
    # notched by the 50^3 they share.
    objects = objects_for('color("red") cube(100);\n'
                          'color("blue") translate([50,50,50]) cube(100);', tmp_path)
    red, blue = volumes(objects)
    assert blue == pytest.approx(100**3, rel=1e-6)
    assert red == pytest.approx(100**3 - 50**3, rel=1e-6)


@pytest.mark.parametrize("src", [
    'color("red") cube(100);\ncolor("blue") translate([50,50,50]) cube(100);',
    'color("red") cube(100);\ncolor("green") translate([30,30,30]) cube(100);\n'
    'color("blue") translate([60,60,60]) cube(100);',
    'color("red") sphere(50,$fn=24);\ncolor("blue") cube(60, center=true);',
])
def test_no_two_objects_occupy_the_same_space(src, tmp_path):
    """The core guarantee: the parts tile the union, they don't overlap it."""
    objects = objects_for(src, tmp_path)
    mans = [as_manifold(v, t) for v, t, _c in objects]
    for a, b in itertools.combinations(mans, 2):
        assert (a ^ b).volume() == pytest.approx(0.0, abs=1e-6)
    union = manifold3d.Manifold.batch_boolean(mans, manifold3d.OpType.Add)
    assert sum(m.volume() for m in mans) == pytest.approx(union.volume(), rel=1e-6)


def test_same_coloured_overlapping_shapes_merge(tmp_path):
    objects = objects_for('color("red") cube(100);\n'
                          'color("red") translate([50,50,50]) cube(100);', tmp_path)
    assert len(objects) == 1
    assert volumes(objects)[0] == pytest.approx(2 * 100**3 - 50**3, rel=1e-6)


# --- rule 3: unconnected parts split ----------------------------------
def test_disconnected_parts_become_separate_objects(tmp_path):
    objects = objects_for("cube(10);\ntranslate([50,0,0]) cube(10);", tmp_path)
    assert len(objects) == 2
    assert all(v == pytest.approx(1000.0, rel=1e-6) for v in volumes(objects))


def test_touching_parts_stay_one_object(tmp_path):
    # Abutting, not disjoint -- the union is connected, so it is one object.
    objects = objects_for("cube(10);\ntranslate([10,0,0]) cube(10);", tmp_path)
    assert len(objects) == 1
    assert volumes(objects)[0] == pytest.approx(2000.0, rel=1e-6)


def test_a_background_body_is_still_excluded(tmp_path):
    assert objects_for("%cube(10);", tmp_path) == []


# --- OBJ ---------------------------------------------------------------
def test_obj_writes_one_group_per_object(tmp_path):
    out = tmp_path / "m.obj"
    exporters.write_obj(str(out), objects_for(
        'color("red") cube(10);\ncolor("blue") translate([50,0,0]) cube(10);', tmp_path))
    text = out.read_text()
    assert text.count("\no object_") + text.startswith("o object_") == 2
    assert "usemtl color_1" in text and "usemtl color_2" in text
    assert "mtllib m.mtl" in text


def test_obj_vertex_indices_are_file_global_and_valid(tmp_path):
    out = tmp_path / "m.obj"
    exporters.write_obj(str(out), objects_for(
        "cube(10);\ntranslate([50,0,0]) sphere(5,$fn=8);", tmp_path))
    lines = out.read_text().splitlines()
    nverts = sum(1 for ln in lines if ln.startswith("v "))
    for ln in lines:
        if ln.startswith("f "):
            for idx in ln.split()[1:]:
                assert 1 <= int(idx) <= nverts


def test_obj_writes_a_companion_mtl(tmp_path):
    out = tmp_path / "m.obj"
    exporters.write_obj(str(out), objects_for(
        'color("red") cube(10);\ncolor([0,0,1,0.5]) translate([50,0,0]) cube(10);',
        tmp_path))
    mtl = (tmp_path / "m.mtl").read_text()
    assert "newmtl color_1" in mtl and "Kd 1 0 0" in mtl
    assert "newmtl color_2" in mtl and "Kd 0 0 1" in mtl
    assert "d 0.5" in mtl          # alpha survives as opacity


def test_obj_reuses_one_material_per_distinct_colour(tmp_path):
    out = tmp_path / "m.obj"
    exporters.write_obj(str(out), objects_for(
        'color("red") cube(10);\ncolor("red") translate([50,0,0]) cube(10);', tmp_path))
    mtl = (tmp_path / "m.mtl").read_text()
    assert mtl.count("newmtl ") == 1
    assert out.read_text().count("usemtl color_1") == 2


# --- PLY ---------------------------------------------------------------
def read_ply(path):
    data = path.read_bytes()
    head, body = data.split(b"end_header\n", 1)
    header = head.decode("ascii")
    nv = int(next(ln for ln in header.splitlines()
                  if ln.startswith("element vertex")).split()[-1])
    nf = int(next(ln for ln in header.splitlines()
                  if ln.startswith("element face")).split()[-1])
    vsize = 3 * 4 + 3       # xyz float32 + rgb uchar
    verts = [struct.unpack_from("<fffBBB", body, i * vsize) for i in range(nv)]
    off = nv * vsize
    faces = []
    for i in range(nf):
        n = body[off]
        assert n == 3
        faces.append(struct.unpack_from("<iii", body, off + 1))
        off += 1 + 12
    return header, verts, faces


def test_ply_is_binary_little_endian_with_vertex_colour(tmp_path):
    out = tmp_path / "m.ply"
    exporters.write_ply(str(out), objects_for("cube(10);", tmp_path))
    header, verts, faces = read_ply(out)
    assert header.startswith("ply\nformat binary_little_endian 1.0\n")
    assert "property uchar red" in header
    assert len(verts) == 8 and len(faces) == 12


def test_ply_carries_each_objects_colour_on_its_vertices(tmp_path):
    out = tmp_path / "m.ply"
    exporters.write_ply(str(out), objects_for(
        'color("red") cube(10);\ncolor("blue") translate([50,0,0]) cube(10);', tmp_path))
    _header, verts, _faces = read_ply(out)
    colours = {v[3:] for v in verts}
    assert colours == {(255, 0, 0), (0, 0, 255)}


def test_ply_face_indices_are_in_range(tmp_path):
    out = tmp_path / "m.ply"
    exporters.write_ply(str(out), objects_for(
        "cube(10);\ntranslate([50,0,0]) sphere(5,$fn=8);", tmp_path))
    _header, verts, faces = read_ply(out)
    for f in faces:
        for i in f:
            assert 0 <= i < len(verts)


def test_ply_of_an_empty_model_is_still_valid(tmp_path):
    out = tmp_path / "m.ply"
    exporters.write_ply(str(out), [])
    header, verts, faces = read_ply(out)
    assert "element vertex 0" in header
    assert verts == [] and faces == []


# --- open shells still get through ------------------------------------
def test_an_open_shell_keeps_its_own_object_and_is_reported(tmp_path):
    # A single triangle is not a closed solid; Manifold discards it, so it
    # cannot join the union -- it is written as-is and reported instead.
    src = "polyhedron(points=[[0,0,0],[10,0,0],[0,10,0]], faces=[[0,1,2]]);"
    path = tmp_path / "t.scad"
    path.write_text(src)
    bodies, _ = Evaluator(echo_fn=lambda _m: None).evaluate(str(path))
    open_parts = []
    objects = exporters.split_bodies_for_export(
        to_renderable_bodies(bodies), open_parts)
    assert open_parts == [1]
    assert len(objects) == 1
    assert len(objects[0][1]) == 1          # its one triangle survived
