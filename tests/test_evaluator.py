"""
Tests for the NeuSCAD evaluator.

Each test calls run(src) which parses, scopes, and evaluates OpenSCAD source,
returning (bodies, echo_lines). Geometry tests inspect bounding boxes;
expression tests capture echo output.
"""
import pytest
from openscad_parser.ast import getASTfromString, build_scopes

from neuscad.engine.evaluator import Evaluator, EvalError


def run(src: str):
    """Parse, scope, and evaluate src. Returns (bodies, echo_lines)."""
    echo_lines = []
    nodes = getASTfromString(src)
    root_scope = build_scopes(nodes)
    ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg))
    bodies, _ = ev.evaluate(nodes, root_scope)
    return bodies, echo_lines


def bbox(bodies):
    """Return (xmin,ymin,zmin,xmax,ymax,zmax) of first body's manifold."""
    assert bodies, "no geometry produced"
    return bodies[0].body.bounding_box()


def approx(v, rel=1e-4):
    return pytest.approx(v, rel=rel)


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

class TestExpressions:
    def test_not_equal(self):
        _, lines = run("echo(1 != 2);")
        assert lines == ["ECHO: true"]

    def test_greater_than_or_equal(self):
        _, lines = run("echo(3 >= 3);")
        assert lines == ["ECHO: true"]

    def test_vector_add(self):
        _, lines = run("echo([1,2,3] + [4,5,6]);")
        assert lines == ["ECHO: [5, 7, 9]"]

    def test_vector_subtract(self):
        _, lines = run("echo([5,7,9] - [4,5,6]);")
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_vector_scale_right(self):
        _, lines = run("echo([1,2,3] * 2);")
        assert lines == ["ECHO: [2, 4, 6]"]

    def test_vector_scale_left(self):
        _, lines = run("echo(3 * [1,2,3]);")
        assert lines == ["ECHO: [3, 6, 9]"]

    def test_unary_minus_vector(self):
        _, lines = run("echo(-[1,2,3]);")
        assert lines == ["ECHO: [-1, -2, -3]"]

    def test_member_x(self):
        _, lines = run("v = [10,20,30]; echo(v.x);")
        assert lines == ["ECHO: 10"]

    def test_member_y(self):
        _, lines = run("v = [10,20,30]; echo(v.y);")
        assert lines == ["ECHO: 20"]

    def test_member_z(self):
        _, lines = run("v = [10,20,30]; echo(v.z);")
        assert lines == ["ECHO: 30"]

    def test_arithmetic(self):
        _, lines = run("echo(2 + 3 * 4);")
        assert lines == ["ECHO: 14"]

    def test_division(self):
        _, lines = run("echo(10 / 4);")
        assert lines == ["ECHO: 2.5"]

    def test_modulo(self):
        _, lines = run("echo(10 % 3);")
        assert lines == ["ECHO: 1"]

    def test_exponent(self):
        _, lines = run("echo(2 ^ 10);")
        assert lines == ["ECHO: 1024"]

    def test_unary_minus(self):
        _, lines = run("echo(-5);")
        assert lines == ["ECHO: -5"]

    def test_comparison(self):
        _, lines = run("echo(3 > 2);")
        assert lines == ["ECHO: true"]

    def test_logical_and(self):
        _, lines = run("echo(true && false);")
        assert lines == ["ECHO: false"]

    def test_logical_or(self):
        _, lines = run("echo(false || true);")
        assert lines == ["ECHO: true"]

    def test_logical_not(self):
        _, lines = run("echo(!true);")
        assert lines == ["ECHO: false"]

    def test_ternary_true(self):
        _, lines = run("echo(1 > 0 ? 42 : 99);")
        assert lines == ["ECHO: 42"]

    def test_ternary_false(self):
        _, lines = run("echo(1 < 0 ? 42 : 99);")
        assert lines == ["ECHO: 99"]

    def test_vector_literal(self):
        _, lines = run("echo([1, 2, 3]);")
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_vector_index(self):
        _, lines = run("v = [10, 20, 30]; echo(v[1]);")
        assert lines == ["ECHO: 20"]

    def test_range(self):
        _, lines = run("echo([1:3]);")
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_range_step(self):
        _, lines = run("echo([0:2:6]);")
        assert lines == ["ECHO: [0, 2, 4, 6]"]


# ---------------------------------------------------------------------------
# Variables and scoping
# ---------------------------------------------------------------------------

