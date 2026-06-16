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
    nodes = getASTfromString(src, include_comments=False)
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

    def test_matrix_add(self):
        # `+`/`-` between lists of vectors (matrices) must recurse element-wise
        # per row, not concatenate each row's elements (e.g. `[0,0,0,0] +
        # [1,1,1,1]` must give `[1,1,1,1]`, not `[0,0,0,0,1,1,1,1]`).
        _, lines = run("echo([[0,0,0,0],[0,0,0,0]] + [[1,1,1,1],[2,2,2,2]]);")
        assert lines == ["ECHO: [[1, 1, 1, 1], [2, 2, 2, 2]]"]

    def test_matrix_subtract(self):
        _, lines = run("echo([[5,5,5],[5,5,5]] - [[1,2,3],[4,5,6]]);")
        assert lines == ["ECHO: [[4, 3, 2], [1, 0, -1]]"]

    def test_string_plus_string_is_undef(self):
        # OpenSCAD has no `+` for strings (unlike Python's str.__add__,
        # which would silently concatenate them).
        _, lines = run('echo("ab" + "cd");')
        assert lines == ["ECHO: undef"]

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
        # Ranges echo as lazy [start : step : end], not expanded
        _, lines = run("echo([1:3]);")
        assert lines == ["ECHO: [1 : 1 : 3]"]

    def test_range_step(self):
        _, lines = run("echo([0:2:6]);")
        assert lines == ["ECHO: [0 : 2 : 6]"]

    def test_range_descending(self):
        _, lines = run("echo([5:-1:3]);")
        assert lines == ["ECHO: [5 : -1 : 3]"]


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

    def test_animation_t_defaults_to_zero(self):
        _, lines = run("echo($t);")
        assert lines == ["ECHO: 0"]

    def test_animation_t_set_via_viewport_params(self):
        nodes = getASTfromString("echo($t);", include_comments=False)
        root_scope = build_scopes(nodes)
        echo_lines = []
        ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg))
        ev.evaluate(nodes, root_scope, {"$t": 0.25})
        assert echo_lines == ["ECHO: 0.25"]


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

    def test_round_half_away_from_zero(self):
        # OpenSCAD rounds .5 away from zero, unlike Python's round-half-to-even
        # (Python's round(2.5) == 2 and round(-0.5) == 0).
        _, lines = run("echo(round(2.5), round(-2.5), round(0.5), round(-0.5));")
        assert lines == ["ECHO: 3, -3, 1, -1"]

    def test_min(self):
        _, lines = run("echo(min(5, 3, 8));")
        assert lines == ["ECHO: 3"]

    def test_max(self):
        _, lines = run("echo(max(5, 3, 8));")
        assert lines == ["ECHO: 8"]

    def test_min_single_scalar(self):
        # A single non-list argument is returned as-is.
        _, lines = run("echo(min(5));")
        assert lines == ["ECHO: 5"]

    def test_min_max_multiple_vector_args_is_undef(self):
        # Real OpenSCAD only supports a single vector argument (returns
        # min/max of its elements) or multiple scalar arguments; mixing in
        # more than one vector is undef.
        _, lines = run("echo(min([1,5],[3,2]), max([1,5],[3,2]));")
        assert lines == ["ECHO: undef, undef"]

    def test_sin(self):
        _, lines = run("echo(sin(90));")
        assert lines == ["ECHO: 1"]

    def test_cos(self):
        _, lines = run("echo(cos(0));")
        assert lines == ["ECHO: 1"]

    def test_sin_cos_tan_exact_at_90_degree_multiples(self):
        # Real OpenSCAD special-cases exact multiples of 90 degrees to avoid
        # floating-point noise (e.g. cos(90) -> 6.12e-17, tan(90) -> 1.63e+16).
        _, lines = run(
            "echo(sin(180), cos(90), cos(180), tan(90), tan(270), "
            "sin(360), sin(-90), tan(180), sin(450), cos(-270));"
        )
        assert lines == ["ECHO: 0, 0, -1, inf, -inf, 0, -1, 0, 1, 0"]

    def test_sin_cos_tan_near_90_degree_multiple_is_not_special_cased(self):
        _, lines = run("echo(cos(90.0000001));")
        assert float(lines[0].split(": ")[1]) == approx(-1.74533e-9)

    def test_cos_of_infinity_is_nan(self):
        _, lines = run("echo(cos(1/0));")
        assert lines == ["ECHO: nan"]

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

    def test_pow_zero_negative_exponent(self):
        # 0 ** negative is +inf in OpenSCAD; Python's pow()/math.pow() raise.
        _, lines = run("echo(pow(0, -1));")
        assert lines == ["ECHO: inf"]

    def test_norm(self):
        _, lines = run("echo(norm([3, 4]));")
        assert float(lines[0].split(": ")[1]) == approx(5.0)

    def test_cross(self):
        _, lines = run("echo(cross([1,0,0],[0,1,0]));")
        assert lines == ["ECHO: [0, 0, 1]"]

    def test_cross_2d(self):
        # 2D cross product returns a scalar: a[0]*b[1] - a[1]*b[0]
        _, lines = run("echo(cross([1,2],[3,4]));")
        assert lines == ["ECHO: -2"]

    def test_chr(self):
        _, lines = run("echo(chr(65));")
        assert lines == ['ECHO: "A"']

    def test_chr_vector(self):
        # chr() also accepts a vector of code points, converting and
        # concatenating each one.
        _, lines = run("echo(chr([65,66,67]));")
        assert lines == ['ECHO: "ABC"']

    def test_chr_vector_truncates_floats(self):
        _, lines = run("echo(chr([65.7,66.2]));")
        assert lines == ['ECHO: "AB"']

    def test_chr_empty_vector(self):
        _, lines = run("echo(chr([]));")
        assert lines == ['ECHO: ""']

    def test_ord(self):
        _, lines = run('echo(ord("A"));')
        assert lines == ["ECHO: 65"]

    def test_ord_multichar_uses_first_char(self):
        _, lines = run('echo(ord("ab"));')
        assert lines == ["ECHO: 97"]


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

    def test_undefined_function_warns_and_returns_undef(self):
        # Real OpenSCAD treats a call to an unknown function as a WARNING
        # ("Ignoring unknown function 'X'") and evaluates it to undef,
        # rather than aborting the whole render.
        _, lines = run("echo(nope(1));")
        assert lines[0] == "WARNING: Ignoring unknown function 'nope' in file <string>, line 1"
        assert lines[1] == "ECHO: undef"

    def test_undefined_function_in_nested_call_no_traceback(self):
        src = """
        function outer() = inner();
        echo(outer());
        """
        _, lines = run(src)
        assert lines[0] == "WARNING: Ignoring unknown function 'inner' in file <string>, line 2"
        assert lines[1] == "ECHO: undef"


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
        assert lines == ["ECHO: [[1, 2, 3], [4, 5, 6]]"]

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

    def test_c_style_for(self):
        # `for (init...; cond; incr...)` — the C-style for in a list
        # comprehension (parsed as `ListCompCFor`), used e.g. by BOSL2's
        # `cumsum()`: [for (a=v[0], i=1; i<=len(v); a = i<len(v)?a+v[i]:a, i=i+1) a]
        src = """
        v = [0, 1, 2, 3];
        echo([for (a = v[0], i = 1; i <= len(v); a = i < len(v) ? a + v[i] : a, i = i + 1) a]);
        """
        _, lines = run(src)
        assert lines == ["ECHO: [0, 1, 3, 6]"]


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

    def test_sphere_diameter(self):
        bodies, _ = run("sphere(d=4, $fn=32);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(4, rel=0.02)

    def test_cylinder_diameter(self):
        bodies, _ = run("cylinder(h=5, d=6, $fn=32);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(6, rel=0.02)

    def test_cylinder_r1_r2(self):
        bodies, _ = run("cylinder(h=10, r1=3, r2=1, $fn=32);")
        bb = bbox(bodies)
        assert bb[5] - bb[2] == approx(10, rel=0.01)
        # base diameter = 6
        assert bb[3] - bb[0] == approx(6, rel=0.02)

    def test_cylinder_d1_d2(self):
        bodies, _ = run("cylinder(h=10, d1=6, d2=2, $fn=32);")
        bb = bbox(bodies)
        assert bb[5] - bb[2] == approx(10, rel=0.01)
        assert bb[3] - bb[0] == approx(6, rel=0.02)


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

    def test_scale_uniform(self):
        # scalar argument scales all three axes uniformly
        bodies, _ = run("scale(3) cube(1);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)
        assert bb[4] - bb[1] == approx(3)

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

    def test_let_sequential_binding_reference(self):
        # Later bindings in the same let() can reference earlier ones.
        _, lines = run("echo(let(a=1, b=a+1) b);")
        assert lines == ["ECHO: 2"]


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

    def test_children_indexed(self):
        src = """
        module first_only() { children(0); }
        first_only() { cube(2); cube(5); }
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

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


# ---------------------------------------------------------------------------
# assert() statement
# ---------------------------------------------------------------------------

class TestAssert:
    def test_assert_statement(self):
        # ModularAssert as a statement produces no geometry and no error
        bodies, _ = run("assert(true); cube(1);")
        assert len(bodies) == 1

    def test_assert_modular_call(self):
        # assert(true) with a child module propagates the child's geometry
        bodies, _ = run("assert(true) cube(1);")
        assert len(bodies) == 1
        assert abs(bodies[0].body.volume() - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Unknown / echo-as-module / misc module dispatch
# ---------------------------------------------------------------------------

class TestModuleDispatch:
    def test_echo_as_modular_call_with_children(self):
        # echo() with children runs echo and returns no geometry
        bodies, lines = run('echo("hi") cube(1);')
        assert bodies == []
        assert "hi" in lines[0]

    def test_unknown_module_skipped(self):
        # Unrecognised module name produces no geometry, no error
        bodies, _ = run("unknownmod() cube(1);")
        assert bodies == []

    def test_module_with_dollar_arg(self):
        # $fn passed as named arg to a user module goes into dyn
        src = "module m($fn=8) { sphere(r=1); } m($fn=16);"
        bodies, _ = run(src)
        assert bodies


# ---------------------------------------------------------------------------
# Primitive edge cases
# ---------------------------------------------------------------------------

class TestPrimitiveEdgeCases:
    def test_sphere_no_args(self):
        # sphere() with no arguments defaults to r=1
        bodies, _ = run("sphere($fn=16);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2, rel=0.05)

    def test_cylinder_no_r(self):
        # cylinder with no r → defaults r1=r2=1
        bodies, _ = run("cylinder(h=5, $fn=16);")
        bb = bbox(bodies)
        assert bb[5] - bb[2] == approx(5, rel=0.01)
        assert bb[3] - bb[0] == approx(2, rel=0.05)

    def test_cylinder_r1_only(self):
        # cylinder with r1 but no r2 → r2 defaults to r1
        bodies, _ = run("cylinder(h=5, r1=3, $fn=16);")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(6, rel=0.05)


# ---------------------------------------------------------------------------
# Transform edge cases
# ---------------------------------------------------------------------------

class TestTransformEdgeCases:
    def test_transform_no_children(self):
        # translate with no children returns no geometry
        bodies, _ = run("translate([1,0,0]);")
        assert bodies == []

    def test_rotate_scalar_no_v(self):
        # rotate(angle) with no axis vector defaults to z-axis
        bodies, _ = run("rotate(90) translate([5,0,0]) cube(1);")
        bb = bbox(bodies)
        # cube was on +x, after 90° z-rotation should land on -y/+y
        assert abs(bb[1]) == approx(5, rel=0.01)

    def test_rotate_zero_axis(self):
        # rotate with a zero-length axis — identity rotation
        bodies, _ = run("rotate(90, v=[0,0,0]) translate([5,0,0]) cube(1);")
        bb = bbox(bodies)
        assert bb[0] == approx(5, rel=0.01)

    def test_translate_scalar_v(self):
        # translate with a scalar (becomes [v, 0, 0])
        bodies, _ = run("translate(5) cube(1);")
        bb = bbox(bodies)
        assert bb[0] == approx(5)

    def test_translate_2d_vector(self):
        # translate with a 2-element vector (z padded to 0)
        bodies, _ = run("translate([3, 4]) cube(1);")
        bb = bbox(bodies)
        assert bb[0] == approx(3)
        assert bb[1] == approx(4)

    def test_multmatrix_3x3(self):
        # multmatrix with 3×3 rows — columns are padded with 0 (no translation)
        src = "multmatrix([[1,0,0],[0,1,0],[0,0,1]]) translate([2,0,0]) cube(1);"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[0] == approx(2)


# ---------------------------------------------------------------------------
# Color edge cases
# ---------------------------------------------------------------------------

class TestColorEdgeCases:
    def test_color_no_children(self):
        # color() with no children produces no geometry
        bodies, _ = run('color("red");')
        assert bodies == []


# ---------------------------------------------------------------------------
# CSG edge cases
# ---------------------------------------------------------------------------

class TestCSGEdgeCases:
    def test_union_no_children(self):
        bodies, _ = run("union();")
        assert bodies == []

    def test_union_single_child(self):
        bodies, _ = run("union() { cube(2); }")
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

    def test_hull_no_children(self):
        bodies, _ = run("hull();")
        assert bodies == []

    def test_children_out_of_range(self):
        # children(idx) where idx >= $children returns no geometry
        src = "module m() { children(10); } m() cube(1);"
        bodies, _ = run(src)
        assert bodies == []


# ---------------------------------------------------------------------------
# for loop body variables
# ---------------------------------------------------------------------------

class TestForBodyVars:
    def test_for_body_variable(self):
        # Variable assigned inside a for body (not the loop var) must be visible to siblings
        src = "for (a=[1:3]) { x = a*2; echo(x); }"
        _, lines = run(src)
        assert lines == ["ECHO: 2", "ECHO: 4", "ECHO: 6"]

    def test_for_body_var_geometry(self):
        # Variable binding in for body used in geometry
        src = "r = 50; for (a=[0:90:270]) { pos = r*[cos(a), sin(a), 0]; translate(pos) cube(2); }"
        bodies, _ = run(src)
        assert len(bodies) == 4

    def test_for_scalar_iterable(self):
        # for over a scalar — treated as [scalar] (single-element sequence)
        src = "for (x = 5) { echo(x); }"
        _, lines = run(src)
        assert lines == ["ECHO: 5"]


# ---------------------------------------------------------------------------
# Expression edge cases
# ---------------------------------------------------------------------------

class TestExpressionEdgeCases:
    def test_division_by_zero(self):
        _, lines = run("echo(1/0);")
        assert lines == ["ECHO: inf"]

    def test_neg_division_by_zero(self):
        _, lines = run("echo(-1/0);")
        assert lines == ["ECHO: -inf"]

    def test_zero_division_by_zero(self):
        _, lines = run("echo(0/0);")
        assert lines == ["ECHO: nan"]

    def test_bool_arithmetic_is_undef(self):
        _, lines = run("echo(true + 1);")
        assert lines == ["ECHO: undef"]

    def test_bool_mul_is_undef(self):
        _, lines = run("echo(true * 5);")
        assert lines == ["ECHO: undef"]

    def test_scalar_times_matrix(self):
        _, lines = run("echo(2 * [[1,2],[3,4]]);")
        assert lines == ["ECHO: [[2, 4], [6, 8]]"]

    def test_matrix_times_scalar(self):
        _, lines = run("echo([[1,2],[3,4]] * 2);")
        assert lines == ["ECHO: [[2, 4], [6, 8]]"]

    def test_vector_dot_product(self):
        _, lines = run("echo([1,2,3] * [4,5,6]);")
        assert lines == ["ECHO: 32"]

    def test_matrix_times_vector(self):
        _, lines = run("echo([[1,0],[0,1]] * [3,4]);")
        assert lines == ["ECHO: [3, 4]"]

    def test_vector_times_matrix(self):
        _, lines = run("echo([3,4] * [[1,0],[0,1]]);")
        assert lines == ["ECHO: [3, 4]"]

    def test_matrix_times_matrix(self):
        _, lines = run("echo([[1,2],[3,4]] * [[1,0],[0,1]]);")
        assert lines == ["ECHO: [[1, 2], [3, 4]]"]

    def test_vector_divided_by_scalar(self):
        _, lines = run("echo([2,4,6] / 2);")
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_undef_comparison_lt(self):
        # Ordering comparisons between mismatched types (here undefined vs.
        # number) warn and evaluate to undef, matching real OpenSCAD.
        _, lines = run("echo(undef < 1);")
        assert lines == ["WARNING: undefined operation (undefined < number) in file <string>, line 1",
                          "ECHO: undef"]

    def test_number_equals_bool_is_false(self):
        # bool is a distinct type from number in OpenSCAD: 1 == true is
        # false, unlike Python where True == 1.
        _, lines = run("echo(1 == true, true == 1, 0 == false);")
        assert lines == ["ECHO: false, false, false"]

    def test_int_equals_float_is_true(self):
        _, lines = run("echo(1 == 1.0);")
        assert lines == ["ECHO: true"]

    def test_list_equality_with_bool_element_is_false(self):
        # [1, true] != [1, 1] even though Python's `1 == True`.
        _, lines = run("echo([1, true] == [1, 1]);")
        assert lines == ["ECHO: false"]

    def test_list_equality_different_lengths(self):
        _, lines = run("echo([1,2] == [1,2,3]);")
        assert lines == ["ECHO: false"]

    def test_bool_greater_than_number_is_undef(self):
        _, lines = run("echo(true > 0);")
        assert lines == ["WARNING: undefined operation (bool > number) in file <string>, line 1",
                          "ECHO: undef"]

    def test_bool_comparison_works(self):
        _, lines = run("echo(true >= false);")
        assert lines == ["ECHO: true"]

    def test_vector_comparison_works(self):
        _, lines = run("echo([1,2] < [3,4]);")
        assert lines == ["ECHO: true"]

    def test_floor_of_nan_is_nan(self):
        _, lines = run("echo(floor(0/0));")
        assert lines == ["ECHO: nan"]

    def test_ceil_of_inf_is_inf(self):
        _, lines = run("echo(ceil(1/0));")
        assert lines == ["ECHO: inf"]

    def test_round_of_nan_is_nan(self):
        _, lines = run("echo(round(0/0));")
        assert lines == ["ECHO: nan"]

    def test_sqrt_negative_is_nan(self):
        _, lines = run("echo(sqrt(-1));")
        assert lines == ["ECHO: nan"]

    def test_ln_zero_is_neg_inf(self):
        _, lines = run("echo(ln(0));")
        assert lines == ["ECHO: -inf"]

    def test_ln_negative_is_nan(self):
        _, lines = run("echo(ln(-1));")
        assert lines == ["ECHO: nan"]

    def test_asin_out_of_range_is_nan(self):
        _, lines = run("echo(asin(2));")
        assert lines == ["ECHO: nan"]

    def test_string_negative_index_is_undef(self):
        _, lines = run('echo("hello"[-1]);')
        assert lines == ["ECHO: undef"]

    def test_index_out_of_bounds(self):
        _, lines = run("echo([1,2,3][10]);")
        assert lines == ["ECHO: undef"]

    def test_index_non_list(self):
        _, lines = run("echo(5[0]);")
        assert lines == ["ECHO: undef"]

    def test_member_not_in_swizzle(self):
        # .w on a 2-element vector is out of range
        _, lines = run("echo([1,2].w);")
        assert lines == ["ECHO: undef"]

    def test_named_arg_to_builtin(self):
        # OpenSCAD maps named args to positional for built-ins
        _, lines = run("echo(abs(x=-3));")
        assert lines == ["ECHO: 3"]

    def test_let_op_in_expression(self):
        _, lines = run("echo(let(a=3, b=4) a + b);")
        assert lines == ["ECHO: 7"]


# ---------------------------------------------------------------------------
# List comprehension edge cases
# ---------------------------------------------------------------------------

class TestListCompEdgeCases:
    def test_listcomp_for_nested_body(self):
        # bracketed sub-comprehension in for body → each iteration yields one list
        _, lines = run("echo([for (i=[1:3]) [for (j=[1:2]) i*j]]);")
        assert lines == ["ECHO: [[1, 2], [2, 4], [3, 6]]"]

    def test_listcomp_if_false_no_else(self):
        # ListCompIf with false condition and no else → item excluded
        _, lines = run("echo([for (i=[1:3]) if (i > 10) i]);")
        assert lines == ["ECHO: []"]

    def test_listcomp_ifelse_false_branch(self):
        # ListCompIfElse, condition false → take false branch
        _, lines = run("echo([for (i=[1:2]) if (i > 1) i*10 else i]);")
        assert lines == ["ECHO: [1, 20]"]

    def test_listcomp_for_undef_iterable(self):
        # for with undef iterable → empty result
        _, lines = run("echo([for (x = undef) x]);")
        assert lines == ["ECHO: []"]

    def test_listcomp_for_scalar_iterable(self):
        # for with scalar iterable → treated as single-element sequence
        _, lines = run("echo([for (x = 5) x]);")
        assert lines == ["ECHO: [5]"]


# ---------------------------------------------------------------------------
# Range edge cases
# ---------------------------------------------------------------------------

class TestRangeEdgeCases:
    def test_range_zero_step(self):
        # [start:0:end] echoes as a lazy range object (iteration yields nothing)
        _, lines = run("echo([1:0:5]);")
        assert lines == ["ECHO: [1 : 0 : 5]"]

    def test_range_zero_step_iteration(self):
        # iterating a zero-step range produces no values
        _, lines = run("echo([for (i=[1:0:5]) i]);")
        assert lines == ["ECHO: []"]

    def test_range_indexing(self):
        # Indexing a range yields its [start, step, end] components, not its
        # iterated values: `[2:3:11][0]` -> 2, `[1]` -> 3, `[2]` -> 11. This is
        # what BOSL2's `is_range()`/`is_finite()` inspect to detect ranges.
        _, lines = run("r = [2:3:11]; echo(r[0], r[1], r[2]);")
        assert lines == ["ECHO: 2, 3, 11"]


# ---------------------------------------------------------------------------
# Function literal values (`function (params) expr`)
# ---------------------------------------------------------------------------

class TestFunctionLiterals:
    def test_call_stored_function_literal(self):
        _, lines = run("g = function(x) x*2; echo(g(3));")
        assert lines == ["ECHO: 6"]

    def test_function_literal_closure(self):
        # The literal closes over the scope where it was written, not the call site.
        _, lines = run("y = 10; h = function(x) x + y; echo(h(5));")
        assert lines == ["ECHO: 15"]

    def test_function_literal_default_param(self):
        _, lines = run("k = function(x, y=100) x + y; echo(k(1));")
        assert lines == ["ECHO: 101"]

    def test_function_literal_named_arg(self):
        _, lines = run("k = function(x, y=100) x + y; echo(k(1, y=5));")
        assert lines == ["ECHO: 6"]

    def test_pass_function_literal_as_argument(self):
        _, lines = run("function apply(fn, v) = fn(v); echo(apply(function(x) x*x, 4));")
        assert lines == ["ECHO: 16"]


# ---------------------------------------------------------------------------
# Function call edge cases
# ---------------------------------------------------------------------------

class TestFunctionCallEdgeCases:
    def test_call_non_function_variable(self):
        # Calling a variable that is not a function returns undef (no error)
        _, lines = run("x = [1,2,3]; echo(x());")
        assert lines == ["ECHO: undef"]

    def test_missing_param_is_undef(self):
        # Function called with fewer args than params → missing param is undef
        _, lines = run("function f(a, b) = b; echo(f(1));")
        assert lines == ["ECHO: undef"]


# ---------------------------------------------------------------------------
# Color numeric fallback
# ---------------------------------------------------------------------------

class TestColorNumericFallback:
    def test_color_non_string_non_list(self):
        # color() with a non-string, non-list arg falls back to white
        bodies, _ = run("color(42) cube(1);")
        assert bodies  # geometry still produced


# ---------------------------------------------------------------------------
# children() with no children bodies
# ---------------------------------------------------------------------------

class TestChildrenNoChildren:
    def test_children_with_no_children(self):
        # Calling children() inside a module with no children passed
        src = "module m() { children(); } m();"
        bodies, _ = run(src)
        assert bodies == []


# ---------------------------------------------------------------------------
# for loop with undef iterable (modular for, not list comp)
# ---------------------------------------------------------------------------

class TestForUndef:
    def test_for_undef_iterable(self):
        # Modular for with undef iterable produces no geometry
        src = "for (x = undef) { echo(x); }"
        _, lines = run(src)
        assert lines == []


# ---------------------------------------------------------------------------
# Function literal (lambda)
# ---------------------------------------------------------------------------

class TestFunctionLiteral:
    def test_function_literal_stored(self):
        # function literal is stored as a value (calling it is not yet implemented)
        src = "f = function(x) x * 3; echo(is_undef(f));"
        _, lines = run(src)
        # f stores the literal node (not a Python callable) — is_undef returns false
        assert lines == ["ECHO: false"]


# ---------------------------------------------------------------------------
# each in for body (scalar)
# ---------------------------------------------------------------------------

class TestEachInForBody:
    def test_each_scalar_in_for_body(self):
        # each applied to a scalar in for body wraps it in a list
        _, lines = run("echo([for (i=[1:3]) each i]);")
        assert lines == ["ECHO: [1, 2, 3]"]


# ---------------------------------------------------------------------------
# Expression operators: EchoOp, AssertOp
# ---------------------------------------------------------------------------

class TestExpressionOps:
    def test_echo_op_passthrough(self):
        # echo("msg") expr — evaluates to expr, side-effect logs the args
        _, lines = run('x = echo("debug") 5; echo(x);')
        assert lines[-1] == "ECHO: 5"

    def test_echo_op_side_effect(self):
        # the echo side-effect fires before the enclosing expression is used
        _, lines = run('x = echo("side") 42; echo(x);')
        assert 'ECHO: "side"' in lines
        assert "ECHO: 42" in lines

    def test_assert_op_passthrough(self):
        # assert(true) expr — evaluates to expr when condition holds
        _, lines = run('x = assert(true) 5; echo(x);')
        assert lines[-1] == "ECHO: 5"

    def test_assert_op_fails_on_false(self):
        # assert(false) should raise an error
        import pytest
        with pytest.raises(Exception):
            run('x = assert(false) 5; echo(x);')

    def test_assert_op_message(self):
        # assert(false, "msg") — error message included in EvalError
        import pytest
        with pytest.raises(Exception, match="Assertion 'false' failed"):
            run('x = assert(false, "msg") 5; echo(x);')


# ---------------------------------------------------------------------------
# List comprehension: let bindings
# ---------------------------------------------------------------------------

class TestListCompLet:
    def test_let_in_listcomp(self):
        # let inside list comprehension introduces a local binding
        _, lines = run("echo([for (i=[1:3]) let(j=i*2) j]);")
        assert lines == ["ECHO: [2, 4, 6]"]

    def test_let_multiple_bindings(self):
        _, lines = run("echo([for (i=[1:2]) let(a=i+1, b=i*3) [a, b]]);")
        assert lines == ["ECHO: [[2, 3], [3, 6]]"]

    def test_nested_let_in_listcomp(self):
        # let can shadow outer variable
        _, lines = run("x = 10; echo([for (i=[1:2]) let(x=i) x]);")
        assert lines == ["ECHO: [1, 2]"]

    def test_let_in_listcomp_with_if(self):
        # let combined with if filter
        _, lines = run("echo([for (i=[1:4]) let(j=i*2) if (j > 4) j]);")
        assert lines == ["ECHO: [6, 8]"]

    def test_grid_let_comprehension(self):
        # the original bug report: nested for with outer let binding
        _, lines = run("grid = [for(h=[0:2]) [let(b=h) for(a=[0:2]) a+b]]; echo(grid);")
        assert lines == ["ECHO: [[0, 1, 2], [1, 2, 3], [2, 3, 4]]"]


# ---------------------------------------------------------------------------
# List comprehension: each with nested lists
# ---------------------------------------------------------------------------

class TestListCompEach:
    def test_each_splices_list(self):
        # each over a list splices its elements into the parent
        _, lines = run("echo([each [1,2,3]]);")
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_each_nested_list_not_flattened(self):
        # each over a list of lists keeps sub-lists intact
        _, lines = run("a = [[1,2],[3,4]]; echo([each a]);")
        assert lines == ["ECHO: [[1, 2], [3, 4]]"]

    def test_each_in_for_body(self):
        # each inside a for body splices one level
        _, lines = run("echo([for (i=[[1,2],[3,4]]) each i]);")
        assert lines == ["ECHO: [1, 2, 3, 4]"]

    def test_each_preserves_inner_structure(self):
        # each splices exactly one level — inner nesting is preserved
        # a has one element: [[1,2],[3,4]]; each a yields that element as-is
        _, lines = run("a = [[[1,2],[3,4]]]; echo([each a]);")
        assert lines == ["ECHO: [[[1, 2], [3, 4]]]"]


# ---------------------------------------------------------------------------
# New built-ins: sign, rands, PI, is_function, search, polyhedron
# ---------------------------------------------------------------------------

class TestNewBuiltins:
    def test_sign_positive(self):
        _, lines = run("echo(sign(5));")
        assert lines == ["ECHO: 1"]

    def test_sign_negative(self):
        _, lines = run("echo(sign(-3));")
        assert lines == ["ECHO: -1"]

    def test_sign_zero(self):
        _, lines = run("echo(sign(0));")
        assert lines == ["ECHO: 0"]

    def test_PI_constant(self):
        _, lines = run("echo(PI);")
        assert len(lines) == 1
        assert abs(float(lines[0].replace("ECHO: ", "")) - 3.14159265) < 1e-5

    def test_rands_length(self):
        _, lines = run("v = rands(0, 1, 5); echo(len(v));")
        assert lines == ["ECHO: 5"]

    def test_rands_range(self):
        _, lines = run("v = rands(10, 20, 3, 42); echo(v[0] >= 10 && v[0] <= 20);")
        assert lines == ["ECHO: true"]

    def test_rands_seeded_deterministic(self):
        _, lines1 = run("v = rands(0, 100, 4, 123); echo(v);")
        _, lines2 = run("v = rands(0, 100, 4, 123); echo(v);")
        assert lines1 == lines2

    def test_is_function_true(self):
        _, lines = run("g = function(x) x*2; echo(is_function(g));")
        assert lines == ["ECHO: true"]

    def test_is_function_false_on_named_function_reference(self):
        # Real OpenSCAD: variables and functions live in separate namespaces,
        # so a bare reference to `function f(x) = ...` is an unknown variable
        # (-> undef, with a warning), not a callable value.
        _, lines = run("function f(x) = x*2; echo(is_function(f));")
        assert lines == ["WARNING: Ignoring unknown variable 'f' in file <string>, line 1",
                          "ECHO: false"]

    def test_is_function_false_on_num(self):
        _, lines = run("echo(is_function(42));")
        assert lines == ["ECHO: false"]

    def test_is_num_excludes_bool(self):
        # bool is not a number in OpenSCAD
        _, lines = run("echo(is_num(true));")
        assert lines == ["ECHO: false"]

    def test_is_num_excludes_nan(self):
        # nan fails is_num() in real OpenSCAD, even though it's a float.
        _, lines = run("echo(is_num(0/0));")
        assert lines == ["ECHO: false"]

    def test_is_num_includes_inf(self):
        # ...but inf/-inf pass.
        _, lines = run("echo(is_num(1/0), is_num(-1/0));")
        assert lines == ["ECHO: true, true"]

    def test_unknown_variable_warns_and_returns_undef(self):
        _, lines = run("echo(totally_undefined_var);")
        assert lines == ["WARNING: Ignoring unknown variable 'totally_undefined_var' in file <string>, line 1",
                          "ECHO: undef"]

    def test_search_string_single_char(self):
        # String match in string vector → char-by-char; single char in string
        _, lines = run('echo(search("b", "abc"));')
        assert lines == ["ECHO: [1]"]

    def test_search_string_single_char_not_found(self):
        # Not found with num_returns=1 → dropped from outer list → []
        _, lines = run('echo(search("z", "abc"));')
        assert lines == ["ECHO: []"]

    def test_search_list(self):
        _, lines = run('echo(search(["b","a"], ["a","b","c"]));')
        assert lines == ["ECHO: [1, 0]"]

    def test_search_vector_match_direct_equality(self):
        # When the match value is itself a vector, it's compared directly
        # against each whole element of the haystack (not column-indexed) —
        # this is the basis of BOSL2's `in_list(v, [UP,RIGHT,BACK])`.
        _, lines = run('echo(search([[0,0,1]], [[0,0,1],[1,0,0],[0,1,0]]));')
        assert lines == ["ECHO: [0]"]

    def test_search_scalar_match_uses_index_col(self):
        # A scalar match value still compares against vector[i][index_col].
        _, lines = run('echo(search([0,0,1], [[0,0,1],[1,0,0],[0,1,0]]));')
        assert lines == ["ECHO: [0, 0, 1]"]

    def test_search_string_as_char_array(self):
        # Multi-char string: each char searched independently in a string vector
        _, lines = run('echo(search("ba", "abcd"));')
        assert lines == ["ECHO: [1, 0]"]

    def test_search_string_num_returns_zero(self):
        # num_returns=0 → all matches per char
        _, lines = run('echo(search("a", "abcdabcd", 0));')
        assert lines == ["ECHO: [[0, 4]]"]

    def test_search_string_in_string(self):
        # Single char in string vector
        _, lines = run('echo(search("a", "abcdabcd"));')
        assert lines == ["ECHO: [0]"]

    def test_search_numeric_scalar(self):
        # Numeric (non-string) scalar: returns list of up to num_returns matches
        _, lines = run('echo(search(2, [1,2,3,2]));')
        assert lines == ["ECHO: [1]"]

    def test_search_numeric_not_found(self):
        _, lines = run('echo(search(9, [1,2,3]));')
        assert lines == ["ECHO: []"]

    def test_polyhedron_tetrahedron(self):
        # Simple tetrahedron using OpenSCAD's CW-from-outside face winding
        src = """
        polyhedron(
          points=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]],
          faces=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]]
        );
        """
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert bodies[0].body.volume() > 0

    def test_polyhedron_cube_equiv(self):
        # 6-face polyhedron matching a unit cube; faces use OpenSCAD CW-from-outside winding
        src = """
        polyhedron(
          points=[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]],
          faces=[[0,1,2,3],[4,7,6,5],[0,4,5,1],[1,5,6,2],[2,6,7,3],[3,7,4,0]]
        );
        """
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert abs(bodies[0].body.volume() - 1.0) < 0.01

    def test_polyhedron_triangles_alias(self):
        # legacy 'triangles' parameter name should work identically to 'faces'
        src = """
        polyhedron(
          points=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]],
          triangles=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]]
        );
        """
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert bodies[0].body.volume() > 0


# ---------------------------------------------------------------------------
# 2D primitives, linear_extrude, rotate_extrude, minkowski
# ---------------------------------------------------------------------------

class Test2DAndExtrusion:
    def test_circle_produces_section(self):
        bodies, _ = run("circle(r=5);")
        assert len(bodies) == 1
        assert bodies[0].section is not None
        assert bodies[0].section.area() > 0

    def test_square_produces_section(self):
        bodies, _ = run("square([3, 4]);")
        assert len(bodies) == 1
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 12.0) < 0.01

    def test_square_centered(self):
        bodies, _ = run("square(2, center=true);")
        bounds = bodies[0].section.bounds()
        assert abs(bounds[0] - (-1.0)) < 1e-6  # min_x
        assert abs(bounds[2] - 1.0) < 1e-6     # max_x

    def test_polygon_triangle(self):
        bodies, _ = run("polygon([[0,0],[1,0],[0,1]]);")
        assert len(bodies) == 1
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 0.5) < 0.01

    def test_polygon_with_hole(self):
        # outer square minus inner square hole
        src = "polygon(points=[[0,0],[4,0],[4,4],[0,4],[1,1],[3,1],[3,3],[1,3]], paths=[[0,1,2,3],[4,5,6,7]]);"
        bodies, _ = run(src)
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 12.0) < 0.1  # 16 - 4

    def test_linear_extrude_circle(self):
        # Use $fn=64 to get close to analytic volume; 2% tolerance
        src = "linear_extrude(height=5) circle(r=2, $fn=64);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert bodies[0].body is not None
        import math
        expected = math.pi * 4 * 5  # pi*r^2*h
        assert abs(bodies[0].body.volume() - expected) / expected < 0.02

    def test_linear_extrude_center(self):
        src = "linear_extrude(height=4, center=true) square([2,2]);"
        bodies, _ = run(src)
        bb = bodies[0].body.bounding_box()  # (min_x, min_y, min_z, max_x, max_y, max_z)
        assert abs(bb[2] - (-2.0)) < 0.01   # min_z
        assert abs(bb[5] - 2.0) < 0.01      # max_z

    def test_linear_extrude_twist(self):
        src = "linear_extrude(height=10, twist=90, slices=20) square([2,2]);"
        bodies, _ = run(src)
        assert bodies[0].body.volume() > 0

    def test_linear_extrude_scale(self):
        # scale=0 at top → cone shape, volume less than full cylinder
        src = "linear_extrude(height=3, scale=0) circle(r=1);"
        bodies, _ = run(src)
        assert bodies[0].body.volume() > 0

    def test_rotate_extrude_full(self):
        # revolve a 1x1 square at x=2 → torus-like; volume ≈ 2π²Rr² = 2π²*2.5*0.25
        src = "rotate_extrude($fn=64) square([1,1], center=true);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert bodies[0].body is not None
        assert bodies[0].body.volume() > 0

    def test_rotate_extrude_partial(self):
        src = "rotate_extrude(angle=180, $fn=32) square([1,1]);"
        bodies, _ = run(src)
        assert bodies[0].body.volume() > 0

    def test_minkowski_inflates_cube(self):
        # cube + sphere → rounded cube; volume > cube alone
        src = "minkowski() { cube([2,2,2]); sphere(r=0.5, $fn=16); }"
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert bodies[0].body.volume() > 8.0  # more than the original cube

    def test_minkowski_single_child(self):
        # single child — just returns the child unchanged
        src = "minkowski() { cube(2); }"
        bodies, _ = run(src)
        assert abs(bodies[0].body.volume() - 8.0) < 0.01

    def test_roof_square_pyramid(self):
        # roof() over a square produces a hip-roof/pyramid: apex height ==
        # inradius (half the square's side), bbox close to (0,0,0)-(10,10,5).
        # The straight-skeleton path is exact for a square, so this matches
        # the analytic pyramid volume to within float precision.
        src = "roof() square([10,10]);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[2] == 0.0
        assert abs(bb[5] - 5.0) < 0.5
        expected_vol = 10 * 10 * 5 / 3  # pyramid: base_area * height / 3
        assert abs(bodies[0].body.volume() - expected_vol) / expected_vol < 1e-3

    def test_roof_circle_cone(self):
        # roof() over a circle produces a cone-like solid; apex height ==
        # circle radius.
        src = "roof() circle(5);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[2] == 0.0
        assert abs(bb[5] - 5.0) < 0.5
        assert bodies[0].body.volume() > 0

    def test_roof_straight_matches_voronoi_for_convex(self):
        # for a convex shape, method="straight" and the default "voronoi"
        # produce equivalent results.
        bodies_v, _ = run("roof() square([10,10]);")
        bodies_s, _ = run('roof(method="straight") square([10,10]);')
        assert bodies_v[0].body.bounding_box() == bodies_s[0].body.bounding_box()
        assert abs(bodies_v[0].body.volume() - bodies_s[0].body.volume()) < 1e-6

    def test_roof_no_children_returns_none(self):
        bodies, _ = run("roof();")
        assert bodies == []

    def test_roof_concave_polygon(self):
        # L-shaped polygon with a reflex corner, both arms 4 units wide —
        # the straight skeleton collapses to a single ridge point at height
        # 2 with no intermediate topology events, so this is exact too.
        src = "roof() polygon([[0,0],[10,0],[10,4],[4,4],[4,10],[0,10]]);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[2] == 0.0
        assert abs(bb[5] - 2.0) < 1e-3
        expected_vol = 58.0 + 2.0 / 3.0
        assert abs(bodies[0].body.volume() - expected_vol) / expected_vol < 1e-3

    def test_roof_rectangle_ridge(self):
        # An 8x2 rectangle's straight skeleton is a hip roof with a ridge of
        # length 6 at height 1 (half the short side).
        src = "roof() square([8,2]);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[2] == 0.0
        assert abs(bb[5] - 1.0) < 1e-3
        assert abs(bodies[0].body.volume() - 22.0 / 3.0) < 1e-2

    def test_roof_asymmetric_l_exact(self):
        # Asymmetric L (arms of different widths) — the mitered offset has
        # an intermediate edge-collapse event before fully vanishing, so this
        # isn't "stable" for the tier-1 closed-form path. The tier-2
        # skeleton-graph path (shapely_polyskel) handles it exactly: the
        # skeleton has internal nodes at heights 1 and 2, giving bbox z=2 and
        # an exact volume of 92/3.
        src = "roof() polygon([[0,0],[8,0],[8,4],[2,4],[2,8],[0,8]]);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[2] == 0.0
        assert abs(bb[5] - 2.0) < 1e-3
        assert abs(bodies[0].body.volume() - 92.0 / 3.0) / (92.0 / 3.0) < 1e-3

    def test_roof_polygon_with_hole_falls_back(self):
        # A square with a square hole is multi-contour, so neither the
        # tier-1 closed-form path nor the tier-2 skeleton-graph path (which
        # only handles single-contour polygons) applies — falls back to the
        # SDF approximation. Should still produce a valid, non-empty roof.
        src = """
        roof() polygon(
            points=[[0,0],[10,0],[10,10],[0,10],[2,2],[2,8],[8,8],[8,2]],
            paths=[[0,1,2,3],[4,5,6,7]]
        );
        """
        bodies, _ = run(src)
        assert len(bodies) == 1
        bb = bodies[0].body.bounding_box()
        assert bb[2] == 0.0
        assert 0 < bb[5]
        assert bodies[0].body.volume() > 0

    def test_roof_unknown_method_warns(self):
        bodies, echoes = run('roof(method="bogus") square([10,10]);')
        assert any("Unknown roof method 'bogus'" in e for e in echoes)
        assert len(bodies) == 1


# ---------------------------------------------------------------------------
# offset, projection, intersection_for, lookup, $children, ModularAssert
# ---------------------------------------------------------------------------

class TestRemainingBuiltins:
    def test_offset_r_expands(self):
        # round offset of unit square by 1 should have area > 1
        bodies, _ = run("offset(r=1) square([2,2]);")
        assert bodies[0].section is not None
        assert bodies[0].section.area() > 4.0

    def test_offset_negative_shrinks(self):
        bodies, _ = run("offset(r=-0.5) square([4,4]);")
        assert bodies[0].section.area() < 16.0

    def test_offset_delta_square_corners(self):
        bodies, _ = run("offset(delta=1) square([2,2]);")
        assert bodies[0].section.area() > 4.0

    def test_projection_cut_false(self):
        # project a cube → roughly square cross section
        bodies, _ = run("projection() cube([3,4,5]);")
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 12.0) < 0.1

    def test_projection_cut_true(self):
        # cut at z=0 through a cube starting at z=-1 → cross section at z=0
        bodies, _ = run("projection(cut=true) translate([0,0,-1]) cube([3,4,2]);")
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 12.0) < 0.1

    def test_intersection_for(self):
        # intersection of three rotated cubes → rounded shape with less volume than a single cube
        src = "intersection_for(i=[0:2]) rotate([0,0,i*60]) cube([10,2,10], center=true);"
        bodies, _ = run(src)
        assert len(bodies) == 1
        assert 0 < bodies[0].body.volume() < 200

    def test_lookup_interpolates(self):
        _, lines = run("echo(lookup(0.5, [[0,0],[1,10]]));")
        assert lines == ["ECHO: 5"]

    def test_lookup_clamps_low(self):
        _, lines = run("echo(lookup(-1, [[0,0],[1,10]]));")
        assert lines == ["ECHO: 0"]

    def test_lookup_clamps_high(self):
        _, lines = run("echo(lookup(5, [[0,0],[1,10]]));")
        assert lines == ["ECHO: 10"]

    def test_lookup_empty_table_is_undef(self):
        _, lines = run("echo(lookup(5, []));")
        assert lines == ["ECHO: undef"]

    def test_children_count(self):
        src = "module m() { echo($children); } m() { cube(1); sphere(1); }"
        _, lines = run(src)
        assert lines == ["ECHO: 2"]

    def test_children_count_zero(self):
        src = "module m() { echo($children); } m();"
        _, lines = run(src)
        assert lines == ["ECHO: 0"]

    def test_children_count_counts_statements_not_geometries(self):
        # $children counts child *statements* in {}, regardless of how many
        # geometries each one produces — an `if` that yields nothing (or a
        # `children()` forwarding zero bodies) still counts as one child.
        src = "module m() { echo($children); } m() { cube(1); if (false) sphere(1); }"
        _, lines = run(src)
        assert lines == ["ECHO: 2"]

    def test_children_call_counts_even_with_no_bodies_to_forward(self):
        # `children()` is itself one child statement in the calling block,
        # even if the *caller's* own children (forwarded here) is empty.
        src = (
            "module inner() { echo($children); }"
            " module outer() { inner() { cube(1); children(); } }"
            " outer();"
        )
        _, lines = run(src)
        assert lines == ["ECHO: 2"]

    def test_modular_assert_passes(self):
        # assert with true condition — children's geometry passes through
        bodies, _ = run("assert(true) cube(1);")
        assert len(bodies) == 1
        assert abs(bodies[0].body.volume() - 1.0) < 0.01

    def test_modular_assert_fails(self):
        import pytest
        with pytest.raises(Exception, match="Assertion 'false' failed"):
            run("assert(false, \"bad\") cube(1);")

    def test_render_passthrough(self):
        # render() is a display hint — just passes through children
        bodies, _ = run("render() cube(2);")
        assert len(bodies) == 1
        assert abs(bodies[0].body.volume() - 8.0) < 0.01


# ---------------------------------------------------------------------------
# 2D CSG: union, difference, intersection on CrossSection children
# ---------------------------------------------------------------------------

class Test2DCSG:
    def test_2d_union(self):
        bodies, _ = run("union() { square([3,1]); square([1,3]); }")
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 5.0) < 0.01  # 3+3-1 overlap

    def test_2d_difference(self):
        bodies, _ = run("difference() { square([4,4]); square([2,2]); }")
        assert bodies[0].section is not None
        assert abs(bodies[0].section.area() - 12.0) < 0.01  # 16-4

    def test_2d_intersection(self):
        bodies, _ = run("intersection() { square([3,3]); circle(r=2, $fn=64); }")
        assert bodies[0].section is not None
        # intersection of 3x3 square and r=2 circle (area ~12.57) — circle wins in corners
        import math
        assert bodies[0].section.area() < math.pi * 4  # less than full circle

    def test_2d_difference_with_circle(self):
        # square with circle punched out
        bodies, _ = run("difference() { square([4,4], center=true); circle(r=1, $fn=64); }")
        import math
        expected = 16.0 - math.pi
        assert abs(bodies[0].section.area() - expected) / expected < 0.01

    def test_2d_csg_then_extrude(self):
        # 2D boolean then extrude to 3D
        src = "linear_extrude(height=5) difference() { square([4,4]); circle(r=1, $fn=32); }"
        bodies, _ = run(src)
        assert bodies[0].body is not None
        assert bodies[0].body.volume() > 0


# ---------------------------------------------------------------------------
# Error call chain — module errors
# ---------------------------------------------------------------------------

class TestModuleErrorCallChain:
    def test_module_appears_in_chain(self):
        src = """
        module bad() { assert(false, "boom"); }
        bad();
        """
        with pytest.raises(EvalError) as exc_info:
            run(src)
        msg = str(exc_info.value)
        assert "Assertion 'false' failed" in msg
        assert "called by 'bad'" in msg

    def test_nested_modules_in_chain(self):
        src = """
        module inner() { assert(false, "boom"); }
        module outer() { inner(); }
        outer();
        """
        with pytest.raises(EvalError) as exc_info:
            run(src)
        msg = str(exc_info.value)
        assert "called by 'inner'" in msg
        assert "called by 'outer'" in msg

    def test_function_inside_module_in_chain(self):
        src = """
        function bad() = assert(false, "boom") 1;
        module m() { echo(bad()); }
        m();
        """
        with pytest.raises(EvalError) as exc_info:
            run(src)
        msg = str(exc_info.value)
        assert "called by 'bad'" in msg
        assert "called by 'm'" in msg


# ---------------------------------------------------------------------------
# Recursive user modules
# ---------------------------------------------------------------------------

class TestRecursiveModule:
    def test_recursive_module_echo(self):
        src = """
        module countdown(n) {
            if (n > 0) {
                echo(n);
                countdown(n - 1);
            }
        }
        countdown(3);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 3", "ECHO: 2", "ECHO: 1"]

    def test_recursive_module_geometry(self):
        src = """
        module stack(n, h=1) {
            cube([1, 1, h]);
            if (n > 1) { translate([0, 0, h]) stack(n - 1, h); }
        }
        stack(3);
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[5] == approx(3.0)  # max z = 3


# ---------------------------------------------------------------------------
# Scoping: last-wins and hoisting
# ---------------------------------------------------------------------------

class TestScoping:
    def test_last_wins_in_block(self):
        _, lines = run("x = 1; x = 7; echo(x);")
        assert any("WARNING" in l and "x" in l and "overwritten" in l for l in lines)
        assert "ECHO: 7" in lines

    def test_param_self_reference_default_does_not_recurse(self):
        # A parameter with no default, re-assigned via a self-referential
        # expression in the body (the BOSL2 `chamfer = approx(chamfer,0) ?
        # undef : chamfer;` pattern) must resolve to its own (undef) param
        # value on the RHS, not recurse into the body's own assignment.
        src = "module m(x) { x = is_undef(x) ? 5 : x; echo(x); } m();"
        _, lines = run(src)
        assert lines == ["ECHO: 5"]

    def test_param_shadow_reassignment_no_warning(self):
        # A body assignment that shadows a parameter name with the same
        # value-normalization pattern must NOT emit a spurious "was assigned
        # ... but was overwritten" warning — only real double-assignments do.
        src = "module m(x) { x = is_undef(x) ? 5 : x; echo(x); } m();"
        _, lines = run(src)
        assert lines == ["ECHO: 5"]
        assert not any("WARNING" in l for l in lines)

    def test_forward_reference_function(self):
        src = "echo(double(5)); function double(x) = x * 2;"
        _, lines = run(src)
        assert lines == ["ECHO: 10"]

    def test_forward_reference_module(self):
        src = "box(3); module box(s) { cube(s); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)

    def test_module_scope_isolates_variable(self):
        src = """
        x = 10;
        module m() { x = 20; echo(x); }
        m();
        echo(x);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 20", "ECHO: 10"]

    def test_nested_module_closes_over_reassigned_outer_local(self):
        # A module nested inside another module's body is a closure over
        # the enclosing call's locals (BOSL2's `cuboid()` defines a nested
        # `module corner_shape()` that reads cuboid's local `edges`, which
        # cuboid reassigns from its own parameter via `edges =
        # _edges(edges, ...)` before calling corner_shape). The inner
        # module must see the REASSIGNED value, not recurse forever trying
        # to resolve the outer assignment's own right-hand side.
        src = """
        module outer(edges=[1,2,3]) {
            edges = [edges[0]+1, edges[1]+1, edges[2]+1];
            module inner() {
                echo(edges);
            }
            inner();
        }
        outer();
        """
        _, lines = run(src)
        assert lines == ["ECHO: [2, 3, 4]"]


