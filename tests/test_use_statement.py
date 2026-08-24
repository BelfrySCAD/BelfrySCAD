"""
Tests for `use <file>` resolution (handled internally by the C++ evaluator's
own parser -- openscad_cpp_evaluator.Evaluator.evaluate() resolves `use`/
`include` as part of a single evaluate() call now; there's no separate
resolve step to call from Python).

Per the OpenSCAD docs, `use <file>`:
- brings the used file's own modules/functions into scope
- does NOT bring in the used file's top-level geometry
- does NOT share variables between the using and used files in either direction
- lets the used file's modules/functions resolve its own globals
- does not leak declarations the used file itself pulled in via a nested `use`
"""
from openscad_cpp_evaluator import Evaluator


def _bbox(body):
    """(xmin,ymin,zmin,xmax,ymax,zmax) of a body's mesh -- the array-shim
    backend has no .bounding_box() (unlike a real manifold3d.Manifold), so
    compute it from the raw vertex array instead."""
    verts = body.to_mesh().vert_properties[:, :3]
    mn, mx = verts.min(axis=0), verts.max(axis=0)
    return (float(mn[0]), float(mn[1]), float(mn[2]), float(mx[0]), float(mx[1]), float(mx[2]))


def run_file(path, log=None):
    """Evaluate `path` (use/include resolved internally). Returns (bodies, echo_lines, logs)."""
    logs = [] if log is None else log
    echo_lines = []
    ev = Evaluator(echo_fn=lambda msg: (logs if msg.startswith("WARNING:") else echo_lines).append(msg))
    bodies, _ = ev.evaluate(str(path))
    return bodies, echo_lines, logs


class TestUseStatement:
    def test_module_and_function_visible_with_own_globals(self, tmp_path):
        (tmp_path / "lib.scad").write_text(
            "width = 10;\n"
            "module box() { cube([width, width, width]); }\n"
            "function double_width() = width * 2;\n"
            "cube([999, 999, 999]);\n"  # top-level geometry, must be ignored
        )
        (tmp_path / "main.scad").write_text(
            "use <lib.scad>\n"
            "width = 5;\n"
            "box();\n"
            "echo(double_width());\n"
            "echo(width);\n"
        )
        bodies, echoes, logs = run_file(tmp_path / "main.scad")
        assert logs == []
        # Only box()'s cube is produced; lib.scad's top-level cube is ignored.
        assert len(bodies) == 1
        assert _bbox(bodies[0].body) == (0.0, 0.0, 0.0, 10.0, 10.0, 10.0)
        # double_width() resolves lib.scad's own `width`, not main.scad's.
        assert echoes[0] == "ECHO: 20"
        # main.scad's own `width` is untouched by lib.scad's.
        assert echoes[1] == "ECHO: 5"

    def test_used_file_cannot_see_using_file_variables(self, tmp_path):
        (tmp_path / "lib.scad").write_text(
            "function get_x() = x;\n"  # `x` is only defined in main.scad
        )
        (tmp_path / "main.scad").write_text(
            "use <lib.scad>\n"
            "x = 42;\n"
            "echo(get_x());\n"
        )
        _bodies, echoes, logs = run_file(tmp_path / "main.scad")
        # `x` is unresolved within lib.scad's scope -> warns, then undef.
        assert any("x" in w for w in logs)
        assert echoes[0] == "ECHO: undef"

    def test_nested_use_does_not_leak(self, tmp_path):
        (tmp_path / "inner.scad").write_text(
            "inner_val = 100;\n"
            "function get_inner() = inner_val;\n"
        )
        (tmp_path / "lib2.scad").write_text(
            "use <inner.scad>\n"
            "lib2_val = 7;\n"
            "function combo() = get_inner() + lib2_val;\n"
        )
        (tmp_path / "main2.scad").write_text(
            "use <lib2.scad>\n"
            "echo(combo());\n"
            "echo(is_undef(inner_val));\n"
            "echo(is_undef(get_inner));\n"
        )
        _bodies, echoes, logs = run_file(tmp_path / "main2.scad")
        # combo() can call get_inner() (lib2's own nested `use`) and reach inner_val.
        assert echoes[0] == "ECHO: 107"
        # inner.scad's declarations don't leak into main2.scad -> both
        # references are unresolved, so is_undef(...) == true.
        #
        # No warning is asserted: is_undef() does not warn about the name it
        # probes, matching the reference, which is silent for this exact
        # script. (A plain unresolved read still warns -- see
        # test_used_file_cannot_see_using_file_variables above.)
        assert echoes[1] == "ECHO: true"
        assert echoes[2] == "ECHO: true"
        assert not any("inner_val" in w or "get_inner" in w for w in logs)

    def test_use_missing_file_is_silently_ignored(self, tmp_path):
        (tmp_path / "main.scad").write_text(
            "use <does_not_exist.scad>\n"
            "echo(1);\n"
        )
        _bodies, echoes, logs = run_file(tmp_path / "main.scad")
        assert logs == []
        assert echoes == ["ECHO: 1"]

    def test_use_path_resolved_relative_to_originating_file(self, tmp_path):
        # `include <sub/inc.scad>` flattens inc.scad's `use <lib.scad>` into
        # main.scad's top-level nodes. `lib.scad` only exists next to inc.scad
        # (in `sub/`), not next to main.scad — so the `use` path must resolve
        # relative to inc.scad's directory (via the node's source position),
        # not main.scad's.
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "lib.scad").write_text(
            "module box() { cube([7,7,7]); }\n"
        )
        (sub / "inc.scad").write_text(
            "use <lib.scad>\n"
        )
        (tmp_path / "main.scad").write_text(
            "include <sub/inc.scad>\n"
            "box();\n"
        )
        bodies, _echoes, logs = run_file(tmp_path / "main.scad")
        assert logs == []
        assert len(bodies) == 1
        assert _bbox(bodies[0].body) == (0.0, 0.0, 0.0, 7.0, 7.0, 7.0)
