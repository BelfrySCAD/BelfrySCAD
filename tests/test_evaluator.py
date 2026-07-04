"""
Tests for the BelfrySCAD evaluator.

Each test calls run(src) which parses, scopes, and evaluates OpenSCAD source,
returning (bodies, echo_lines). Geometry tests inspect bounding boxes;
expression tests capture echo output.
"""
import pytest
from openscad_lalr_parser import getASTfromString, build_scopes

from belfryscad.engine.evaluator import (
    Evaluator, EvalContext, EvalError, _resolve_font, CSGNode, flatten_csg_tree,
)


def run(src: str):
    """Parse, scope, and evaluate src. Returns (bodies, echo_lines)."""
    echo_lines = []
    nodes = getASTfromString(src, include_comments=False)
    root_scope = build_scopes(nodes)
    ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg))
    bodies, _ = ev.evaluate(nodes, root_scope)
    return bodies, echo_lines


def run_tree(src: str):
    """Like run(), but also returns the Evaluator so tests can inspect
    its csg_tree. Returns (bodies, echo_lines, evaluator)."""
    echo_lines = []
    nodes = getASTfromString(src, include_comments=False)
    root_scope = build_scopes(nodes)
    ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg))
    bodies, _ = ev.evaluate(nodes, root_scope)
    return bodies, echo_lines, ev


def skip_unless_font_installed(font_spec: str, expected_family: str):
    """Skip a test if `font_spec` doesn't resolve (via fc-match) to a font
    actually named `expected_family` on this machine. Arial/Times New
    Roman/STIXGeneral aren't installed on every system (e.g. CI runners) —
    fc-match then silently substitutes a metric-compatible fallback
    (Liberation Sans/Serif), which has different real glyph metrics than
    the tests' hardcoded expected values. Skip rather than assert against
    whatever substitute happens to be installed."""
    resolved = _resolve_font(font_spec)
    family = resolved["family_name"]
    if family.lower() != expected_family.lower():
        pytest.skip(
            f"{expected_family!r} not installed on this system "
            f"(fc-match resolved {font_spec!r} to {family!r} instead)"
        )