# ---------------------------------------------------------------------------
# Hull 2D
# ---------------------------------------------------------------------------

class TestHull2D:
    def test_hull_two_circles_yields_section(self):
        src = "hull() { circle(r=1, $fn=32); translate([4,0]) circle(r=1, $fn=32); }"
        bodies, _ = run(src)
        assert bodies[0].section is not None
        assert bodies[0].body is None

    def test_hull_2d_larger_than_parts(self):
        import math
        # Hull of two unit circles separated by 5 units, extruded to measure area
        src = "linear_extrude(1) hull() { circle(r=1, $fn=64); translate([5,0]) circle(r=1, $fn=64); }"
        bodies, _ = run(src)
        vol = bodies[0].body.volume()
        # Two separate circles would give ~2*pi ≈ 6.28; hull is strictly larger
        assert vol > 2 * math.pi * 0.95


# ---------------------------------------------------------------------------
# str() and concat() edge cases
# ---------------------------------------------------------------------------

class TestStrEdgeCases:
    def test_str_bool_true(self):
        _, lines = run('echo(str(true));')
        assert lines == ['ECHO: "true"']

    def test_str_bool_false(self):
        _, lines = run('echo(str(false));')
        assert lines == ['ECHO: "false"']

    def test_str_undef(self):
        _, lines = run('echo(str(undef));')
        assert lines == ['ECHO: "undef"']

    def test_str_list(self):
        _, lines = run('echo(str([1, 2, 3]));')
        assert lines == ['ECHO: "[1, 2, 3]"']

    def test_str_multi_arg_concatenates(self):
        _, lines = run('echo(str(1, "+", 2, "=", 3));')
        assert lines == ['ECHO: "1+2=3"']

    def test_concat_two_lists(self):
        _, lines = run('echo(concat([1, 2], [3, 4]));')
        assert lines == ["ECHO: [1, 2, 3, 4]"]

    def test_concat_list_and_scalar(self):
        _, lines = run('echo(concat([1, 2], 3));')
        assert lines == ["ECHO: [1, 2, 3]"]

    def test_concat_three_lists(self):
        _, lines = run('echo(concat([1], [2], [3]));')
        assert lines == ["ECHO: [1, 2, 3]"]


