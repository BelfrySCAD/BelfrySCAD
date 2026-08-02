"""Tests for belfryscad.headless -- the -o/-D CLI export path. No Qt/GL
dependency (real openscad_cpp_evaluator + plain file I/O only), so unlike
the GUI code this is directly pytest-testable -- see test_customizer.py's
own module docstring for why the rest of the suite avoids real Qt widgets.
"""

import struct

import pytest

from belfryscad.headless import build_define_prelude, render_and_export


class TestBuildDefinePrelude:
    def test_single_define(self):
        assert build_define_prelude(["x=5"]) == "x=5;\n"

    def test_multiple_defines(self):
        assert build_define_prelude(["x=5", "y=10"]) == "x=5;\ny=10;\n"

    def test_string_value(self):
        assert build_define_prelude(['name="foo"']) == 'name="foo";\n'

    def test_value_containing_equals(self):
        # Splits on the FIRST "=" only, so a value like a comparison
        # expression doesn't get truncated.
        assert build_define_prelude(["ok=(1==1)"]) == "ok=(1==1);\n"

    def test_empty_list(self):
        assert build_define_prelude([]) == ""

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError):
            build_define_prelude(["x5"])

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            build_define_prelude(["=5"])


def _stl_vertex_xs(path) -> set[int]:
    """Read a binary STL (belfryscad.exporters.write_stl's own format) and
    return the set of rounded X coordinates across every vertex."""
    with open(path, "rb") as f:
        f.read(80)
        n = struct.unpack("<I", f.read(4))[0]
        data = f.read()
    xs = set()
    for i in range(n):
        rec = data[i * 50:(i + 1) * 50]
        vals = struct.unpack("<12fH", rec)
        xs.update([round(vals[3]), round(vals[6]), round(vals[9])])
    return xs


class TestRenderAndExport:
    def test_basic_export_succeeds(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        out = tmp_path / "out.stl"
        code = render_and_export(str(src), str(out))
        assert code == 0
        assert out.exists()
        assert _stl_vertex_xs(out) == {0, 10}

    def test_define_overrides_script_value(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("x = 10;\ncube([x, 5, 3]);\n")
        out = tmp_path / "out.stl"
        code = render_and_export(str(src), str(out), defines=["x=20"])
        assert code == 0
        assert _stl_vertex_xs(out) == {0, 20}

    def test_define_wins_over_later_script_reassignment(self, tmp_path):
        # Regression case: real OpenSCAD's -D locks a top-level variable for
        # the WHOLE script, immune to any of the script's own reassignments
        # -- confirmed directly against real OpenSCAD.app (see
        # belfryscad.headless's own doc comment). A plain "prepend the
        # override" implementation would fail this: the script's own LATER
        # `x = 5;` would win under ordinary last-assignment-wins semantics.
        src = tmp_path / "in.scad"
        src.write_text(
            "translate([0,0,0]) cube([x, 1, 1]);\n"
            "x = 5;\n"
            "translate([0,10,0]) cube([x, 2, 2]);\n"
        )
        out = tmp_path / "out.stl"
        code = render_and_export(str(src), str(out), defines=["x=20"])
        assert code == 0
        assert _stl_vertex_xs(out) == {0, 20}  # never 5 -- both cubes locked to the override

    def test_define_does_not_affect_module_local_variable(self, tmp_path):
        # A module's own local variable of the same name is a genuinely
        # separate binding (real OpenSCAD confirmed: module-local x=99
        # untouched by a top-level -D x=20).
        src = tmp_path / "in.scad"
        src.write_text(
            "module m() { x = 99; cube([x, 3, 3]); }\n"
            "translate([0,0,0]) cube([x, 1, 1]);\n"
            "translate([0,20,0]) m();\n"
        )
        out = tmp_path / "out.stl"
        code = render_and_export(str(src), str(out), defines=["x=20"])
        assert code == 0
        assert _stl_vertex_xs(out) == {0, 20, 99}

    def test_missing_input_file_fails(self, tmp_path):
        out = tmp_path / "out.stl"
        code = render_and_export(str(tmp_path / "nope.scad"), str(out))
        assert code == 1
        assert not out.exists()

    def test_unsupported_output_extension_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.png"))
        assert code == 1

    def test_no_geometry_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("x = 5;\n")  # a bare assignment, no geometry statement
        code = render_and_export(str(src), str(tmp_path / "out.stl"))
        assert code == 1

    def test_bad_define_syntax_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), defines=["not_an_assignment"])
        assert code == 1

    def test_obj_export_succeeds(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        out = tmp_path / "out.obj"
        code = render_and_export(str(src), str(out))
        assert code == 0
        text = out.read_text()
        assert text.startswith("v ")
        assert "\nf " in text