def bbox(bodies):
    """Return (xmin,ymin,zmin,xmax,ymax,zmax) union over all non-empty manifold bodies."""
    assert bodies, "no geometry produced"
    bbs = [b.body.bounding_box() for b in bodies if b.body is not None]
    assert bbs, "no 3D geometry produced"
    return (
        min(bb[0] for bb in bbs), min(bb[1] for bb in bbs), min(bb[2] for bb in bbs),
        max(bb[3] for bb in bbs), max(bb[4] for bb in bbs), max(bb[5] for bb in bbs),
    )


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

    def test_for_string_iterates_chars(self):
        _, lines = run('for (c = "abc") { echo(c); }')
        assert lines == ['ECHO: "a"', 'ECHO: "b"', 'ECHO: "c"']

    def test_for_string_variable_iterates_chars(self):
        _, lines = run('s = "hi"; for (c = s) { echo(c); }')
        assert lines == ['ECHO: "h"', 'ECHO: "i"']


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

    def test_for_comp_string_iterates_chars(self):
        _, lines = run('echo([for (c = "xyz") c]);')
        assert lines == ['ECHO: ["x", "y", "z"]']

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
        # # (highlight) produces geometry with role="highlight"
        bodies, _ = run("#cube(2);")
        assert len(bodies) == 1
        assert bodies[0].role == "highlight"
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(2)

    def test_showonly_produces_geometry(self):
        # ! (show-only) filters other geometry; produces role="show_only" body
        bodies, _ = run("!cube(3);")
        assert len(bodies) == 1
        assert bodies[0].role == "show_only"
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(3)

    def test_showonly_filters_others(self):
        # ! filters out normal geometry, keeping only show_only bodies
        bodies, _ = run("cube(1); !cube(3);")
        assert len(bodies) == 1
        assert bodies[0].role == "show_only"

    def test_background_role(self):
        # % (background) produces a ghost body tagged role="background"
        bodies, _ = run("%cube(1);")
        assert len(bodies) == 1
        assert bodies[0].role == "background"

    def test_disable_suppressed(self):
        # * (disable) produces no geometry
        bodies, _ = run("*cube(1);")
        assert bodies == []

    def test_background_with_other_geometry(self):
        # % cube is kept (role="background") alongside normal geometry
        src = "cube(1); %cube([10,10,10]);"
        bodies, _ = run(src)
        assert len(bodies) == 2
        normal = [b for b in bodies if b.role == "normal"]
        bg = [b for b in bodies if b.role == "background"]
        assert len(normal) == 1
        assert len(bg) == 1
        bb = normal[0].body.bounding_box()
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

    def test_difference_first_stmt_multiple_bodies_unioned(self):
        # When the first child statement of difference() produces multiple bodies
        # (e.g., a module that emits two shapes), they must be unioned as the
        # positive operand — not treated as sequential subtractors.
        # Two non-overlapping cubes at z=+10 and z=-10; only a tiny cube at
        # the origin is actually subtracted (no overlap with either cube).
        src = """
        module pair() {
            translate([0, 0,  10]) cube([4, 4, 4], center=true);
            translate([0, 0, -10]) cube([4, 4, 4], center=true);
        }
        difference() {
            pair();               // statement 0: TWO bodies
            cube(1, center=true); // statement 1: subtractor at origin
        }
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        # Both cubes must survive: z ranges [8,12] and [-12,-8].
        assert bb[2] == approx(-12)
        assert bb[5] == approx(12)

    def test_union_first_stmt_multiple_bodies(self):
        # union() produces the same result regardless of body grouping, but
        # verify that a multi-body first statement still contributes all bodies.
        src = """
        module pair() {
            translate([0, 0,  5]) cube([2, 2, 2], center=true);
            translate([0, 0, -5]) cube([2, 2, 2], center=true);
        }
        union() { pair(); }
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[2] == approx(-6)
        assert bb[5] == approx(6)

    def test_intersection_empty_first_operand_gives_empty(self):
        # intersection(∅, B) = ∅. When the first child statement of intersection()
        # produces no geometry (disabled with *), the clip body from the second
        # statement must NOT escape as the result.
        src = "intersection() { *cube(10); cube(5); }"
        bodies, _ = run(src)
        assert bodies == []

    def test_difference_empty_first_operand_gives_empty(self):
        # difference(∅, B) = ∅. If the positive operand of difference() is empty,
        # the subtractor must not become the result.
        src = "difference() { *cube(10); cube(5); }"
        bodies, _ = run(src)
        assert bodies == []

    def test_intersection_empty_second_operand_gives_empty(self):
        # intersection(A, ∅) = ∅. If any operand is empty, result must be empty.
        src = "intersection() { cube(5); *cube(10); }"
        bodies, _ = run(src)
        assert bodies == []

    def test_difference_empty_subtractor_leaves_base(self):
        # difference(A, ∅) = A. An empty subtractor is a no-op.
        src = "difference() { cube(4); *cube(10); }"
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(4)


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

    def test_listcomp_for_undef_body(self):
        # for body that evaluates to undef → undef is a valid element, not dropped
        _, lines = run("echo([for (i=[1:2]) undef]);")
        assert lines == ["ECHO: [undef, undef]"]

    def test_listcomp_for_undef_body_via_var(self):
        # same via a variable: mirrors test16.scad force_list(undef, 2)
        src = """
        function force_list(value, n=1, fill) =
            is_list(value) ? value :
            is_undef(fill)
              ? [for (i=[1:1:n]) value]
              : [value, for (i=[2:1:n]) fill];
        echo(force_list(undef, 2));
        """
        _, lines = run(src)
        assert lines == ["ECHO: [undef, undef]"]


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

    def test_polygon_cw_winding_fills(self):
        # polygon() must fill regardless of winding direction (OpenSCAD uses EvenOdd).
        # CW triangle: same area as CCW triangle [[0,0],[1,0],[0,1]].
        bodies, _ = run("polygon([[0,0],[0,1],[1,0]]);")
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

    def test_roof_polygon_with_hole(self):
        # A 10x10 square with a 6x6 square hole (frame width = 2). Tier 2
        # handles this exactly via skeletonize() with holes. The max ridge
        # height equals the half-width of the frame = 1.0, and the volume
        # of the roof over the frame is exactly 32.
        src = """
        roof() polygon(
            points=[[0,0],[10,0],[10,10],[0,10],[2,2],[2,8],[8,8],[8,2]],
            paths=[[0,1,2,3],[4,5,6,7]]
        );
        """
        bodies, _ = run(src)
        assert len(bodies) == 1
        b = bodies[0].body
        import manifold3d as m3d
        assert b.status() == m3d.Error.NoError
        bb = b.bounding_box()
        assert bb[2] == 0.0
        assert abs(bb[5] - 1.0) < 1e-3
        assert abs(b.volume() - 32.0) < 0.1

    def test_roof_text_with_holes(self):
        # Glyphs that have counter-holes (like "a" and "g") must be roofed
        # using the hole-aware skeleton path. Verify they produce valid,
        # non-empty geometry.
        bodies, _ = run('roof() text("ag", size=72);')
        assert len(bodies) == 1
        b = bodies[0].body
        import manifold3d as m3d
        assert b.status() == m3d.Error.NoError
        assert not b.is_empty()
        assert b.volume() > 0

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

    def hook(line, depth, *, forced=False, expr_level=False, expr_depth=0, origin=None, get_frames=None):
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
        assert echoes == ["ECHO: 10", "ECHO: object(x = 10, y = 20)"]

    def test_empty_object_echo(self):
        _, echoes = run("echo(object());")
        assert echoes == ["ECHO: object()"]

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
        assert echoes == ['ECHO: "object(a = 1, nested = object(x = 10, y = 20))"']

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
            "ECHO: object(a = 1, b = 2, c = 3)",
            "ECHO: object(a = 1, b = 99, c = 3)",
        ]

    def test_merge_via_positional_list_of_pairs(self):
        _, echoes = run('echo(object([["x",10],["y",20]]));')
        assert echoes == ["ECHO: object(x = 10, y = 20)"]

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
            "ECHO: object(position = [1.13932, -0.135634], size = [29.9276, 10.1997], "
            "ascent = 10.064, descent = -0.135634, offset = [0, 0], advance = [31.6501, 0])"
        ]

    def test_size_scales_linearly(self):
        _, echoes = run('echo(textmetrics(text="Hello", size=20));')
        assert echoes == [
            "ECHO: object(position = [2.27865, -0.271267], size = [59.8551, 20.3993], "
            "ascent = 20.128, descent = -0.271267, offset = [0, 0], advance = [63.3002, 0])"
        ]

    def test_single_char_no_descender(self):
        _, echoes = run('echo(textmetrics(text="A", size=10));')
        assert echoes == [
            "ECHO: object(position = [0.0271267, 0], size = [9.20953, 9.55539], "
            "ascent = 9.55539, descent = 0, offset = [0, 0], advance = [9.26378, 0])"
        ]

    def test_empty_text_is_all_zero(self):
        _, echoes = run('echo(textmetrics(text="", size=10));')
        assert echoes == [
            "ECHO: object(position = [0, 0], size = [0, 0], ascent = 0, descent = 0, "
            "offset = [0, 0], advance = [0, 0])"
        ]

    def test_halign_center_valign_center(self):
        _, echoes = run(
            'echo(textmetrics(text="Hello", size=10, halign="center", valign="center"));'
        )
        assert echoes == [
            "ECHO: object(position = [-14.6857, -5.09983], size = [29.9276, 10.1997], "
            "ascent = 10.064, descent = -0.135634, offset = [-15.8251, -4.96419], advance = [31.6501, 0])"
        ]

    def test_halign_right_valign_top(self):
        _, echoes = run(
            'echo(textmetrics(text="Hello", size=10, halign="right", valign="top"));'
        )
        assert echoes == [
            "ECHO: object(position = [-30.5108, -10.1997], size = [29.9276, 10.1997], "
            "ascent = 10.064, descent = -0.135634, offset = [-31.6501, -10.064], advance = [31.6501, 0])"
        ]

    def test_halign_left_valign_bottom(self):
        _, echoes = run(
            'echo(textmetrics(text="Hello", size=10, halign="left", valign="bottom"));'
        )
        assert echoes == [
            "ECHO: object(position = [1.13932, 0], size = [29.9276, 10.1997], "
            "ascent = 10.064, descent = -0.135634, offset = [0, 0.135634], advance = [31.6501, 0])"
        ]

    def test_spacing_scales_advance_and_size(self):
        _, echoes = run('echo(textmetrics(text="Hello", size=10, spacing=1.5));')
        assert echoes == [
            "ECHO: object(position = [1.13932, -0.135634], size = [41.8905, 10.1997], "
            "ascent = 10.064, descent = -0.135634, offset = [0, 0], advance = [47.4752, 0])"
        ]

        _, echoes = run('echo(textmetrics(text="Hello", size=10, spacing=2));')
        assert echoes == [
            "ECHO: object(position = [1.13932, -0.135634], size = [53.8534, 10.1997], "
            "ascent = 10.064, descent = -0.135634, offset = [0, 0], advance = [63.3002, 0])"
        ]

    def test_is_object_and_member_access(self):
        _, echoes = run('echo(is_object(textmetrics(text="Hi", size=10)));')
        assert echoes == ["ECHO: true"]

        _, echoes = run('m = textmetrics(text="Hello", size=10); echo(m.size, m["ascent"]);')
        assert echoes == ["ECHO: [29.9276, 10.1997], 10.064"]

    def test_fontmetrics_structure(self):
        _, echoes = run("echo(fontmetrics(size=10));")
        assert echoes == [
            "ECHO: object(nominal = object(ascent = 12.5732, descent = -2.94325), "
            "max = object(ascent = 13.6108, descent = -4.21143), interline = 15.9709, "
            'font = object(family = "Liberation Sans", style = "Regular"))'
        ]

    def test_fontmetrics_resolves_requested_font(self):
        # Arial is metric-compatible with Liberation Sans by design (same
        # hhea-derived nominal/interline), but "max" comes from the actual
        # glyph bbox extremes in the *resolved* font's head table, so it
        # differs — proving font= actually selects a different font rather
        # than just being echoed back into the family name. Skipped where
        # Arial isn't installed (e.g. CI) — see skip_unless_font_installed.
        skip_unless_font_installed("Arial", "Arial")
        _, echoes = run('echo(fontmetrics(size=10, font="Arial"));')
        assert echoes == [
            "ECHO: object(nominal = object(ascent = 12.5732, descent = -2.94325), "
            "max = object(ascent = 13.9703, descent = -4.50982), interline = 15.9709, "
            'font = object(family = "Arial", style = "Regular"))'
        ]

    def test_fontmetrics_reports_resolved_style(self):
        skip_unless_font_installed("Times New Roman:style=Bold", "Times New Roman")
        _, echoes = run('echo(fontmetrics(size=10, font="Times New Roman:style=Bold").font);')
        assert echoes == ['ECHO: object(family = "Times New Roman", style = "Bold")']

    def test_textmetrics_resolves_requested_font(self):
        # Times New Roman's serif proportions measure differently from the
        # default Liberation Sans for the same text/size.
        skip_unless_font_installed("Times New Roman", "Times New Roman")
        _, echoes = run('echo(textmetrics(text="Hello", size=10, font="Times New Roman").size, '
                         'textmetrics(text="Hello", size=10, font="Times New Roman")["ascent"]);')
        assert echoes == ["ECHO: [30.1378, 9.83344], 9.64355"]


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

    def test_cff_font_renders(self):
        # CFF/OTF glyphs use cubic Bezier curves (vs. TrueType's quadratic);
        # this exercises that flattening path via a system CFF font. Skipped
        # where STIXGeneral isn't installed (e.g. CI).
        skip_unless_font_installed("STIXGeneral:style=Bold Italic", "STIXGeneral")
        bb = bbox(run(
            'linear_extrude(height=1) text("Hi", size=10, font="STIXGeneral:style=Bold Italic");'
        )[0])
        assert bb[0] == approx(-0.333333, rel=1e-3)
        assert bb[1] == pytest.approx(-0.125, abs=1e-3)
        assert bb[3] == approx(14.4444, rel=1e-3)
        assert bb[4] == approx(9.5, rel=1e-3)

    def test_spacing_increases_extent(self):
        bb1 = bbox(run('linear_extrude(height=1) text("AA", size=10, spacing=1);')[0])
        bb2 = bbox(run('linear_extrude(height=1) text("AA", size=10, spacing=2);')[0])
        assert bb2[3] > bb1[3]