class TestNumberFormatting:
    """`echo()`/`str()` number formatting must match OpenSCAD's output:
    6 significant digits, exponents without a leading zero, and fixed
    notation for exponents in [-5, 5] (one wider than Python's `%g`)."""

    def test_large_exponent_no_leading_zero(self):
        _, lines = run("echo(1000000);")
        assert lines == ["ECHO: 1e+6"]

    def test_small_number_stays_fixed_notation(self):
        _, lines = run("echo(0.00001);")
        assert lines == ["ECHO: 0.00001"]

    def test_small_exponent_no_leading_zero(self):
        _, lines = run("echo(1.23456789e-7);")
        assert lines == ["ECHO: 1.23457e-7"]

    def test_negative_zero(self):
        _, lines = run("echo(-0.0);")
        assert lines == ["ECHO: 0"]


# ---------------------------------------------------------------------------
# Special variables and stub built-ins
# ---------------------------------------------------------------------------

class TestSpecialVariables:
    def test_fa_default(self):
        _, lines = run('echo($fa);')
        assert lines == ["ECHO: 12"]

    def test_fs_default(self):
        _, lines = run('echo($fs);')
        assert lines == ["ECHO: 2"]

    def test_fn_default(self):
        _, lines = run('echo($fn);')
        assert lines == ["ECHO: 0"]

    def test_fn_override_via_named_arg(self):
        # $fn set as named arg on a built-in should not crash
        bodies, _ = run("sphere(r=1, $fn=8);")
        assert bodies[0].body.volume() > 0

    def test_version_returns_list(self):
        _, lines = run('echo(is_list(version()));')
        assert lines == ["ECHO: true"]

    def test_version_num_returns_number(self):
        _, lines = run('echo(is_num(version_num()));')
        assert lines == ["ECHO: true"]

    def test_parent_module_at_toplevel(self):
        # At top level, parent_module() returns undef (no parent)
        _, lines = run('echo(is_undef(parent_module()));')
        assert lines == ["ECHO: true"]


