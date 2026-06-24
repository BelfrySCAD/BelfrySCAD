"""
Tests for `use <file>` resolution (`_resolve_use_scopes` in main_window.py).

Per the OpenSCAD docs, `use <file>`:
- brings the used file's own modules/functions into scope
- does NOT bring in the used file's top-level geometry
- does NOT share variables between the using and used files in either direction
- lets the used file's modules/functions resolve its own globals
- does not leak declarations the used file itself pulled in via a nested `use`
"""
import pytest
from openscad_lalr_parser import getASTfromFile

from belfryscad.engine.evaluator import Evaluator
from belfryscad.window.main_window import _resolve_use_scopes


def run_file(path, log=None):
    """Parse `path`, resolve `use` statements, and evaluate. Returns (bodies, echo_lines, logs)."""
    logs = [] if log is None else log
    echo_lines = []
    nodes = getASTfromFile(str(path), include_comments=False)
    nodes, _own, root_scope = _resolve_use_scopes(nodes, str(path), logs.append)
    ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg))
    bodies, _ = ev.evaluate(nodes, root_scope)
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
        assert bodies[0].body.bounding_box() == (0.0, 0.0, 0.0, 10.0, 10.0, 10.0)
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
        assert logs == []
        # `x` is unresolved within lib.scad's scope -> warns, then undef.
        assert echoes[0].startswith("WARNING: Ignoring unknown variable 'x'")
        assert echoes[1] == "ECHO: undef"

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
        assert logs == []
        # combo() can call get_inner() (lib2's own nested `use`) and reach inner_val.
        assert echoes[0] == "ECHO: 107"
        # inner.scad's declarations don't leak into main2.scad -> both
        # references are unresolved (warn), then is_undef(undef) == true.
        assert echoes[1].startswith("WARNING: Ignoring unknown variable 'inner_val'")
        assert echoes[2] == "ECHO: true"
        assert echoes[3].startswith("WARNING: Ignoring unknown variable 'get_inner'")
        assert echoes[4] == "ECHO: true"

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
        assert bodies[0].body.bounding_box() == (0.0, 0.0, 0.0, 7.0, 7.0, 7.0)
