"""Tests for belfryscad.scad_deps -- the -d/--deps and -m/--make CLI
backing (static source scanning, Makefile dependency file format, and the
missing-file make_cmd trigger). Semantics verified directly against real
OpenSCAD's src/handle_dep.cc rather than guessed from --help text alone;
see the module's own doc comment."""

import os

from belfryscad.scad_deps import scan_dependencies, write_deps_file, run_make_for_missing


class TestScanDependencies:
    def test_main_file_alone(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("cube(1);\n")
        assert scan_dependencies(str(main)) == [str(main)]

    def test_use_include_transitive(self, tmp_path):
        (tmp_path / "lib2.scad").write_text("module deep() { cube(1); }\n")
        (tmp_path / "lib.scad").write_text("use <lib2.scad>\nmodule wrapper() { deep(); }\n")
        (tmp_path / "config.scad").write_text("CFG = 5;\n")
        main = tmp_path / "main.scad"
        main.write_text("use <lib.scad>\ninclude <config.scad>\nwrapper();\n")
        deps = scan_dependencies(str(main))
        assert deps == [
            str(main), str(tmp_path / "lib.scad"),
            str(tmp_path / "lib2.scad"), str(tmp_path / "config.scad"),
        ]

    def test_missing_use_target_not_tracked(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("use <nonexistent.scad>\ncube(1);\n")
        assert scan_dependencies(str(main)) == [str(main)]

    def test_import_target_tracked_even_when_missing(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text('import("part.stl");\n')
        assert scan_dependencies(str(main)) == [str(main), str(tmp_path / "part.stl")]

    def test_surface_and_extrude_file_tracked(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text(
            'surface("height.png");\n'
            'linear_extrude(file = "shape.dxf", height=5);\n'
        )
        deps = scan_dependencies(str(main))
        assert str(tmp_path / "height.png") in deps
        assert str(tmp_path / "shape.dxf") in deps

    def test_comment_does_not_count_as_reference(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("// use <fake.scad>\n/* import(\"also_fake.stl\"); */\ncube(1);\n")
        assert scan_dependencies(str(main)) == [str(main)]

    def test_duplicate_use_only_counted_once(self, tmp_path):
        (tmp_path / "lib.scad").write_text("module m() {}\n")
        main = tmp_path / "main.scad"
        main.write_text("use <lib.scad>\nuse <lib.scad>\nm();\n")
        assert scan_dependencies(str(main)) == [str(main), str(tmp_path / "lib.scad")]


class TestWriteDepsFile:
    def test_format_matches_real_openscad(self, tmp_path):
        deps_path = tmp_path / "out.deps"
        write_deps_file(str(deps_path), ["out.stl"], ["a.scad", "b.scad"])
        assert deps_path.read_text() == "out.stl: \\\n\ta.scad \\\n\tb.scad\n"

    def test_multiple_output_files(self, tmp_path):
        deps_path = tmp_path / "out.deps"
        write_deps_file(str(deps_path), ["out.stl", "out.png"], ["a.scad"])
        assert deps_path.read_text() == "out.stl out.png: \\\n\ta.scad\n"

    def test_unwritable_path_fails(self, tmp_path):
        assert write_deps_file(str(tmp_path / "nodir" / "out.deps"), ["out.stl"], ["a.scad"]) is False


class TestRunMakeForMissing:
    def _make_cmd(self, tmp_path, content="cube(1);\n"):
        script = tmp_path / "gen.sh"
        marker = tmp_path / "gen_content.txt"
        marker.write_text(content)
        script.write_text(f'#!/bin/sh\ncp {marker} "$1"\n')
        os.chmod(script, 0o755)
        return str(script)

    def test_generates_missing_main_file(self, tmp_path):
        main = tmp_path / "generated.scad"
        assert not main.exists()
        run_make_for_missing(str(main), self._make_cmd(tmp_path))
        assert main.exists()
        assert main.read_text() == "cube(1);\n"

    def test_does_not_touch_existing_main_file(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("sphere(1);\n")
        run_make_for_missing(str(main), self._make_cmd(tmp_path))
        assert main.read_text() == "sphere(1);\n"

    def test_generates_missing_import_target(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text('import("part.stl");\n')
        part = tmp_path / "part.stl"
        assert not part.exists()
        run_make_for_missing(str(main), self._make_cmd(tmp_path, content="solid\nendsolid\n"))
        assert part.exists()
        assert part.read_text() == "solid\nendsolid\n"

    def test_does_not_touch_existing_import_target(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text('import("part.stl");\n')
        part = tmp_path / "part.stl"
        part.write_text("original\n")
        run_make_for_missing(str(main), self._make_cmd(tmp_path))
        assert part.read_text() == "original\n"