# ---------------------------------------------------------------------------
# breakpoint()
# ---------------------------------------------------------------------------

def _run_with_hook(src: str):
    """Run src with a debug hook attached. Returns (paused_lines, echo_lines)."""
    echo_lines = []
    paused_lines = []

    def hook(line, locals_dict, call_stack, all_frame_locals, forced=False, expr_level=False, expr_depth=0):
        if forced:
            paused_lines.append(line)
        return ("continue", {})

    nodes = getASTfromString(src, include_comments=False)
    root_scope = build_scopes(nodes)
    ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg), debug_hook=hook)
    ev.evaluate(nodes, root_scope)
    return paused_lines, echo_lines


class TestBreakpoint:
    def test_unconditional_pauses_in_debug_mode(self):
        paused, _ = _run_with_hook("breakpoint();")
        assert len(paused) == 1

    def test_unconditional_noop_without_hook(self):
        # No exception and no side effects when no debugger is attached.
        bodies, lines = run("breakpoint(); cube(1);")
        assert lines == []
        assert len(bodies) == 1

    def test_true_condition_pauses(self):
        paused, _ = _run_with_hook("breakpoint(true);")
        assert len(paused) == 1

    def test_false_condition_skips(self):
        paused, _ = _run_with_hook("breakpoint(false);")
        assert paused == []

    def test_zero_condition_skips(self):
        paused, _ = _run_with_hook("breakpoint(0);")
        assert paused == []

    def test_nonzero_condition_pauses(self):
        paused, _ = _run_with_hook("breakpoint(1);")
        assert len(paused) == 1

    def test_named_condition_arg(self):
        paused, _ = _run_with_hook("breakpoint(condition=true);")
        assert len(paused) == 1

    def test_named_condition_false_skips(self):
        paused, _ = _run_with_hook("breakpoint(condition=false);")
        assert paused == []

    def test_variable_condition(self):
        paused, _ = _run_with_hook("x = 5; breakpoint(x > 3);")
        assert len(paused) == 1

    def test_variable_condition_false(self):
        paused, _ = _run_with_hook("x = 2; breakpoint(x > 3);")
        assert paused == []

    def test_pauses_at_correct_line(self):
        src = "cube(1);\nbreakpoint();\ncube(2);"
        paused, _ = _run_with_hook(src)
        assert paused == [2]

    def test_multiple_breakpoints(self):
        src = "breakpoint();\nbreakpoint();"
        paused, _ = _run_with_hook(src)
        assert len(paused) == 2

    def test_produces_no_geometry(self):
        bodies, _ = run("breakpoint();")
        assert bodies == []

    def test_does_not_interfere_with_geometry(self):
        paused, _ = _run_with_hook("cube(1); breakpoint(); sphere(1);")
        assert len(paused) == 1

    def test_breakpoint_inside_module(self):
        src = "module foo() { breakpoint(); } foo();"
        paused, _ = _run_with_hook(src)
        assert len(paused) == 1

    def test_conditional_breakpoint_inside_loop(self):
        # Breaks only on iterations where i >= 3 (i = 3, 4 → 2 breaks)
        src = "for (i = [0:4]) { breakpoint(i >= 3); }"
        paused, _ = _run_with_hook(src)
        assert len(paused) == 2