class TestVariables:
    def test_assignment(self):
        _, lines = run("x = 7; echo(x);")
        assert lines == ["ECHO: 7"]

    def test_undef(self):
        _, lines = run("echo(undef);")
        assert lines == ["ECHO: undef"]

    def test_boolean_literals(self):
        _, lines = run("echo(true, false);")
        assert lines == ["ECHO: true, false"]

    def test_string_literal(self):
        _, lines = run('echo("hello");')
        assert lines == ['ECHO: "hello"']

    def test_computed_assignment(self):
        _, lines = run("a = 3; b = a * 2; echo(b);")
        assert lines == ["ECHO: 6"]

    def test_special_var_assignment(self):
        # $fn at top level goes into dynamic context
        bodies, _ = run("$fn = 8; sphere(r=1);")
        assert bodies

    def test_special_var_lookup(self):
        _, lines = run("$fn = 64; echo($fn);")
        assert lines == ["ECHO: 64"]


# ---------------------------------------------------------------------------
# Built-in functions
# ---------------------------------------------------------------------------

class TestBuiltinFunctions:
    def test_abs(self):
        _, lines = run("echo(abs(-5));")
        assert lines == ["ECHO: 5"]

    def test_sqrt(self):
        _, lines = run("echo(sqrt(4));")
        assert lines == ["ECHO: 2"]

    def test_floor(self):
        _, lines = run("echo(floor(3.9));")
        assert lines == ["ECHO: 3"]

    def test_ceil(self):
        _, lines = run("echo(ceil(3.1));")
        assert lines == ["ECHO: 4"]

    def test_round(self):
        _, lines = run("echo(round(3.5));")
        assert lines == ["ECHO: 4"]

    def test_min(self):
        _, lines = run("echo(min(5, 3, 8));")
        assert lines == ["ECHO: 3"]

    def test_max(self):
        _, lines = run("echo(max(5, 3, 8));")
        assert lines == ["ECHO: 8"]

    def test_sin(self):
        _, lines = run("echo(sin(90));")
        assert lines == ["ECHO: 1"]

    def test_cos(self):
        _, lines = run("echo(cos(0));")
        assert lines == ["ECHO: 1"]

    def test_len(self):
        _, lines = run("echo(len([1,2,3]));")
        assert lines == ["ECHO: 3"]

    def test_concat(self):
        _, lines = run("echo(concat([1,2],[3,4]));")
        assert lines == ["ECHO: [1, 2, 3, 4]"]

    def test_str_numbers(self):
        _, lines = run('echo(str(1, 2, 3));')
        assert lines == ["ECHO: \"123\""]

    def test_str_string_no_quotes(self):
        _, lines = run('echo(str("hello", 42));')
        assert lines == ['ECHO: "hello42"']

    def test_is_num(self):
        _, lines = run("echo(is_num(3));")
        assert lines == ["ECHO: true"]

    def test_is_list(self):
        _, lines = run("echo(is_list([1,2]));")
        assert lines == ["ECHO: true"]

    def test_is_undef(self):
        _, lines = run("echo(is_undef(undef));")
        assert lines == ["ECHO: true"]

    def test_is_bool(self):
        _, lines = run("echo(is_bool(true));")
        assert lines == ["ECHO: true"]

    def test_is_string(self):
        _, lines = run('echo(is_string("hi"));')
        assert lines == ["ECHO: true"]

    def test_tan(self):
        _, lines = run("echo(tan(45));")
        assert float(lines[0].split(": ")[1]) == approx(1.0)

    def test_asin(self):
        _, lines = run("echo(asin(1));")
        assert float(lines[0].split(": ")[1]) == approx(90.0)

    def test_acos(self):
        _, lines = run("echo(acos(1));")
        assert float(lines[0].split(": ")[1]) == approx(0.0)

    def test_atan(self):
        _, lines = run("echo(atan(1));")
        assert float(lines[0].split(": ")[1]) == approx(45.0)

    def test_atan2(self):
        _, lines = run("echo(atan2(1, 1));")
        assert float(lines[0].split(": ")[1]) == approx(45.0)

    def test_ln(self):
        _, lines = run("echo(ln(1));")
        assert lines == ["ECHO: 0"]

    def test_log(self):
        _, lines = run("echo(log(100));")
        assert lines == ["ECHO: 2"]

    def test_exp(self):
        _, lines = run("echo(exp(0));")
        assert lines == ["ECHO: 1"]

    def test_pow(self):
        _, lines = run("echo(pow(3, 3));")
        assert lines == ["ECHO: 27"]

    def test_norm(self):
        _, lines = run("echo(norm([3, 4]));")
        assert float(lines[0].split(": ")[1]) == approx(5.0)

    def test_cross(self):
        _, lines = run("echo(cross([1,0,0],[0,1,0]));")
        assert lines == ["ECHO: [0, 0, 1]"]

    def test_chr(self):
        _, lines = run("echo(chr(65));")
        assert lines == ['ECHO: "A"']

    def test_ord(self):
        _, lines = run('echo(ord("A"));')
        assert lines == ["ECHO: 65"]


