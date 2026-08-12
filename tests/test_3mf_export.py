"""3MF is written directly, so the file itself is what gets asserted on.

It used to go through lib3mf, which had no aarch64/ARM64 wheels -- .3mf
export did not exist on Linux ARM or Windows on ARM. Writing the package
here removes that gap, and puts the burden of being a valid OPC container
on this file: a ZIP with a content-types part, a relationships part and
the model XML.

Reference values come from what lib3mf produced for the same input while
both implementations existed.
"""
import xml.etree.ElementTree as ET
import zipfile

import pytest

from belfryscad import exporters
from openscad_cpp_evaluator import Evaluator

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MATERIAL = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
NS = {"c": CORE, "m": MATERIAL}


def bodies_for(src, tmp_path):
    path = tmp_path / "t.scad"
    path.write_text(src)
    bodies, _ = Evaluator(echo_fn=lambda _m: None).evaluate(str(path))
    return bodies


def write(src, tmp_path):
    out = tmp_path / "out.3mf"
    exporters.write_3mf(str(out), bodies_for(src, tmp_path))
    return out


def model_of(path):
    with zipfile.ZipFile(path) as z:
        return ET.fromstring(z.read("3D/3dmodel.model"))


# --- the container ----------------------------------------------------
def test_it_is_a_zip_with_the_three_opc_parts(tmp_path):
    out = write("cube(10);", tmp_path)
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist()) == {
            "[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}
        # Every part must be well-formed XML, not just present.
        for name in z.namelist():
            ET.fromstring(z.read(name))


def test_the_relationship_points_at_the_model(tmp_path):
    out = write("cube(10);", tmp_path)
    with zipfile.ZipFile(out) as z:
        rels = ET.fromstring(z.read("_rels/.rels"))
    targets = [r.get("Target") for r in rels]
    assert targets == ["/3D/3dmodel.model"]


# --- geometry ---------------------------------------------------------
def test_a_cube_writes_8_vertices_and_12_triangles(tmp_path):
    # lib3mf produced exactly this for the same source.
    model = model_of(write("cube(10);", tmp_path))
    assert len(model.findall(".//c:vertex", NS)) == 8
    assert len(model.findall(".//c:triangle", NS)) == 12


def test_every_triangle_indexes_a_real_vertex(tmp_path):
    model = model_of(write("sphere(r=6, $fn=16);", tmp_path))
    n = len(model.findall(".//c:vertex", NS))
    for t in model.findall(".//c:triangle", NS):
        for a in ("v1", "v2", "v3"):
            assert 0 <= int(t.get(a)) < n


def test_coordinates_never_use_exponent_notation(tmp_path):
    # ST_Number in the spec forbids it, so a stray "1e-07" is invalid 3MF.
    model = model_of(write("scale(0.0001) cube(1);", tmp_path))
    for v in model.findall(".//c:vertex", NS):
        for a in ("x", "y", "z"):
            assert "e" not in v.get(a).lower(), v.get(a)


# --- objects and the build ---------------------------------------------
def test_each_body_becomes_its_own_object_and_build_item(tmp_path):
    src = "cube(10);\ntranslate([20,0,0]) sphere(r=5, $fn=8);"
    model = model_of(write(src, tmp_path))
    objects = model.findall(".//c:object", NS)
    items = model.findall(".//c:build/c:item", NS)
    assert len(objects) == 2
    assert len(items) == 2
    assert {i.get("objectid") for i in items} == {o.get("id") for o in objects}


def test_every_id_in_the_file_is_unique(tmp_path):
    src = 'color("red") cube(10);\ncolor("blue") translate([20,0,0]) cube(5);'
    model = model_of(write(src, tmp_path))
    ids = [e.get("id") for e in model.iter() if e.get("id") is not None]
    assert len(ids) == len(set(ids)), ids


# --- colour ------------------------------------------------------------
def test_colour_is_carried_on_the_object(tmp_path):
    model = model_of(write('color("red") cube(10);', tmp_path))
    group = model.find(".//m:colorgroup", NS)
    obj = model.find(".//c:object", NS)
    assert group.find("m:color", NS).get("color") == "#FF0000FF"
    assert obj.get("pid") == group.get("id")
    assert obj.get("pindex") == "0"


def test_alpha_survives(tmp_path):
    model = model_of(write("color([0, 1, 0, 0.5]) cube(10);", tmp_path))
    # 0.5 * 255 rounds to 128 -> 0x80
    assert model.find(".//m:color", NS).get("color") == "#00FF0080"


@pytest.mark.parametrize("src,expected", [
    ('color("red") cube(1);', "#FF0000FF"),
    ('color("blue") cube(1);', "#0000FFFF"),
    ('color("white") cube(1);', "#FFFFFFFF"),
    ('color("black") cube(1);', "#000000FF"),
])
def test_named_colours(src, expected, tmp_path):
    assert model_of(write(src, tmp_path)).find(".//m:color", NS).get("color") == expected


# --- what must not be written ------------------------------------------
def test_a_background_body_is_not_exported(tmp_path):
    # Same rule as the other writers -- see test_export_modifiers.py.
    model = model_of(write("%cube(10);", tmp_path))
    assert model.findall(".//c:object", NS) == []
    assert model.findall(".//c:vertex", NS) == []


def test_an_empty_model_still_writes_a_valid_package(tmp_path):
    out = write("", tmp_path)
    with zipfile.ZipFile(out) as z:
        assert "3D/3dmodel.model" in z.namelist()
    model = model_of(out)
    assert model.find("c:resources", NS) is not None
    assert model.find("c:build", NS) is not None


# --- readability ------------------------------------------------------
# The model part is deliberately pretty-printed so it can be read when
# someone unzips a .3mf. It costs ~1.8% compressed (2MB raw of tabs that
# deflate almost entirely away), and that trade was made on purpose.
def test_the_model_part_is_indented_with_tabs(tmp_path):
    out = write("cube(10);", tmp_path)
    with zipfile.ZipFile(out) as z:
        text = z.read("3D/3dmodel.model").decode()
    lines = text.splitlines()
    assert any(ln.startswith("\t<resources>") for ln in lines), lines[:4]
    assert any(ln.startswith("\t\t\t\t\t<vertex ") for ln in lines)
    assert any(ln.startswith("\t\t\t\t\t<triangle ") for ln in lines)
    assert any(ln.startswith("\t\t<item ") for ln in lines)


def test_empty_elements_keep_the_space_before_the_slash(tmp_path):
    out = write('color("red") cube(10);', tmp_path)
    with zipfile.ZipFile(out) as z:
        text = z.read("3D/3dmodel.model").decode()
    assert "<vertex " in text and '"/>' not in text.replace(' />', '')
    for frag in ("<vertex ", "<triangle ", "<item ", "<m:color "):
        line = next(ln for ln in text.splitlines() if frag in ln)
        assert line.rstrip().endswith(" />"), line