# ---------------------------------------------------------------------------
# object()
# ---------------------------------------------------------------------------

class TestObject:
    def test_basic_creation_and_access(self):
        src = 'o = object(a=1, b="hello", c=[1,2,3]); echo(o.a, o.b, o.c, o["a"]);'
        _, echoes = run(src)
        assert echoes == ['ECHO: 1, "hello", [1, 2, 3], 1']

    def test_nested_object(self):
        src = "o = object(a=1, nested=object(x=10, y=20)); echo(o.nested.x); echo(o.nested);"
        _, echoes = run(src)
        assert echoes == ["ECHO: 10", "ECHO: { x = 10; y = 20; }"]

    def test_empty_object_echo(self):
        _, echoes = run("echo(object());")
        assert echoes == ["ECHO: { }"]

    def test_missing_key_is_undef(self):
        src = 'o = object(a=1); echo(o.nope); echo(o["nope"]); echo(o[0]);'
        _, echoes = run(src)
        assert echoes == ["ECHO: undef", "ECHO: undef", "ECHO: undef"]

    def test_type_predicates(self):
        src = (
            "o = object(a=1);"
            "echo(is_object(o), is_list(o), is_string(o), is_num(o), is_undef(o), is_object(5));"
        )
        _, echoes = run(src)
        assert echoes == ["ECHO: true, false, false, false, false, false"]

    def test_len(self):
        _, echoes = run("echo(len(object(a=1, b=2, c=3)));")
        assert echoes == ["ECHO: 3"]

    def test_equality_is_deep_and_order_sensitive(self):
        src = (
            "echo(object(a=1,b=2) == object(a=1,b=2));"
            "echo(object(a=1,b=2) == object(b=2,a=1));"
            "echo(object(a=1,b=2) != object(b=2,a=1));"
        )
        _, echoes = run(src)
        assert echoes == ["ECHO: true", "ECHO: false", "ECHO: true"]

    def test_str_formatting(self):
        src = 'echo(str(object(a=1, nested=object(x=10, y=20))));'
        _, echoes = run(src)
        assert echoes == ['ECHO: "{ a = 1; nested = { x = 10; y = 20; }; }"']

    def test_for_iterates_over_keys(self):
        src = "for (k = object(z=1, a=2, m=3)) echo(k);"
        _, echoes = run(src)
        assert echoes == ['ECHO: "z"', 'ECHO: "a"', 'ECHO: "m"']

    def test_function_valued_member_is_callable(self):
        src = "f = object(fn=function(x) x*2); echo(f.fn(5));"
        _, echoes = run(src)
        assert echoes == ["ECHO: 10"]

    def test_merge_via_positional_object(self):
        src = (
            "o1 = object(a=1, b=2);"
            "echo(object(o1, c=3));"
            "echo(object(o1, b=99, c=3));"
        )
        _, echoes = run(src)
        assert echoes == [
            "ECHO: { a = 1; b = 2; c = 3; }",
            "ECHO: { a = 1; b = 99; c = 3; }",
        ]

    def test_merge_via_positional_list_of_pairs(self):
        _, echoes = run('echo(object([["x",10],["y",20]]));')
        assert echoes == ["ECHO: { x = 10; y = 20; }"]

    def test_invalid_positional_arg_warns_and_is_undef(self):
        _, echoes = run("echo(object(1,2));")
        assert len(echoes) == 2
        assert "WARNING: object(Argument 0 <number>) An unnamed argument must be either <object> or <list>, it is <number>." in echoes[0]
        assert echoes[1] == "ECHO: undef"

    def test_addition_on_objects_is_undef(self):
        _, echoes = run("echo(object(a=1) + object(b=2));")
        assert echoes == ["ECHO: undef"]


