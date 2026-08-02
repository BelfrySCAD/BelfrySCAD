"""Tests for belfryscad.headless -- the -o/-D CLI export path. No Qt/GL
dependency (real openscad_cpp_evaluator + plain file I/O only), so unlike
the GUI code this is directly pytest-testable -- see test_customizer.py's
own module docstring for why the rest of the suite avoids real Qt widgets.
"""

import json
import struct

import pytest

from belfryscad.headless import build_define_prelude, render_and_export, render_and_export_animation


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


class TestRenderAndExportAnimation:
    def test_frame_filenames_and_dollar_t_progression(self, tmp_path):
        # File naming (5-digit zero-padded, {stem}{i}{ext}) and $t = i/steps
        # both confirmed directly against real OpenSCAD.app (`--animate 5
        # -o out.stl` -> out00000.stl .. out00004.stl, same width for
        # --animate 150 too).
        src = tmp_path / "in.scad"
        src.write_text("translate([$t*10, 0, 0]) cube(2);\n")
        out = tmp_path / "out.stl"
        code = render_and_export_animation(str(src), str(out), 5)
        assert code == 0
        names = sorted(p.name for p in tmp_path.glob("out*.stl"))
        assert names == [f"out{i:05d}.stl" for i in range(5)]
        assert _stl_vertex_xs(tmp_path / "out00000.stl") == {0, 2}
        assert _stl_vertex_xs(tmp_path / "out00001.stl") == {2, 4}  # $t=0.2 -> x offset 2
        assert _stl_vertex_xs(tmp_path / "out00004.stl") == {8, 10}  # $t=0.8 -> x offset 8

    def test_animate_dir_routes_frames_elsewhere(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        out_dir = tmp_path / "out"
        frames_dir = tmp_path / "frames"
        code = render_and_export_animation(str(src), str(out_dir / "out.stl"), 3, animate_dir=str(frames_dir))
        assert code == 0
        assert sorted(p.name for p in frames_dir.glob("*.stl")) == [f"out{i:05d}.stl" for i in range(3)]
        assert not out_dir.exists()  # never created -- animate_dir wins, matches -o's own dir being unused

    def test_define_applies_to_every_frame(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("translate([$t*10 + offset, 0, 0]) cube(2);\n")
        out = tmp_path / "out.stl"
        code = render_and_export_animation(str(src), str(out), 2, defines=["offset=100"])
        assert code == 0
        assert _stl_vertex_xs(tmp_path / "out00000.stl") == {100, 102}
        assert _stl_vertex_xs(tmp_path / "out00001.stl") == {105, 107}  # $t=0.5 -> +5, plus offset

    def test_zero_steps_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export_animation(str(src), str(tmp_path / "out.stl"), 0)
        assert code == 1

    def test_one_bad_frame_does_not_abort_the_rest(self, tmp_path):
        # A model that only has geometry for $t > 0 -- frame 0 fails (no
        # geometry), the rest should still render, and the overall exit
        # code should reflect the one failure.
        src = tmp_path / "in.scad"
        src.write_text("if ($t > 0) cube(1);\n")
        out = tmp_path / "out.stl"
        code = render_and_export_animation(str(src), str(out), 4)
        assert code == 1
        assert not (tmp_path / "out00000.stl").exists()
        assert (tmp_path / "out00001.stl").exists()
        assert (tmp_path / "out00002.stl").exists()
        assert (tmp_path / "out00003.stl").exists()


class TestQuiet:
    def test_quiet_suppresses_success_message(self, tmp_path, capsys):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), quiet=True)
        assert code == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert err == ""

    def test_quiet_suppresses_warnings_but_not_errors(self, tmp_path, capsys):
        src = tmp_path / "in.scad"
        src.write_text("x = 1;\nx = 2;\ncube(x);\n")  # triggers a WARNING
        code = render_and_export(str(src), str(tmp_path / "out.stl"), quiet=True)
        assert code == 0
        out, err = capsys.readouterr()
        assert "WARNING" not in err

    def test_not_quiet_prints_success_message(self, tmp_path, capsys):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"))
        assert code == 0
        out, _err = capsys.readouterr()
        assert "Exported to" in out


class TestHardWarnings:
    def test_stops_on_first_warning(self, tmp_path, capsys):
        src = tmp_path / "in.scad"
        src.write_text("x = 1;\nx = 2;\ncube(x);\n")  # triggers a WARNING
        code = render_and_export(str(src), str(tmp_path / "out.stl"), hard_warnings=True)
        assert code == 1
        assert not (tmp_path / "out.stl").exists()
        _out, err = capsys.readouterr()
        assert "WARNING" in err

    def test_no_warning_still_succeeds(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), hard_warnings=True)
        assert code == 0


class TestExportFormat:
    def test_asciistl_produces_ascii_output(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        out = tmp_path / "out.stl"
        code = render_and_export(str(src), str(out), export_format="asciistl")
        assert code == 0
        text = out.read_text()
        assert text.startswith("solid OpenSCAD_Model\n")
        assert text.rstrip().endswith("endsolid OpenSCAD_Model")

    def test_binstl_is_default_binary(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        out = tmp_path / "out.stl"
        code = render_and_export(str(src), str(out), export_format="binstl")
        assert code == 0
        assert out.read_bytes()[:5] != b"solid"

    def test_invalid_value_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), export_format="nope")
        assert code == 1

    def test_ignored_for_non_stl(self, tmp_path, capsys):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.obj"), export_format="asciistl")
        assert code == 0  # warns, doesn't fail
        _out, err = capsys.readouterr()
        assert "only applies to .stl" in err


class TestBackend:
    def test_manifold_accepted(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), backend="Manifold")
        assert code == 0

    def test_cgal_rejected(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), backend="CGAL")
        assert code == 1


class TestSummary:
    def test_all_prints_to_stdout(self, tmp_path, capsys):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), summary="all")
        assert code == 0
        out, _err = capsys.readouterr()
        assert "geometry:" in out
        assert "bounding-box:" in out
        assert "time:" in out

    def test_summary_file_writes_json(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        summary_path = tmp_path / "summary.json"
        code = render_and_export(str(src), str(tmp_path / "out.stl"), summary="geometry,bounding-box",
                                  summary_file=str(summary_path))
        assert code == 0
        data = json.loads(summary_path.read_text())
        assert data["geometry"] == {"bodies": 1, "facets": 12, "vertices": 8}
        assert data["bounding-box"]["max"] == [10.0, 5.0, 3.0]
        assert "time" not in data  # only the requested keys

    def test_unknown_key_fails(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube(1);\n")
        code = render_and_export(str(src), str(tmp_path / "out.stl"), summary="nonsense")
        assert code == 1

    def test_area(self, tmp_path):
        src = tmp_path / "in.scad"
        src.write_text("cube([10, 5, 3]);\n")
        summary_path = tmp_path / "summary.json"
        code = render_and_export(str(src), str(tmp_path / "out.stl"), summary="area",
                                  summary_file=str(summary_path))
        assert code == 0
        data = json.loads(summary_path.read_text())
        assert data["area"] == pytest.approx(2 * (10 * 5 + 10 * 3 + 5 * 3))