# ---------------------------------------------------------------------------
# User-defined functions
# ---------------------------------------------------------------------------

class TestUserFunctions:
    def test_simple_function(self):
        _, lines = run("function double(x) = x * 2; echo(double(5));")
        assert lines == ["ECHO: 10"]

    def test_recursive_function(self):
        src = """
        function fact(n) = n <= 1 ? 1 : n * fact(n - 1);
        echo(fact(5));
        """
        _, lines = run(src)
        assert lines == ["ECHO: 120"]

    def test_function_default_args(self):
        src = "function add(a, b=10) = a + b; echo(add(5));"
        _, lines = run(src)
        assert lines == ["ECHO: 15"]

    def test_undefined_function_error(self):
        with pytest.raises(EvalError, match="undefined function 'nope'"):
            run("echo(nope(1));")

    def test_undefined_function_traceback(self):
        src = """
        function outer() = inner();
        echo(outer());
        """
        with pytest.raises(EvalError) as exc_info:
            run(src)
        msg = str(exc_info.value)
        assert "undefined function 'inner'" in msg
        assert "Traceback" in msg
        assert "in outer()" in msg


# ---------------------------------------------------------------------------
# Control flow
# ---------------------------------------------------------------------------

class TestControlFlow:
    def test_if_true(self):
        src = "if (true) { echo(1); }"
        _, lines = run(src)
        assert lines == ["ECHO: 1"]

    def test_if_false(self):
        src = "if (false) { echo(1); }"
        _, lines = run(src)
        assert lines == []

    def test_if_else(self):
        src = "if (false) { echo(1); } else { echo(2); }"
        _, lines = run(src)
        assert lines == ["ECHO: 2"]

    def test_for_loop(self):
        src = "for (i = [1:3]) { echo(i); }"
        _, lines = run(src)
        assert lines == ["ECHO: 1", "ECHO: 2", "ECHO: 3"]

    def test_for_step(self):
        src = "for (i = [0:2:4]) { echo(i); }"
        _, lines = run(src)
        assert lines == ["ECHO: 0", "ECHO: 2", "ECHO: 4"]

    def test_for_vector(self):
        src = "for (x = [10, 20, 30]) { echo(x); }"
        _, lines = run(src)
        assert lines == ["ECHO: 10", "ECHO: 20", "ECHO: 30"]


# ---------------------------------------------------------------------------
# List comprehensions
# ---------------------------------------------------------------------------

class TestListComprehensions:
    def test_for_comp(self):
        _, lines = run("echo([for (i=[1:3]) i*2]);")
        assert lines == ["ECHO: [2, 4, 6]"]

    def test_if_comp(self):
        _, lines = run("echo([for (i=[1:5]) if (i % 2 == 1) i]);")
        assert lines == ["ECHO: [1, 3, 5]"]

    def test_each_flat(self):
        _, lines = run("a = [1,2,3]; echo([each a]);")
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_each_nested(self):
        _, lines = run("a = [[1,2,3],[4,5,6]]; b = [each a]; echo(b);")
        assert lines == ["ECHO: [1, 2, 3, 4, 5, 6]"]

    def test_listcompif_direct(self):
        # ListCompIf as a direct element (not nested in for)
        _, lines = run("x = [if (true) 1, if (false) 2, if (true) 3]; echo(x);")
        assert lines == ["ECHO: [1, 3]"]

    def test_listcompifelse_direct(self):
        _, lines = run("x = [if (true) 1 else 9, if (false) 2 else 8]; echo(x);")
        assert lines == ["ECHO: [1, 8]"]

    def test_for_each_flatten(self):
        src = """
        function flatten(list) = [for (x=list) each x];
        grid = [[1,2,3],[4,5,6]];
        echo(flatten(grid));
        """
        _, lines = run(src)
        assert lines == ["ECHO: [1, 2, 3, 4, 5, 6]"]


# ---------------------------------------------------------------------------
# Primitives and geometry
# ---------------------------------------------------------------------------