class TestTextMetrics:
    """`textmetrics()`/`fontmetrics()` measure against the bundled Liberation
    Sans font (see docs/evaluator.md). Values are close to, but not bit-for-bit
    identical to, real OpenSCAD (which applies FreeType hinting we don't
    replicate) — expected strings below are this implementation's own output."""

    def test_basic_left_baseline(self):
        _, echoes = run('echo(textmetrics(text="Hello", size=10));')
        assert echoes == [
            "ECHO: { position = [1.13932, -0.135634]; size = [29.9276, 10.1997]; "
            "ascent = 10.064; descent = -0.135634; offset = [0, 0]; advance = [31.6501, 0]; }"
        ]

    def test_size_scales_linearly(self):
        _, echoes = run('echo(textmetrics(text="Hello", size=20));')
        assert echoes == [
            "ECHO: { position = [2.27865, -0.271267]; size = [59.8551, 20.3993]; "
            "ascent = 20.128; descent = -0.271267; offset = [0, 0]; advance = [63.3002, 0]; }"
        ]

    def test_single_char_no_descender(self):
        _, echoes = run('echo(textmetrics(text="A", size=10));')
        assert echoes == [
            "ECHO: { position = [0.0271267, 0]; size = [9.20953, 9.55539]; "
            "ascent = 9.55539; descent = 0; offset = [0, 0]; advance = [9.26378, 0]; }"
        ]

    def test_empty_text_is_all_zero(self):
        _, echoes = run('echo(textmetrics(text="", size=10));')
        assert echoes == [
            "ECHO: { position = [0, 0]; size = [0, 0]; ascent = 0; descent = 0; "
            "offset = [0, 0]; advance = [0, 0]; }"
        ]

    def test_halign_center_valign_center(self):
        _, echoes = run(
            'echo(textmetrics(text="Hello", size=10, halign="center", valign="center"));'
        )
        assert echoes == [
            "ECHO: { position = [-14.6857, -5.09983]; size = [29.9276, 10.1997]; "
            "ascent = 10.064; descent = -0.135634; offset = [-15.8251, -4.96419]; advance = [31.6501, 0]; }"
        ]

    def test_halign_right_valign_top(self):
        _, echoes = run(
            'echo(textmetrics(text="Hello", size=10, halign="right", valign="top"));'
        )
        assert echoes == [
            "ECHO: { position = [-30.5108, -10.1997]; size = [29.9276, 10.1997]; "
            "ascent = 10.064; descent = -0.135634; offset = [-31.6501, -10.064]; advance = [31.6501, 0]; }"
        ]

    def test_halign_left_valign_bottom(self):
        _, echoes = run(
            'echo(textmetrics(text="Hello", size=10, halign="left", valign="bottom"));'
        )
        assert echoes == [
            "ECHO: { position = [1.13932, 0]; size = [29.9276, 10.1997]; "
            "ascent = 10.064; descent = -0.135634; offset = [0, 0.135634]; advance = [31.6501, 0]; }"
        ]

    def test_spacing_scales_advance_and_size(self):
        _, echoes = run('echo(textmetrics(text="Hello", size=10, spacing=1.5));')
        assert echoes == [
            "ECHO: { position = [1.13932, -0.135634]; size = [41.8905, 10.1997]; "
            "ascent = 10.064; descent = -0.135634; offset = [0, 0]; advance = [47.4752, 0]; }"
        ]

        _, echoes = run('echo(textmetrics(text="Hello", size=10, spacing=2));')
        assert echoes == [
            "ECHO: { position = [1.13932, -0.135634]; size = [53.8534, 10.1997]; "
            "ascent = 10.064; descent = -0.135634; offset = [0, 0]; advance = [63.3002, 0]; }"
        ]

    def test_is_object_and_member_access(self):
        _, echoes = run('echo(is_object(textmetrics(text="Hi", size=10)));')
        assert echoes == ["ECHO: true"]

        _, echoes = run('m = textmetrics(text="Hello", size=10); echo(m.size, m["ascent"]);')
        assert echoes == ["ECHO: [29.9276, 10.1997], 10.064"]

    def test_fontmetrics_structure(self):
        _, echoes = run("echo(fontmetrics(size=10));")
        assert echoes == [
            "ECHO: { nominal = { ascent = 12.5732; descent = -2.94325; }; "
            "max = { ascent = 13.6108; descent = -4.21143; }; interline = 15.9709; "
            'font = { family = "Liberation Sans"; style = "Regular"; }; }'
        ]

    def test_fontmetrics_echoes_requested_font_family(self):
        _, echoes = run('echo(fontmetrics(size=10, font="Arial"));')
        assert echoes == [
            "ECHO: { nominal = { ascent = 12.5732; descent = -2.94325; }; "
            "max = { ascent = 13.6108; descent = -4.21143; }; interline = 15.9709; "
            'font = { family = "Arial"; style = "Regular"; }; }'
        ]


