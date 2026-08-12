"""Tests for belfryscad.scad_deps -- the -d/--deps and -m/--make CLI
backing (static source scanning, Makefile dependency file format, and the
missing-file make_cmd trigger). Semantics verified directly against real
OpenSCAD's src/handle_dep.cc rather than guessed from --help text alone;
see the module's own doc comment."""

import os
import sys

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
        """A generator command that works on every platform.

        This used to be a `#!/bin/sh` script with `cp`, which simply cannot
        run on Windows -- the tests using it failed there with the target
        file never appearing. Driving the interpreter running the tests
        needs no shell at all.
        """
        script = tmp_path / "gen.py"
        marker = tmp_path / "gen_content.txt"
        marker.write_text(content)
        script.write_text(
            "import shutil, sys\n"
            f"shutil.copyfile({str(marker)!r}, sys.argv[1])\n")
        return f'"{sys.executable}" "{script}"'

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


class TestLibraryPathSearch:
    """Where a use/include is looked for, and in what order.

    The directory of the file that wrote the statement comes first, then
    the library path. The library half was missing outright: `-d` on a
    script whose only dependency was `include <BOSL2/std.scad>` listed the
    script alone, where the reference listed 33 files.
    """

    def test_a_library_include_is_found_at_all(self, tmp_path, monkeypatch):
        lib = tmp_path / "libs"
        (lib / "Pkg").mkdir(parents=True)
        (lib / "Pkg" / "std.scad").write_text("cube(1);\n")
        monkeypatch.setenv("OPENSCADPATH", str(lib))

        main = tmp_path / "main.scad"
        main.write_text("include <Pkg/std.scad>\n")
        deps = scan_dependencies(str(main))
        assert str(lib / "Pkg" / "std.scad") in deps

    def test_a_sibling_wins_over_a_library_of_the_same_name(self, tmp_path, monkeypatch):
        lib = tmp_path / "libs"
        lib.mkdir()
        (lib / "helper.scad").write_text("// the library one\n")
        monkeypatch.setenv("OPENSCADPATH", str(lib))

        project = tmp_path / "project"
        project.mkdir()
        (project / "helper.scad").write_text("// the one next to the model\n")
        main = project / "main.scad"
        main.write_text("include <helper.scad>\n")

        deps = scan_dependencies(str(main))
        assert str(project / "helper.scad") in deps
        assert str(lib / "helper.scad") not in deps

    def test_a_library_file_gets_its_own_sibling_not_the_models(self, tmp_path, monkeypatch):
        # The rule is "relative to the file that included it", not
        # "relative to the top-level model" -- a library including its own
        # helper must not pick up a same-named file beside the model.
        lib = tmp_path / "libs"
        (lib / "Pkg").mkdir(parents=True)
        (lib / "Pkg" / "helper.scad").write_text("// the library's own\n")
        (lib / "Pkg" / "lib.scad").write_text("include <helper.scad>\n")
        monkeypatch.setenv("OPENSCADPATH", str(lib))

        project = tmp_path / "project"
        project.mkdir()
        (project / "helper.scad").write_text("// a decoy beside the model\n")
        main = project / "main.scad"
        main.write_text("include <Pkg/lib.scad>\n")

        deps = scan_dependencies(str(main))
        assert str(lib / "Pkg" / "helper.scad") in deps
        assert str(project / "helper.scad") not in deps

    def test_a_use_target_searches_the_library_path_too(self, tmp_path, monkeypatch):
        lib = tmp_path / "libs"
        lib.mkdir()
        (lib / "thing.scad").write_text("module thing(){}\n")
        monkeypatch.setenv("OPENSCADPATH", str(lib))
        main = tmp_path / "main.scad"
        main.write_text("use <thing.scad>\n")
        assert str(lib / "thing.scad") in scan_dependencies(str(main))

    def test_a_target_found_nowhere_is_still_not_a_dependency(self, tmp_path, monkeypatch):
        # use/include register only once the target is found -- that is the
        # asymmetry with import(), and the library fallback must not change it.
        monkeypatch.setenv("OPENSCADPATH", str(tmp_path / "empty"))
        main = tmp_path / "main.scad"
        main.write_text("include <nowhere.scad>\n")
        assert scan_dependencies(str(main)) == [str(main)]