class TestPrimitives:
    def test_cube_default(self):
        bodies, _ = run("cube(1);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(1)  # x size

    def test_cube_sized(self):
        bodies, _ = run("cube([2, 3, 4]);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)
        assert bb[4] - bb[1] == approx(3)
        assert bb[5] - bb[2] == approx(4)

    def test_cube_centered(self):
        bodies, _ = run("cube([4, 4, 4], center=true);")
        bb = bbox(bodies)
        assert bb[0] == approx(-2)
        assert bb[3] == approx(2)

    def test_sphere(self):
        bodies, _ = run("sphere(r=5, $fn=32);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(10, rel=0.02)

    def test_cylinder(self):
        bodies, _ = run("cylinder(h=10, r=3, $fn=32);")
        bb = bbox(bodies)
        assert bb[5] - bb[2] == approx(10, rel=0.01)
        assert bb[3] - bb[0] == approx(6, rel=0.02)

    def test_no_geometry_for_assignment(self):
        bodies, _ = run("x = 5;")
        assert bodies == []


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

class TestTransforms:
    def test_translate(self):
        bodies, _ = run("translate([10, 0, 0]) cube(1);")
        bb = bbox(bodies)
        assert bb[0] == approx(10)
        assert bb[3] == approx(11)

    def test_scale(self):
        bodies, _ = run("scale([2, 1, 1]) cube(1);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

    def test_rotate(self):
        bodies, _ = run("rotate([0, 0, 90]) translate([5, 0, 0]) cube(1);")
        bb = bbox(bodies)
        # after 90° z-rotation, x extent of translated cube maps to y axis
        assert abs(bb[1]) == approx(5, rel=0.01)

    def test_rotate_axis_angle(self):
        # rotate(90, v=[0,0,1]) is equivalent to rotate([0,0,90])
        bodies_euler, _ = run("rotate([0,0,90]) translate([5,0,0]) cube(1);")
        bodies_axis,  _ = run("rotate(90, v=[0,0,1]) translate([5,0,0]) cube(1);")
        bb_e = bodies_euler[0].body.bounding_box()
        bb_a = bodies_axis[0].body.bounding_box()
        assert bb_a[0] == approx(bb_e[0], rel=0.01)
        assert bb_a[3] == approx(bb_e[3], rel=0.01)


# ---------------------------------------------------------------------------
# color()
# ---------------------------------------------------------------------------

class TestColor:
    def _color(self, bodies):
        assert bodies
        return bodies[0].color

    def test_color_rgb_list(self):
        bodies, _ = run("color([1,0,0]) cube(1);")
        c = self._color(bodies)
        assert c[0] == approx(1.0)
        assert c[1] == approx(0.0)
        assert c[2] == approx(0.0)

    def test_color_rgba_list(self):
        bodies, _ = run("color([0,1,0,0.5]) cube(1);")
        c = self._color(bodies)
        assert c[1] == approx(1.0)
        assert c[3] == approx(0.5)

    def test_color_css_name(self):
        bodies, _ = run('color("red") cube(1);')
        c = self._color(bodies)
        assert c[0] == approx(1.0)
        assert c[1] == approx(0.0)

    def test_color_hex6(self):
        bodies, _ = run('color("#ff0000") cube(1);')
        c = self._color(bodies)
        assert c[0] == approx(1.0)
        assert c[1] == approx(0.0)

    def test_color_hex3(self):
        bodies, _ = run('color("#f00") cube(1);')
        c = self._color(bodies)
        assert c[0] == approx(1.0)
        assert c[1] == approx(0.0)

    def test_color_alpha_arg(self):
        bodies, _ = run('color("blue", alpha=0.25) cube(1);')
        c = self._color(bodies)
        assert c[2] == approx(1.0)
        assert c[3] == approx(0.25)

    def test_color_geometry_preserved(self):
        bodies, _ = run("color([0,0,1]) cube([2,3,4]);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)


# ---------------------------------------------------------------------------
# hull()
# ---------------------------------------------------------------------------

class TestHull:
    def test_hull_two_cubes(self):
        src = "hull() { cube(1); translate([5,0,0]) cube(1); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(6)

    def test_hull_contains_children(self):
        src = "hull() { sphere(r=1, $fn=16); translate([4,0,0]) sphere(r=1, $fn=16); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(6, rel=0.05)


# ---------------------------------------------------------------------------
# Modifiers (#, %, !, *)
# ---------------------------------------------------------------------------

class TestModifiers:
    def test_highlight_produces_geometry(self):
        # # (highlight) passes through the child geometry
        bodies, _ = run("#cube(2);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

    def test_showonly_produces_geometry(self):
        # ! (show-only) passes through the child geometry
        bodies, _ = run("!cube(3);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)

    def test_background_suppressed(self):
        # % (background) produces no geometry
        bodies, _ = run("%cube(1);")
        assert bodies == []

    def test_disable_suppressed(self):
        # * (disable) produces no geometry
        bodies, _ = run("*cube(1);")
        assert bodies == []

    def test_highlight_with_other_geometry(self):
        # only the non-highlighted cube should survive if % suppresses the other
        src = "cube(1); %cube([10,10,10]);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[3] - bb[0] == approx(1)


# ---------------------------------------------------------------------------
# CSG operations
# ---------------------------------------------------------------------------

class TestCSG:
    def test_union(self):
        src = "union() { cube([2,1,1]); translate([1,0,0]) cube([2,1,1]); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)

    def test_difference(self):
        src = "difference() { cube([4,4,4]); cube([2,2,2]); }"
        bodies, _ = run(src)
        assert bodies  # geometry produced; exact shape is hollow

    def test_intersection(self):
        src = "intersection() { cube([3,3,3]); translate([1,1,1]) cube([3,3,3]); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)


# ---------------------------------------------------------------------------
# More transforms
# ---------------------------------------------------------------------------

class TestMoreTransforms:
    def test_mirror_x(self):
        bodies, _ = run("mirror([1,0,0]) translate([3,0,0]) cube(1);")
        bb = bbox(bodies)
        # cube was at x=[3,4]; after mirroring on YZ plane it lands at x=[-4,-3]
        assert bb[3] <= 0.01

    def test_resize(self):
        bodies, _ = run("resize([6,6,6]) cube(2);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(6, rel=0.01)

    def test_multmatrix_identity(self):
        # identity matrix should leave the cube unchanged
        src = """
        multmatrix([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]])
            cube(2);
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

    def test_multmatrix_translate(self):
        # translation via multmatrix
        src = """
        multmatrix([[1,0,0,5],[0,1,0,0],[0,0,1,0],[0,0,0,1]])
            cube(1);
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[0] == approx(5)


# ---------------------------------------------------------------------------
# for loop producing geometry
# ---------------------------------------------------------------------------

class TestForGeometry:
    def test_for_produces_multiple_bodies(self):
        src = "for (i=[0:2:4]) { translate([i,0,0]) cube(1); }"
        bodies, _ = run(src)
        # three cubes (i=0,2,4) produced as separate bodies
        assert len(bodies) == 3
        xmax = max(b.body.bounding_box()[3] for b in bodies)
        assert xmax == approx(5)

    def test_for_vector_geometry(self):
        src = "for (x=[0,10]) { translate([x,0,0]) cube(1); }"
        bodies, _ = run(src)
        assert bodies


# ---------------------------------------------------------------------------
# let blocks
# ---------------------------------------------------------------------------

class TestLetBlocks:
    def test_let_expression(self):
        _, lines = run("echo(let(x=5, y=3) x + y);")
        assert lines == ["ECHO: 8"]

    def test_let_scoping(self):
        _, lines = run("x = 1; echo(let(x=99) x);")
        assert lines == ["ECHO: 99"]

    def test_let_block_geometry(self):
        src = "let(s=3) { cube(s); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)

    def test_let_block_shadowing(self):
        src = "s = 1; let(s=5) { cube(s); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(5)


# ---------------------------------------------------------------------------
# User-defined modules
# ---------------------------------------------------------------------------

class TestUserModules:
    def test_simple_module(self):
        src = "module box(s) { cube(s); } box(3);"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)

    def test_module_named_args(self):
        src = "module box(w, h) { cube([w, h, 1]); } box(h=3, w=5);"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(5)
        assert bb[4] - bb[1] == approx(3)

    def test_if_else_geometry_true_branch(self):
        src = "if (true) { cube(2); } else { cube(5); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

    def test_module_with_children(self):
        src = """
        module twice() { children(); translate([5,0,0]) children(); }
        twice() cube(1);
        """
        bodies, _ = run(src)
        assert bodies  # produces geometry

    def test_module_default_param(self):
        src = "module box(s=2) { cube(s); } box();"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)


# ---------------------------------------------------------------------------
# Echo
# ---------------------------------------------------------------------------

class TestEcho:
    def test_echo_named_arg(self):
        _, lines = run("echo(x=42);")
        assert lines == ["ECHO: x = 42"]

    def test_echo_multiple(self):
        _, lines = run("echo(1, 2, 3);")
        assert lines == ["ECHO: 1, 2, 3"]

    def test_echo_in_module(self):
        src = "module m() { echo(99); } m();"
        _, lines = run(src)
        assert lines == ["ECHO: 99"]