# ---------------------------------------------------------------------------
# text()
# ---------------------------------------------------------------------------

class TestText:
    """`text()` renders glyph outlines (from the same bundled Liberation Sans
    font as `textmetrics()`) as a 2D cross-section. Bbox values below come
    from `linear_extrude(height=1) text(...)` and were cross-checked against
    real OpenSCAD-dev output (see docs/evaluator.md)."""

    def test_single_char_left_baseline(self):
        bb = bbox(run('linear_extrude(height=1) text("A", size=10);')[0])
        assert bb[0] == approx(0.0271267, rel=1e-3)
        assert bb[1] == pytest.approx(0.0, abs=1e-3)
        assert bb[3] == approx(9.23665, rel=1e-3)
        assert bb[4] == approx(9.55539, rel=1e-3)

    def test_word_left_baseline(self):
        bb = bbox(run('linear_extrude(height=1) text("Hello", size=10);')[0])
        assert bb[0] == approx(1.13932, rel=1e-3)
        assert bb[1] == approx(-0.135634, rel=1e-2)
        assert bb[3] == approx(31.0669, rel=1e-3)
        assert bb[4] == approx(10.064, rel=1e-3)

    def test_halign_center_valign_center(self):
        bb = bbox(run(
            'linear_extrude(height=1) text("Hello", size=10, halign="center", valign="center");'
        )[0])
        assert bb[0] == approx(-14.6857, rel=1e-3)
        assert bb[1] == approx(-5.09983, rel=1e-3)
        assert bb[3] == approx(15.2418, rel=1e-3)
        assert bb[4] == approx(5.09983, rel=1e-3)

    def test_empty_text_produces_empty_geometry(self):
        bodies, _ = run('linear_extrude(height=1) text("");')
        assert bodies
        assert bodies[0].body.volume() == 0

    def test_composite_glyph_renders(self):
        bb = bbox(run('linear_extrude(height=1) text("é", size=10);')[0])
        area = (bb[3] - bb[0]) * (bb[4] - bb[1])
        assert area > 0

    def test_spacing_increases_extent(self):
        bb1 = bbox(run('linear_extrude(height=1) text("AA", size=10, spacing=1);')[0])
        bb2 = bbox(run('linear_extrude(height=1) text("AA", size=10, spacing=2);')[0])
        assert bb2[3] > bb1[3]