# ---------------------------------------------------------------------------
# $-variable dynamic scoping into children()
# ---------------------------------------------------------------------------

class TestDollarVarChildren:
    def test_dollar_var_visible_to_children(self):
        src = """
        module m() { $x = 42; children(); }
        m() echo($x);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 42"]

    def test_dollar_var_from_for_loop_visible_to_children(self):
        src = """
        module xcopies(spacing, n=2) {
            for ($idx = [0:1:n-1]) {
                translate([($idx - n/2 + 0.5) * spacing, 0, 0])
                    children();
            }
        }
        xcopies(10, n=3) sphere(d=$idx+1);
        """
        bodies, lines = run(src)
        assert len(bodies) == 3
        widths = sorted(
            b.body.bounding_box()[3] - b.body.bounding_box()[0] for b in bodies
        )
        assert widths[0] < widths[1] < widths[2]

    def test_dollar_var_assignment_in_for_body_visible_to_children(self):
        src = """
        module m() {
            for (i = [0:2]) {
                $val = i * 10;
                children();
            }
        }
        m() echo($val);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 0", "ECHO: 10", "ECHO: 20"]

    def test_dollar_var_overrides_in_nested_module(self):
        src = """
        module outer() { $x = 1; children(); }
        module inner() { $x = 2; children(); }
        outer() inner() echo($x);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 2"]

    def test_dollar_var_from_caller_visible_without_override(self):
        src = """
        module m() { children(); }
        $x = 99;
        m() echo($x);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 99"]

    def test_children_in_for_loop_produces_multiple_bodies(self):
        src = """
        module triple() {
            for (i = [0:2]) {
                translate([i * 10, 0, 0]) children();
            }
        }
        triple() cube(1);
        """
        bodies, _ = run(src)
        assert len(bodies) == 3

    def test_children_in_let_block_preserves_dollar_vars(self):
        src = """
        module m() {
            $v = 5;
            let (x = 1) { children(); }
        }
        m() echo($v);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 5"]

    def test_three_part_range_in_for(self):
        src = """
        vals = [];
        for (i = [0:2:6]) echo(i);
        """
        _, lines = run(src)
        assert lines == ["ECHO: 0", "ECHO: 2", "ECHO: 4", "ECHO: 6"]

    def test_two_part_range_in_for(self):
        src = "for (i = [0:3]) echo(i);"
        _, lines = run(src)
        assert lines == ["ECHO: 0", "ECHO: 1", "ECHO: 2", "ECHO: 3"]

    def test_children_indexed_with_dollar_var(self):
        src = """
        module m() { $x = 10; children(0); }
        m() { cube($x); cube(1); }
        """
        bodies, _ = run(src)
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(10)

    def test_children_n_indexes_by_statement_not_body(self):
        # children(N) must index by child STATEMENT, not by output body.
        # Statement 0 is disabled (*) so it produces 0 bodies; the body list
        # is therefore just [cube6_body].  The old code returned bodies[1]
        # (OOB → nothing).  The fix evaluates statement 1 directly → cube(6).
        src = """
        module pick_second() { children(1); }
        pick_second() {
            *cube(10);  // statement 0: disabled, 0 bodies
            cube(6);    // statement 1: must be returned by children(1)
        }
        """
        bodies, _ = run(src)
        assert bodies, "children(1) must return statement 1 even when statement 0 produces 0 bodies"
        bb = bbox(bodies)
        # cube(6) → side length 6; cube(10) would be 10
        assert bb[3] - bb[0] == approx(6)

    def test_children_n_correct_stmt_when_prior_stmt_is_empty(self):
        # With three statements where statement 0 produces 0 bodies, children(1)
        # must map to the small cube and children(2) to the large cube — not shifted.
        src = """
        module pick() {
            children(1);  // must be cube(2), not cube(5)
            children(2);  // must be cube(5)
        }
        pick() {
            *cube(1);  // statement 0: disabled, 0 bodies
            cube(2);   // statement 1
            cube(5);   // statement 2
        }
        """
        bodies, _ = run(src)
        assert len(bodies) == 2
        sides = sorted(
            b.body.bounding_box()[3] - b.body.bounding_box()[0] for b in bodies
        )
        assert sides[0] == approx(2)  # cube(2)
        assert sides[1] == approx(5)  # cube(5)


# ---------------------------------------------------------------------------
# CSG tree (Evaluator.csg_tree) — Phase 1 of the evaluator refactor: an
# explicit, persistent tree built as a side effect of eager evaluation,
# purely additive (bodies/echo output are unaffected). See docs/evaluator.md
# "CSG tree" section.
# ---------------------------------------------------------------------------

class TestCSGTree:
    def test_single_primitive_one_node(self):
        _, _, ev = run_tree("cube(2);")
        assert len(ev.csg_tree) == 1
        node = ev.csg_tree[0]
        assert node.kind == "cube"
        assert node.is_builtin is True
        assert node.children == []

    def test_union_nests_children(self):
        _, _, ev = run_tree("union() { cube(1); sphere(1); }")
        assert len(ev.csg_tree) == 1
        node = ev.csg_tree[0]
        assert node.kind == "union"
        assert [c.kind for c in node.children] == ["cube", "sphere"]

    def test_highlight_wraps_one_child(self):
        _, _, ev = run_tree("#cube(2);")
        assert len(ev.csg_tree) == 1
        assert ev.csg_tree[0].kind == "highlight"
        assert len(ev.csg_tree[0].children) == 1
        assert ev.csg_tree[0].children[0].kind == "cube"

    def test_background_wraps_one_child(self):
        _, _, ev = run_tree("%sphere(2);")
        assert len(ev.csg_tree) == 1
        assert ev.csg_tree[0].kind == "background"
        assert ev.csg_tree[0].children[0].kind == "sphere"

    def test_color_wraps_one_child(self):
        _, _, ev = run_tree('color("red") cube(2);')
        assert len(ev.csg_tree) == 1
        assert ev.csg_tree[0].kind == "color"
        assert len(ev.csg_tree[0].children) == 1
        assert ev.csg_tree[0].children[0].kind == "cube"

    def test_for_produces_sibling_nodes_no_for_node(self):
        _, _, ev = run_tree("for (i=[0:2]) cube(i+1);")
        # transparent: 3 sibling cube nodes directly at root, no "for" node
        assert [n.kind for n in ev.csg_tree] == ["cube", "cube", "cube"]

    def test_if_is_transparent(self):
        _, _, ev = run_tree("if (true) cube(1); if (false) sphere(1);")
        # true branch's cube attaches directly at root; false branch never runs
        assert [n.kind for n in ev.csg_tree] == ["cube"]

    def test_intersection_for_gets_its_own_combiner_node(self):
        src = "intersection_for(i=[0:2]) rotate([0,0,i*60]) cube([10,2,10], center=true);"
        bodies, _, ev = run_tree(src)
        assert len(ev.csg_tree) == 1
        node = ev.csg_tree[0]
        assert node.kind == "intersection_for"
        assert len(node.children) == 3          # one rotate(...) per iteration
        assert node.bodies == bodies             # the combined (post-^) result, not the 3 pre-intersection bodies
        assert len(node.bodies) == 1

    def test_user_module_call_is_not_builtin(self):
        _, _, ev = run_tree("module foo() { cube(1); } foo();")
        assert len(ev.csg_tree) == 1
        node = ev.csg_tree[0]
        assert node.kind == "foo"
        assert node.is_builtin is False
        assert len(node.children) == 1
        assert node.children[0].kind == "cube"

    def test_disable_produces_no_tree_node(self):
        _, _, ev = run_tree("*cube(1);")
        assert ev.csg_tree == []

    def test_nested_mix(self):
        src = """
        module box(s) { cube(s); }
        union() {
            #box(2);
            translate([5,0,0]) %sphere(1);
        }
        """
        _, _, ev = run_tree(src)
        assert len(ev.csg_tree) == 1
        union_node = ev.csg_tree[0]
        assert union_node.kind == "union"
        assert len(union_node.children) == 2
        hl, tr = union_node.children
        assert hl.kind == "highlight"
        assert hl.children[0].kind == "box" and hl.children[0].is_builtin is False
        assert tr.kind == "translate"
        assert tr.children[0].kind == "background"
        assert tr.children[0].children[0].kind == "sphere"

    @pytest.mark.parametrize("src", [
        "cube(2);",
        "union() { cube(1); sphere(1); }",
        "difference() { cube([4,4,4]); cube([2,2,2]); }",
        "for (i=[0:2]) cube(i+1);",
        "module box(s) { cube(s); } box(3);",
        'color("red") translate([1,0,0]) cube(1);',
        "%cube(1); cube(2);",
    ])
    def test_flatten_matches_evaluate_result(self, src):
        # Regression proof: flattening the tree reproduces evaluate()'s own
        # result for any script with no top-level `!` (show_only).
        bodies, _, ev = run_tree(src)
        assert flatten_csg_tree(ev.csg_tree) == bodies

    def test_flatten_vs_evaluate_with_top_level_show_only(self):
        # Documented exception: evaluate()'s own post-hoc show_only filter
        # (applied once, outside any single tree node) makes evaluate()'s
        # result a strict subset of the flattened (pre-filter) tree.
        src = "cube(1); !cube(3);"
        bodies, _, ev = run_tree(src)
        flat = flatten_csg_tree(ev.csg_tree)
        assert len(flat) == 2                        # both top-level statements recorded
        assert len(bodies) == 1                       # evaluate()'s post-filter result
        assert bodies[0].role == "show_only"
        filtered = [b for b in flat if b.role in ("show_only", "highlight")]
        assert filtered == bodies                     # replaying evaluate()'s own filter matches exactly

    def test_error_mid_subtree_leaves_valid_partial_tree(self):
        # An EvalError raised deep inside a subtree must not corrupt the
        # parent's accumulator — the in-progress node is simply never
        # appended, and prior completed siblings remain intact.
        src = 'cube(1); union() { sphere(1); assert(false, "boom"); }'
        nodes = getASTfromString(src, include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator(echo_fn=lambda msg: None)
        with pytest.raises(EvalError):
            ev.evaluate(nodes, root_scope)
        assert [n.kind for n in ev.csg_tree] == ["cube"]


# ---------------------------------------------------------------------------
# CSG tree Phase 2, step 1 — resolve/generate split for leaf primitives
# (cube, sphere, cylinder, polyhedron, circle, square, polygon, text).
# Generation is now fully deferred to generate_tree() (Phase 2 step 6, the
# final cutover), so these tests cover the params shape and the "mixed
# migration" scenarios exercised while the split was rolled out kind-by-kind
# — nesting a migrated leaf inside a wrapper migrated in a later step, which
# during the rollout risked a migrated node's bodies being silently dropped
# by a not-yet-migrated wrapper. Kept as regression coverage for that nesting.
# ---------------------------------------------------------------------------

class TestCSGTreeResolveGenerateSplit:
    def test_all_geometry_kinds_registered_in_dispatch(self):
        # Every geometry-producing builtin is migrated as of Phase 2 step 5.
        _, _, ev = run_tree("cube(1);")
        for kind in ("cube", "sphere", "cylinder", "polyhedron", "circle", "square", "polygon", "text",
                     "translate", "rotate", "scale", "mirror", "resize", "multmatrix", "color",
                     "hull", "minkowski", "offset", "projection",
                     "union", "difference", "intersection", "intersection_for",
                     "linear_extrude", "rotate_extrude", "roof", "surface", "import"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind in ev._GENERATE_DISPATCH

    def test_cube_params_shape(self):
        _, _, ev = run_tree("cube([2,3,4], center=true);")
        params = ev.csg_tree[0].params
        assert params["size"] == [2.0, 3.0, 4.0]
        assert params["center"] is True
        assert "color" in params

    def test_sphere_params_shape(self):
        _, _, ev = run_tree("sphere(r=5, $fn=12);")
        params = ev.csg_tree[0].params
        assert "verts" in params and "tris" in params
        assert params["verts"].shape[1] == 3

    def test_cylinder_params_shape(self):
        _, _, ev = run_tree("cylinder(h=10, r1=2, r2=4);")
        params = ev.csg_tree[0].params
        assert params["h"] == 10.0
        assert params["r1"] == 2.0
        assert params["r2"] == 4.0
        assert params["segs"] >= 3

    def test_polyhedron_params_shape(self):
        src = "polyhedron(points=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]], faces=[[0,1,2],[0,1,3],[0,2,3],[1,2,3]]);"
        _, _, ev = run_tree(src)
        params = ev.csg_tree[0].params
        assert "verts" in params and "tri_arr" in params

    def test_circle_square_polygon_params_shape(self):
        _, _, ev = run_tree("circle(r=3, $fn=8);")
        assert ev.csg_tree[0].params["name"] == "circle"
        assert ev.csg_tree[0].params["r"] == 3.0

        _, _, ev = run_tree("square([2,5]);")
        assert ev.csg_tree[0].params["name"] == "square"
        assert ev.csg_tree[0].params["size"] == [2.0, 5.0]

        _, _, ev = run_tree("polygon(points=[[0,0],[1,0],[0,1]]);")
        assert ev.csg_tree[0].params["name"] == "polygon"
        assert ev.csg_tree[0].params["paths"] is None

    def test_text_params_shape(self):
        _, _, ev = run_tree('text("Hi", size=10);')
        params = ev.csg_tree[0].params
        assert "font_spec" in params and "glyphs" in params and "scale" in params

    def test_migrated_leaf_inside_migrated_union(self):
        # union() was migrated in step 4; still a useful direct regression
        # check for the mixed-migration hazard this test was first written
        # for (when union was still eager: if a migrated cube/sphere
        # silently returned no bodies, union's per-statement grouping would
        # treat them as empty statements and drop them from the result).
        bb = bbox(run('union() { cube(2, center=true); translate([5,0,0]) sphere(1); }')[0])
        assert bb[3] - bb[0] == approx(7)  # spans from cube's left edge to sphere's right edge

    def test_migrated_leaf_inside_migrated_transform(self):
        # translate() was migrated in step 2 (both this and cube are now
        # resolve/generate); still a useful direct regression check.
        bb = bbox(run("translate([10,0,0]) cube(2, center=true);")[0])
        assert bb[0] == approx(9) and bb[3] == approx(11)

    def test_migrated_leaf_inside_migrated_color(self):
        # color() was migrated in step 2 (both this and sphere are now
        # resolve/generate); still a useful direct regression check.
        bodies, _ = run('color("red") sphere(2);')
        assert len(bodies) == 1
        r, g, b = bodies[0].color[:3]
        assert r == approx(1.0) and g == approx(0.0, rel=1) and b == approx(0.0, rel=1)

    def test_migrated_leaf_inside_migrated_hull_and_transform(self):
        # hull() and translate() were both migrated in later steps (3, 2);
        # still a useful direct regression check alongside cube.
        bb = bbox(run("hull() { cube(1); translate([5,0,0]) cube(1); }")[0])
        assert bb[3] - bb[0] == approx(6)

    def test_migrated_leaf_inside_for_inside_migrated_difference(self):
        # for() is transparent (Phase 1); difference() was migrated in step
        # 4 and specifically needs group_sizes bookkeeping to correctly
        # group a for loop's variable number of contributed tree children
        # into "the second statement" for its per-statement CSG grouping.
        src = "difference() { cube(4, center=true); for (i=[-1:2:1]) translate([i*3,0,0]) sphere(0.5); }"
        bb = bbox(run(src)[0])
        assert bb[3] - bb[0] == approx(4)  # still a 4x4x4 cube's extent, just with holes

    def test_polyhedron_inside_migrated_minkowski(self):
        # minkowski() was migrated in step 3; still a useful direct
        # regression check. CW-from-outside face winding (OpenSCAD
        # convention), matching TestNewBuiltins.test_polyhedron_tetrahedron.
        tet = "polyhedron(points=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]], faces=[[0,1,2],[0,3,1],[0,2,3],[1,3,2]])"
        bodies, _ = run(f"minkowski() {{ {tet}; cube(0.1); }}")
        assert len(bodies) == 1
        assert bodies[0].body.volume() > 0


# ---------------------------------------------------------------------------
# CSG tree Phase 2, step 2 — resolve/generate split for transforms
# (translate/rotate/scale/mirror/multmatrix/resize) and color.
# ---------------------------------------------------------------------------

class TestCSGTreeStep2Transforms:
    def test_transform_kinds_registered(self):
        _, _, ev = run_tree("cube(1);")
        for kind in ("translate", "rotate", "scale", "mirror", "resize", "multmatrix", "color"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind in ev._GENERATE_DISPATCH

    def test_translate_params_shape(self):
        _, _, ev = run_tree("translate([1,2,3]) cube(1);")
        node = ev.csg_tree[0]
        assert node.kind == "translate"
        assert node.params["name"] == "translate"
        assert node.params["args"][0] == [1, 2, 3]
        assert len(node.children) == 1 and node.children[0].kind == "cube"

    def test_resize_params_shape_and_generate_uses_child_bbox(self):
        # resize's generate step needs its own (already-generated) child's
        # bounding_box() — confirmed safe since it's this node's own child,
        # not a different node's output.
        bb = bbox(run("resize([4,4,4]) sphere(1);")[0])
        assert bb[3] - bb[0] == approx(4)
        assert bb[4] - bb[1] == approx(4)

    def test_color_params_shape(self):
        _, _, ev = run_tree('color([0,1,0,1]) cube(1);')
        node = ev.csg_tree[0]
        assert node.kind == "color"
        assert node.params["rgba"] == (0.0, 1.0, 0.0, 1.0)

    def test_migrated_transform_wraps_migrated_transform(self):
        # Both translate and scale are migrated — nested migrated wrappers.
        bb = bbox(run("translate([10,0,0]) scale([2,2,2]) cube(1, center=true);")[0])
        assert bb[0] == approx(9) and bb[3] == approx(11)

    def test_migrated_color_wraps_migrated_transform_wraps_migrated_leaf(self):
        bodies, _ = run('color("blue") translate([1,0,0]) sphere(1);')
        assert len(bodies) == 1
        r, g, b = bodies[0].color[:3]
        assert r == approx(0.0, rel=1) and b == approx(1.0)

    def test_migrated_transform_wraps_migrated_offset_wraps_migrated_extrude(self):
        # offset() was migrated in step 3, linear_extrude in step 5 — all
        # three kinds in this nesting are now migrated.
        bb = bbox(run("linear_extrude(height=1) translate([5,0]) offset(r=1) square(2);")[0])
        assert bb[3] - bb[0] == approx(4)  # 2x2 square offset(r=1) -> 4x4, translate doesn't change extent
        assert bb[0] == approx(4) and bb[3] == approx(8)

    def test_migrated_color_wraps_migrated_union(self):
        # union() was migrated in step 4 — migrated color wrapping a
        # migrated union of two migrated leaves.
        bodies, _ = run('color("red") union() { cube(1); translate([3,0,0]) cube(1); }')
        assert len(bodies) == 1
        bb = bbox(bodies)
        assert bb[3] - bb[0] == approx(4)
        assert bodies[0].color[:3][0] == approx(1.0)

    def test_flatten_matches_evaluate_result_with_transforms_and_color(self):
        for src in [
            "translate([1,0,0]) cube(1);",
            "rotate([0,0,45]) cube(1);",
            "scale([2,1,1]) sphere(1);",
            "mirror([1,0,0]) cube(1);",
            'color("green") cylinder(h=2, r=1);',
            "resize([2,2,2]) sphere(1);",
        ]:
            bodies, _, ev = run_tree(src)
            assert flatten_csg_tree(ev.csg_tree) == bodies


# ---------------------------------------------------------------------------
# CSG tree Phase 2, step 3 — resolve/generate split for topology (hull,
# minkowski, projection, offset).
# ---------------------------------------------------------------------------

class TestCSGTreeStep3Topology:
    def test_topology_kinds_registered(self):
        _, _, ev = run_tree("cube(1);")
        for kind in ("hull", "minkowski", "offset", "projection"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind in ev._GENERATE_DISPATCH

    def test_hull_minkowski_params_empty(self):
        # hull()/minkowski() take no arguments — only children matter.
        _, _, ev = run_tree("hull() { cube(1); sphere(1); }")
        assert ev.csg_tree[0].params == {}
        _, _, ev = run_tree("minkowski() { cube(1); sphere(1); }")
        assert ev.csg_tree[0].params == {}

    def test_offset_params_shape(self):
        _, _, ev = run_tree("offset(r=2) square(4);")
        params = ev.csg_tree[0].params
        assert params["r"] == 2
        assert params["delta"] is None
        assert params["segs"] is not None

        _, _, ev = run_tree("offset(delta=1, chamfer=true) square(4);")
        params = ev.csg_tree[0].params
        assert params["delta"] == 1
        assert params["chamfer"] is True
        assert params["segs"] is None

    def test_projection_params_shape(self):
        _, _, ev = run_tree("projection(cut=true) cube(2);")
        assert ev.csg_tree[0].params == {"cut": True}
        _, _, ev = run_tree("projection() cube(2);")
        assert ev.csg_tree[0].params == {"cut": False}

    def test_migrated_hull_wraps_migrated_leaves(self):
        bb = bbox(run("hull() { cube(1); translate([5,0,0]) cube(1); }")[0])
        assert bb[3] - bb[0] == approx(6)

    def test_migrated_offset_wraps_migrated_union(self):
        # union() was migrated in step 4 — migrated offset wrapping a
        # migrated union of two migrated 2D leaves.
        src = "linear_extrude(height=1) offset(r=1) union() { square(2); translate([3,0]) square(2); }"
        bb = bbox(run(src)[0])
        assert bb[0] == approx(-1) and bb[3] == approx(6)

    def test_migrated_projection_wraps_migrated_union(self):
        src = "linear_extrude(height=1) projection() union() { cube(2); translate([3,0,0]) cube(2); }"
        bodies = run(src)[0]
        assert len(bodies) == 1
        bb = bbox(bodies)
        assert bb[0] == approx(0) and bb[3] == approx(5)

    def test_flatten_matches_evaluate_result_with_topology(self):
        for src in [
            "hull() { cube(1); translate([3,0,0]) sphere(1); }",
            "minkowski() { cube(1); sphere(0.2); }",
            "offset(r=1) square(2);",
            "projection() cube(2);",
        ]:
            bodies, _, ev = run_tree(src)
            assert flatten_csg_tree(ev.csg_tree) == bodies


# ---------------------------------------------------------------------------
# CSG tree Phase 2, step 4 — resolve/generate split for booleans (union/
# difference/intersection) and intersection_for. The genuinely tricky step:
# these do per-statement (or per-iteration) grouping, and for/if/let are
# transparent in the tree (Phase 1), so a single top-level statement can
# contribute a variable, unmarked number of tree children — group_sizes
# bookkeeping (measuring self._tree_stack[-1] length deltas) recovers the
# grouping without needing to inspect AST structure.
# ---------------------------------------------------------------------------

class TestCSGTreeStep4Booleans:
    def test_boolean_kinds_registered(self):
        _, _, ev = run_tree("cube(1);")
        for kind in ("union", "difference", "intersection", "intersection_for"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind in ev._GENERATE_DISPATCH

    def test_union_params_shape_one_group_per_statement(self):
        _, _, ev = run_tree("union() { cube(1); sphere(1); translate([3,0,0]) cube(1); }")
        node = ev.csg_tree[0]
        assert node.kind == "union"
        assert node.params["op"] == "union"
        assert node.params["group_sizes"] == [1, 1, 1]
        assert len(node.children) == 3

    def test_intersection_for_params_shape(self):
        _, _, ev = run_tree("intersection_for(i=[0:2]) rotate([0,0,i*60]) cube([10,2,10], center=true);")
        node = ev.csg_tree[0]
        assert node.kind == "intersection_for"
        assert node.params["group_sizes"] == [1, 1, 1]

    def test_for_nested_in_union_contributes_one_group_of_many(self):
        # A single top-level `for` statement inside union() must count as
        # ONE group in group_sizes, even though it contributes 3 sibling
        # tree children (for is transparent — Phase 1) — otherwise the
        # union's per-statement grouping would misinterpret the 3 spheres
        # as 3 separate top-level statements instead of 1.
        src = "union() { cube(1); for (i=[0:2]) translate([2+i*2,0,0]) sphere(0.5); }"
        bodies, _, ev = run_tree(src)
        node = ev.csg_tree[0]
        assert node.params["group_sizes"] == [1, 3]
        assert len(node.children) == 4  # cube + 3 spheres, flattened in the tree
        bb = bbox(bodies)
        assert bb[0] == approx(0) and bb[3] == approx(6.5)

    def test_if_else_nested_in_difference_contributes_one_group(self):
        src = ("difference() { cube(4, center=true); "
               "if (true) { translate([1,0,0]) sphere(0.5); } else { sphere(2); } }")
        bodies, _, ev = run_tree(src)
        node = ev.csg_tree[0]
        assert node.params["group_sizes"] == [1, 1]  # true-branch: 1 sphere (not 2 — else never ran)
        bb = bbox(bodies)
        # Carving a small sphere out of the cube doesn't change the outer extent.
        assert bb[0] == approx(-2) and bb[3] == approx(2)

    def test_let_nested_in_intersection_contributes_one_group(self):
        src = "intersection() { cube(4, center=true); let(r=1.5) sphere(r); }"
        bodies, _, ev = run_tree(src)
        node = ev.csg_tree[0]
        assert node.params["group_sizes"] == [1, 1]
        assert len(bodies) == 1
        assert bodies[0].body.volume() < 64  # strictly smaller than the 4^3 cube

    def test_intersection_for_iteration_with_multiple_statements(self):
        # Each iteration's body can itself contain a variable number of
        # geometry statements (here via if/else) — group_sizes must track
        # per-ITERATION size, not assume 1 tree child per iteration.
        src = ("intersection_for(i=[0:1]) { "
               "if (i==0) { cube(3, center=true); } else { cube(2, center=true); } }")
        bodies, _, ev = run_tree(src)
        node = ev.csg_tree[0]
        assert node.params["group_sizes"] == [1, 1]
        bb = bbox(bodies)
        # Intersection of cube(3) and cube(2), both centered -> cube(2)'s extent.
        assert bb[0] == approx(-1) and bb[3] == approx(1)

    def test_intersection_empty_operand_anywhere_discards_whole_result(self):
        # intersection(A, ∅, B) = ∅ regardless of position — an empty
        # operand nullifies the whole result, even one already established
        # from prior non-empty statements.
        bodies = run("intersection() { cube(3); *cube(10); cube(2); }")[0]
        assert bodies == []

    def test_difference_later_empty_operand_just_skipped_not_discarded(self):
        # Only an empty FIRST (positive) operand empties a difference — a
        # later empty operand just subtracts nothing and is skipped.
        bodies = run("difference() { cube(3); *cube(10); }")[0]
        assert len(bodies) == 1
        assert bodies[0].body.volume() == approx(27)

    def test_difference_first_empty_operand_discards_whole_result(self):
        bodies = run("difference() { *cube(3); cube(2); }")[0]
        assert bodies == []

    def test_union_skips_disabled_middle_statement(self):
        bodies = run("union() { cube(1); *cube(10); translate([3,0,0]) cube(1); }")[0]
        assert len(bodies) == 1
        assert bodies[0].body.volume() == approx(2)

    def test_migrated_union_wraps_migrated_linear_extrude(self):
        # linear_extrude() was migrated in step 5 — migrated union wrapping
        # a migrated extrude. If linear_extrude silently returned no bodies,
        # union's per-statement grouping would drop it.
        bodies = run("union() { cube(1); translate([3,0,0]) linear_extrude(height=2) square(1); }")[0]
        assert len(bodies) == 1
        bb = bbox(bodies)
        assert bb[0] == approx(0) and bb[3] == approx(4)

    def test_flatten_matches_evaluate_result_with_booleans(self):
        for src in [
            "union() { cube(1); sphere(1); }",
            "difference() { cube([4,4,4]); cube([2,2,2]); }",
            "intersection() { cube(3, center=true); sphere(2); }",
            "union() { cube(1); for (i=[0:2]) translate([2+i*2,0,0]) sphere(0.5); }",
            "intersection_for(i=[0:2]) rotate([0,0,i*60]) cube([10,2,10], center=true);",
        ]:
            bodies, _, ev = run_tree(src)
            assert flatten_csg_tree(ev.csg_tree) == bodies


# ---------------------------------------------------------------------------
# CSG tree — Phase 2 step 5: extrusion + surface + import
# ---------------------------------------------------------------------------

_UNIT_CUBE_OBJ = """\
v 0.0 0.0 0.0
v 0.0 0.0 1.0
v 0.0 1.0 0.0
v 0.0 1.0 1.0
v 1.0 0.0 0.0
v 1.0 0.0 1.0
v 1.0 1.0 0.0
v 1.0 1.0 1.0
f 2 1 5
f 3 5 1
f 2 4 1
f 4 2 6
f 4 3 1
f 4 8 3
f 6 5 7
f 6 2 5
f 7 5 3
f 8 7 3
f 8 4 6
f 8 6 7
"""


class TestCSGTreeStep5Extrusion:
    def test_extrusion_kinds_registered(self):
        _, _, ev = run_tree("cube(1);")
        for kind in ("linear_extrude", "rotate_extrude", "roof", "surface", "import"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind in ev._GENERATE_DISPATCH

    def test_linear_extrude_params_shape(self):
        _, _, ev = run_tree(
            "linear_extrude(height=5, center=true, twist=90, slices=10, scale=2) square(1);")
        params = ev.csg_tree[0].params
        assert params["height"] == approx(5)
        assert params["center"] is True
        assert params["twist"] == approx(90)
        assert params["slices"] == 10
        assert params["scale_top"] == (approx(2), approx(2))
        assert "color" in params

    def test_linear_extrude_geometry_centered(self):
        bodies = run("linear_extrude(height=5, center=true) square([2,3]);")[0]
        bb = bbox(bodies)
        assert bb[2] == approx(-2.5) and bb[5] == approx(2.5)
        assert bodies[0].body.volume() == approx(30)

    def test_rotate_extrude_params_shape_caches_fn_fa_fs(self):
        _, _, ev = run_tree("rotate_extrude($fn=32, $fa=5, $fs=1) translate([5,0]) square([2,3]);")
        params = ev.csg_tree[0].params
        assert params["angle"] == approx(360)
        assert params["fn"] == approx(32)
        assert params["fa"] == approx(5)
        assert params["fs"] == approx(1)
        assert "color" in params

    def test_rotate_extrude_segment_count_depends_on_children_bounds(self):
        # segs is computed from cs.bounds() at generate time — bounds don't
        # exist until the 2D children are generated, so this can't be
        # precomputed in resolve the way e.g. offset's segs can. A regular
        # 32-gon revolve of a shape spanning x=[5,7] hits exactly +-7 on
        # both axes (vertices land on the 0/90/180/270 degree marks).
        bodies = run("rotate_extrude($fn=32) translate([5,0]) square([2,3]);")[0]
        bb = bbox(bodies)
        assert bb[0] == approx(-7) and bb[3] == approx(7)
        assert bb[1] == approx(-7) and bb[4] == approx(7)

    def test_roof_params_shape_and_bad_method_warning(self):
        _, echo, ev = run_tree('roof(method="bogus") square(10, center=true);')
        assert ev.csg_tree[0].params["method"] == "voronoi"
        assert any("Unknown roof method" in line for line in echo)

    def test_roof_straight_skeleton_peak_height(self):
        bodies = run('roof(method="straight") square([10,10], center=true);')[0]
        bb = bbox(bodies)
        assert bb[5] == approx(5, rel=1e-2)  # square roof peaks at half the min side

    def test_surface_params_caches_parsed_heights(self, tmp_path):
        dat = tmp_path / "heights.dat"
        dat.write_text("0 0 0\n0 5 0\n0 0 0\n")
        _, _, ev = run_tree(f'surface(file="{dat}");')
        params = ev.csg_tree[0].params
        assert params["heights"] == [[0.0, 0.0, 0.0], [0.0, 5.0, 0.0], [0.0, 0.0, 0.0]]
        assert params["center"] is False

    def test_surface_geometry_from_dat_file(self, tmp_path):
        dat = tmp_path / "heights.dat"
        dat.write_text("0 0 0\n0 5 0\n0 0 0\n")
        bodies = run(f'surface(file="{dat}");')[0]
        bb = bbox(bodies)
        assert bb[3] == approx(2) and bb[4] == approx(2)  # 3x3 grid -> 2x2 footprint
        assert bb[5] == approx(5)

    def test_surface_missing_file_param_raises(self):
        with pytest.raises(EvalError):
            run("surface();")

    def test_import_obj_params_shape_caches_verts_tris(self, tmp_path):
        obj = tmp_path / "cube.obj"
        obj.write_text(_UNIT_CUBE_OBJ)
        _, _, ev = run_tree(f'import("{obj}");')
        params = ev.csg_tree[0].params
        assert params["kind"] == "mesh"
        assert len(params["verts"]) == 8
        assert len(params["tris"]) == 12

    def test_import_obj_geometry_matches_unit_cube(self, tmp_path):
        obj = tmp_path / "cube.obj"
        obj.write_text(_UNIT_CUBE_OBJ)
        bodies = run(f'import("{obj}");')[0]
        assert bodies[0].body.volume() == approx(1)
        bb = bbox(bodies)
        assert bb == (approx(0), approx(0), approx(0), approx(1), approx(1), approx(1))

    def test_import_svg_geometry_rect(self, tmp_path):
        svg = tmp_path / "rect.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg">'
                        '<rect x="0" y="0" width="10" height="20"/></svg>')
        bodies = run(f'import("{svg}");')[0]
        assert bodies[0].section.area() == approx(200)

    def test_import_unsupported_extension_raises(self):
        with pytest.raises(EvalError):
            run('import("nonexistent.xyz");')

    def test_migrated_transform_wraps_migrated_rotate_extrude(self):
        bodies = run("translate([1,0,0]) rotate_extrude($fn=16) translate([5,0]) circle(1);")[0]
        assert len(bodies) == 1
        bb = bbox(bodies)
        assert bb == (approx(-5), approx(-6), approx(-1), approx(7), approx(6), approx(1))

    def test_migrated_union_wraps_migrated_roof(self):
        # migrated union wrapping a migrated roof (and a migrated cube). If
        # roof silently returned no bodies, union's per-statement grouping
        # would drop it and the combined bbox would only cover the cube.
        bodies = run(
            "union() { cube([1,1,1]); translate([20,0,0]) roof() square(4, center=true); }")[0]
        assert len(bodies) == 1
        bb = bbox(bodies)
        assert bb[3] == approx(22)  # 20 + half of the 4-wide roof footprint

    def test_flatten_matches_evaluate_result_with_extrusion(self):
        for src in [
            "linear_extrude(height=3) circle(2);",
            "rotate_extrude() translate([3,0]) circle(1);",
            'roof(method="straight") square(6, center=true);',
        ]:
            bodies, _, ev = run_tree(src)
            assert flatten_csg_tree(ev.csg_tree) == bodies


# ---------------------------------------------------------------------------
# CSG tree Phase 2, step 6 — final cutover: evaluate() is now genuinely
# two-pass. Resolve (the AST walk) builds the whole tree as plain data with
# no Manifold calls at all; generate_tree() is a separate bottom-up pass
# that does all the Manifold/CrossSection work. This also required giving
# the #/%/! tag modifiers and render()/children()/breakpoint() their own
# resolve_fn (previously handled eagerly inline in _eval_statement_impl),
# and removing _resolve_csg's resolve-time short-circuit (which relied on
# already-generated child bodies that no longer exist until generate_tree()
# runs) in favor of _generate_csg deciding discard-vs-skip purely from real
# generated bodies.
# ---------------------------------------------------------------------------

class TestCSGTreeStep6FinalCutover:
    def _resolve_only(self, src: str):
        """Run just the resolve pass (build csg_tree) without calling
        generate_tree(), to inspect the tree before any Manifold work has
        happened."""
        nodes = getASTfromString(src, include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator()
        ev._resolve_use_statements(nodes, root_scope)
        ev.csg_tree = []
        ev._tree_stack = [ev.csg_tree]
        ctx = EvalContext(scope=root_scope)
        ev._root_ctx = ctx
        for node in nodes:
            ev._eval_statement(node, ctx)
        return ev

    def test_resolve_alone_leaves_bodies_empty(self):
        ev = self._resolve_only("cube(1); sphere(1);")
        assert [n.bodies for n in ev.csg_tree] == [[], []]

    def test_generate_tree_populates_bodies_after_resolve(self):
        ev = self._resolve_only("cube(1); sphere(1);")
        result = ev.generate_tree(ev.csg_tree)
        assert len(result) == 2
        assert all(n.bodies for n in ev.csg_tree)

    def test_generate_tree_works_on_partial_tree(self):
        # Simulates a debugger breakpoint mid-walk (Phase 3): resolve only
        # the first two of three top-level statements, then generate_tree()
        # just the partial tree built so far.
        nodes = getASTfromString(
            "cube(1); sphere(1); translate([5,0,0]) cube(2);", include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator()
        ev._resolve_use_statements(nodes, root_scope)
        ev.csg_tree = []
        ev._tree_stack = [ev.csg_tree]
        ctx = EvalContext(scope=root_scope)
        ev._root_ctx = ctx
        ev._eval_statement(nodes[0], ctx)
        ev._eval_statement(nodes[1], ctx)
        partial = ev.generate_tree(ev.csg_tree)
        assert [n.kind for n in ev.csg_tree] == ["cube", "sphere"]
        assert len(partial) == 2

    def test_modifier_kinds_registered_with_generate_fn(self):
        _, _, ev = run_tree("cube(1);")
        for kind in ("highlight", "background", "show_only"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind in ev._GENERATE_DISPATCH

    def test_render_children_breakpoint_use_default_concatenation(self):
        # These have a resolve_fn (to build the tree correctly) but no
        # generate_fn — generate_tree()'s default (concatenate children's
        # bodies) is what reproduces their old passthrough behavior.
        _, _, ev = run_tree("cube(1);")
        for kind in ("render", "children", "breakpoint"):
            assert kind in ev._RESOLVE_DISPATCH
            assert kind not in ev._GENERATE_DISPATCH

    def test_highlight_background_show_only_tree_and_roles(self):
        bodies, _, ev = run_tree("#cube(1); %sphere(1); cube(1);")
        assert [n.kind for n in ev.csg_tree] == ["highlight", "background", "cube"]
        assert [c.kind for c in ev.csg_tree[0].children] == ["cube"]
        assert [b.role for b in bodies] == ["highlight", "background", "normal"]
        assert flatten_csg_tree(ev.csg_tree) == bodies

    def test_user_module_call_tree_uses_fallback_and_concatenates(self):
        bodies, _, ev = run_tree(
            "module wrap() { translate([1,0,0]) cube(2); } wrap();")
        node = ev.csg_tree[0]
        assert node.kind == "wrap"
        assert node.is_builtin is False
        assert [c.kind for c in node.children] == ["translate"]
        assert flatten_csg_tree(ev.csg_tree) == bodies

    def test_module_shadowing_builtin_name_still_dispatches_to_user_module(self):
        bodies = run("module render() { cube(3); } render();")[0]
        assert bodies[0].body.volume() == approx(27)

    def test_unknown_module_warns_and_produces_no_geometry(self):
        bodies, echo = run("foobar_totally_unknown(1, 2, 3);")
        assert bodies == []
        assert any("Ignoring unknown module 'foobar_totally_unknown'" in line for line in echo)

    def test_side_effect_after_would_be_short_circuited_statement_still_fires(self):
        # Final-cutover behavior change (intentional): since resolve can no
        # longer tell a statement's geometry will end up empty (that's only
        # knowable once real bodies exist, in generate_tree()), every
        # statement is always resolved — so echo() after a *cube(10)
        # (disabled, contributes no geometry) still fires, even inside an
        # intersection() whose combined geometry result is discarded to ∅
        # by the first empty operand.
        bodies, echo = run('intersection() { *cube(10); echo("fired"); cube(2); }')
        assert bodies == []
        assert echo == ['ECHO: "fired"']

    def test_flatten_matches_evaluate_result_with_modifiers_and_modules(self):
        for src in [
            "#cube(1); %sphere(1); cube(1);",
            "module wrap() { translate([1,0,0]) cube(2); } wrap();",
            "render() cube(2);",
            "module m() { children(); } m() cube(1);",
        ]:
            bodies, _, ev = run_tree(src)
            assert flatten_csg_tree(ev.csg_tree) == bodies
