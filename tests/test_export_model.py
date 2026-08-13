"""The export interface, now that the writers live in the evaluator.

The formats themselves -- the object split, per-triangle colour, the exact
bytes of each writer -- are tested in openscad_cpp_evaluator's own suite,
which is where that code now is. What is worth testing here is the boundary:
that the handle reaches export_model, that every format the dialog offers
actually writes, and that warnings come back rather than being swallowed.

test_3mf_export.py and test_export_object_split.py were retired with the
Python writers they covered; their assertions live on in the C++ tests.
"""
import pytest

from belfryscad import exporters
from openscad_cpp_evaluator import Evaluator


def geometry_for(src, tmp_path):
    path = tmp_path / "t.scad"
    path.write_text(src)
    ev = Evaluator(echo_fn=lambda _m: None)
    ev.evaluate(str(path))
    return ev.geometry


def test_evaluate_leaves_a_geometry_handle(tmp_path):
    g = geometry_for("cube(10);", tmp_path)
    assert g is not None
    assert not g.is_empty()
    assert len(g) == 1


def test_the_handle_is_not_the_body_list(tmp_path):
    """It is opaque on purpose: the point is that geometry never round-trips
    through numpy just to be exported."""
    g = geometry_for("cube(10);", tmp_path)
    assert not isinstance(g, list)
    assert not hasattr(g, "to_mesh")


@pytest.mark.parametrize("ext", [".3mf", ".stl", ".obj", ".off", ".ply", ".wrl", ".x3d"])
def test_every_format_writes_a_non_empty_file(ext, tmp_path):
    g = geometry_for('color("red") cube(10);', tmp_path)
    out = tmp_path / f"m{ext}"
    assert exporters.export_model(str(out), g) == []
    assert out.is_file() and out.stat().st_size > 0


def test_obj_writes_its_companion_mtl(tmp_path):
    g = geometry_for('color("red") cube(10);', tmp_path)
    out = tmp_path / "m.obj"
    exporters.export_model(str(out), g)
    assert (tmp_path / "m.mtl").is_file()


def test_format_can_override_the_extension(tmp_path):
    g = geometry_for("cube(10);", tmp_path)
    out = tmp_path / "named_wrong.dat"
    exporters.export_model(str(out), g, format="ply")
    assert out.read_bytes().startswith(b"ply\n")


def test_ascii_stl_is_opt_in(tmp_path):
    g = geometry_for("cube(10);", tmp_path)
    binary, ascii_ = tmp_path / "b.stl", tmp_path / "a.stl"
    exporters.export_model(str(binary), g)
    exporters.export_model(str(ascii_), g, ascii_stl=True)
    assert not binary.read_bytes().startswith(b"solid")
    # No trailing newline in the assertion: the text formats are written in
    # text mode, so Windows gets CRLF here. That matches what the Python
    # writer did before the port (open(path, "w") translates newlines too),
    # so it is the existing behaviour rather than a regression -- but it
    # does mean the text formats are not byte-identical across platforms.
    # PLY is unaffected; its header rides on a binary-mode stream.
    assert ascii_.read_bytes().startswith(b"solid OpenSCAD_Model")
    assert b"endsolid OpenSCAD_Model" in ascii_.read_bytes()


def test_the_extension_list_matches_what_the_dialog_offers():
    from belfryscad.window.main_window import _EXPORT_FORMATS

    offered = {e for _f, e in _EXPORT_FORMATS}
    assert offered <= set(exporters.export_extensions())


# --- warnings come back, they are not swallowed -----------------------
def test_an_open_shell_is_reported_and_still_written(tmp_path):
    """Nothing here refuses to write: a deliberately open surface is a
    legitimate export, so it is warned about and written anyway."""
    src = "polyhedron(points=[[0,0,0],[10,0,0],[0,10,0]], faces=[[0,1,2]]);"
    g = geometry_for(src, tmp_path)
    out = tmp_path / "m.3mf"
    warnings = exporters.export_model(str(out), g)
    assert any("not a closed solid" in w for w in warnings), warnings
    assert out.is_file() and out.stat().st_size > 0


def test_a_sound_model_warns_about_nothing(tmp_path):
    g = geometry_for("cube(10);", tmp_path)
    assert exporters.export_model(str(tmp_path / "m.3mf"), g) == []


# --- failures are errors, not silence ---------------------------------
def test_an_unknown_format_raises(tmp_path):
    g = geometry_for("cube(10);", tmp_path)
    with pytest.raises(Exception, match="(?i)format"):
        exporters.export_model(str(tmp_path / "m.xyz"), g)


def test_an_empty_model_raises_rather_than_writing_nothing(tmp_path):
    g = geometry_for("// nothing here\n", tmp_path)
    with pytest.raises(Exception, match="(?i)geometry"):
        exporters.export_model(str(tmp_path / "m.3mf"), g)


# --- the union rule, end to end ---------------------------------------
def test_top_level_is_an_implicit_union(tmp_path):
    """The bug that started all of this: two overlapping top-level cubes are
    one solid. Real OpenSCAD 2022.08.22 writes 20 vertices and 36 triangles
    for this script; asserted here because it is the behaviour the GUI
    depends on, whichever side of the boundary implements it."""
    import re
    import zipfile

    g = geometry_for("cube(100, center=false); cube(100, center=true);", tmp_path)
    out = tmp_path / "m.3mf"
    exporters.export_model(str(out), g)
    xml = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
    assert len(re.findall(r"<object ", xml)) == 1
    assert len(re.findall(r"<vertex ", xml)) == 20
    assert len(re.findall(r"<triangle ", xml)) == 36
