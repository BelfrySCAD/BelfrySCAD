"""
Validates NeuSCAD evaluator echo output against real OpenSCAD.

For each test case, runs the source through both engines and compares
the ECHO: lines. Geometry tests are skipped (OpenSCAD geometry comparison
would require STL parsing; not in scope here).

Usage: uv run python scripts/validate_against_openscad.py
"""

import subprocess
import tempfile
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openscad_parser.ast import getASTfromString, build_scopes
from neuscad.engine.evaluator import Evaluator, EvalError

OPENSCAD = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"

# ---------------------------------------------------------------------------
# Test cases: (name, source)
# Only include cases that produce deterministic echo output.
# ---------------------------------------------------------------------------

CASES = [
    # Expressions
    ("not_equal",           'echo(1 != 2);'),
    ("gte",                 'echo(3 >= 3);'),
    ("vector_add",          'echo([1,2,3] + [4,5,6]);'),
    ("vector_subtract",     'echo([4,5,6] - [1,2,3]);'),
    ("vector_scale_right",  'echo([1,2,3] * 2);'),
    ("vector_scale_left",   'echo(2 * [1,2,3]);'),
    ("unary_minus_vector",  'echo(-[1,2,3]);'),
    ("member_x",            'echo([1,2,3].x);'),
    ("member_y",            'echo([1,2,3].y);'),
    ("member_z",            'echo([1,2,3].z);'),
    ("arithmetic",          'echo(2 + 3 * 4);'),
    ("division",            'echo(7 / 2);'),
    ("modulo",              'echo(7 % 3);'),
    ("exponent",            'echo(2 ^ 10);'),
    ("unary_minus",         'echo(-5);'),
    ("comparison",          'echo(3 > 2);'),
    ("logical_and",         'echo(true && false);'),
    ("logical_or",          'echo(false || true);'),
    ("logical_not",         'echo(!true);'),
    ("ternary_true",        'echo(true ? 1 : 2);'),
    ("ternary_false",       'echo(false ? 1 : 2);'),
    ("vector_literal",      'echo([1,2,3]);'),
    ("vector_index",        'echo([10,20,30][1]);'),
    ("range",               'echo([1:3]);'),
    ("range_step",          'echo([0:2:6]);'),
    ("range_descending",    'echo([5:-1:3]);'),

    # Variables
    ("assignment",          'x = 42; echo(x);'),
    ("undef",               'echo(undef);'),
    ("boolean_literals",    'echo(true); echo(false);'),
    ("string_literal",      'echo("hello");'),
    ("computed_assignment", 'x = 2 + 3; echo(x);'),
    ("special_var_assign",  '$fn = 32; echo($fn);'),
    ("special_var_lookup",  'echo($fa);'),

    # Built-in functions
    ("abs",         'echo(abs(-5));'),
    ("sqrt",        'echo(sqrt(4));'),
    ("floor",       'echo(floor(3.7));'),
    ("ceil",        'echo(ceil(3.2));'),
    ("round",       'echo(round(3.5));'),
    ("min",         'echo(min(3,1,2));'),
    ("max",         'echo(max(3,1,2));'),
    ("sin",         'echo(sin(90));'),
    ("cos",         'echo(cos(0));'),
    ("len",         'echo(len([1,2,3]));'),
    ("concat",      'echo(concat([1,2],[3,4]));'),
    ("str_numbers", 'echo(str(1,2,3));'),
    ("str_no_quotes",'echo(str("hello"));'),
    ("is_num",      'echo(is_num(3));'),
    ("is_list",     'echo(is_list([1,2]));'),
    ("is_undef",    'echo(is_undef(undef));'),
    ("is_bool",     'echo(is_bool(true));'),
    ("is_string",   'echo(is_string("x"));'),
    ("tan",         'echo(tan(45));'),
    ("asin",        'echo(asin(1));'),
    ("acos",        'echo(acos(1));'),
    ("atan",        'echo(atan(1));'),
    ("atan2",       'echo(atan2(1,1));'),
    ("ln",          'echo(ln(1));'),
    ("log",         'echo(log(100));'),
    ("exp",         'echo(exp(0));'),
    ("pow",         'echo(pow(2,8));'),
    ("norm",        'echo(norm([3,4]));'),
    ("cross",       'echo(cross([1,0,0],[0,1,0]));'),
    ("chr",         'echo(chr(65));'),
    ("ord",         'echo(ord("A"));'),

    # User functions
    ("simple_function",     'function double(x) = x * 2; echo(double(5));'),
    ("recursive_function",  'function fact(n) = n <= 1 ? 1 : n * fact(n-1); echo(fact(5));'),
    ("function_default",    'function add(a, b=10) = a + b; echo(add(5));'),

    # Control flow
    ("if_true",     'if (true) { echo(1); }'),
    ("if_false",    'if (false) { echo(1); }'),
    ("if_else",     'if (false) { echo(1); } else { echo(2); }'),
    ("for_loop",    'for (i=[1:3]) { echo(i); }'),
    ("for_step",    'for (i=[0:2:4]) { echo(i); }'),
    ("for_vector",  'for (x=[10,20,30]) { echo(x); }'),

    # List comprehensions
    ("listcomp_for",    'echo([for (i=[1:3]) i*2]);'),
    ("listcomp_if",     'echo([for (i=[1:5]) if(i%2==0) i]);'),
    ("listcomp_let",    'echo([for (i=[1:3]) let(j=i*2) j]);'),

    # Let expressions
    ("let_expression",  'echo(let(x=5) x*2);'),
    ("let_scoping",     'x=1; echo(let(x=99) x); echo(x);'),

    # Echo formatting
    ("echo_named_arg",  'echo(x=42);'),
    ("echo_multiple",   'echo(1,2,3);'),

    # Assert
    ("assert_passes",   'assert(true); echo(1);'),
    ("assert_expr",     'x = assert(true) 42; echo(x);'),

    # Special variables
    ("fa_default",      'echo($fa);'),
    ("fs_default",      'echo($fs);'),
    ("fn_default",      'echo($fn);'),

    # New built-ins: sign, PI, is_num(bool)
    ("sign_positive",   'echo(sign(5));'),
    ("sign_negative",   'echo(sign(-3));'),
    ("sign_zero",       'echo(sign(0));'),
    ("PI_constant",     'echo(PI);'),
    ("is_num_bool",     'echo(is_num(true));'),
    ("is_num_num",      'echo(is_num(42));'),
    ("is_function_false",'echo(is_function(42));'),

    # search — only cases that work correctly in both
    ("search_list",             'echo(search(["b","a"], ["a","b","c"]));'),
    ("search_string_in_string", 'echo(search("a", "abcdabcd"));'),
    ("search_string_num_ret0",  'echo(search("a", "abcdabcd", 0));'),
    ("search_numeric",          'echo(search(2, [1,2,3,2]));'),
    ("search_numeric_notfound", 'echo(search(9, [1,2,3]));'),

    # Lookup
    ("lookup_interpolates",  'echo(lookup(1.5, [[0,0],[1,1],[2,4]]));'),
    ("lookup_clamps_low",    'echo(lookup(-1, [[0,0],[1,1]]));'),
    ("lookup_clamps_high",   'echo(lookup(99, [[0,0],[1,1]]));'),

    # str edge cases
    ("str_bool_true",   'echo(str(true));'),
    ("str_bool_false",  'echo(str(false));'),
    ("str_undef",       'echo(str(undef));'),
    ("str_list",        'echo(str([1,2,3]));'),
    ("str_multi",       'echo(str(1,"+",2,"=",3));'),
    ("concat_two",      'echo(concat([1,2],[3,4]));'),
    ("concat_scalar",   'echo(concat([1,2],3));'),
    ("concat_three",    'echo(concat([1],[2],[3]));'),

    # Scoping
    ("last_wins",               'x=1; x=7; echo(x);'),
    ("forward_ref_function",    'echo(double(5)); function double(x)=x*2;'),
    ("recursive_module_echo",   'module countdown(n) { if(n>0) { echo(n); countdown(n-1); } } countdown(3);'),
    ("module_scope_isolation",  'x=10; module m() { x=20; echo(x); } m(); echo(x);'),

    # version / parent_module stubs
    ("version_is_list",         'echo(is_list(version()));'),
    ("version_num_is_num",      'echo(is_num(version_num()));'),
    ("parent_module_at_toplevel", 'echo(is_undef(parent_module()));'),

    # Number edge cases
    ("div_by_zero",         'echo(1/0);'),
    ("neg_div_by_zero",     'echo(-1/0);'),
    ("zero_div_zero",       'echo(0/0);'),
    ("sqrt_negative",       'echo(sqrt(-1));'),
    ("ln_zero",             'echo(ln(0));'),
    ("ln_negative",         'echo(ln(-1));'),
    ("asin_out_of_range",   'echo(asin(2));'),
    ("float_precision",     'echo(1/3);'),
    ("float_precision2",    'echo(100/3);'),
    ("large_num",           'echo(1e15);'),
    ("pow_zero_zero",       'echo(pow(0,0));'),

    # Boolean arithmetic → undef
    ("bool_add_undef",  'echo(is_undef(true + 1));'),
    ("bool_mul_undef",  'echo(is_undef(true * 5));'),

    # undef comparisons
    ("undef_eq_undef",  'echo(undef == undef);'),
    ("undef_eq_num",    'echo(undef == 1);'),
    ("undef_lt_undef",  'echo(is_undef(undef < 1));'),

    # String operations
    ("string_len",          'echo(len("hello"));'),
    ("string_index",        'echo("hello"[1]);'),
    ("string_index_last",   'echo("hello"[4]);'),
    ("string_neg_index",    'echo(is_undef("hello"[-1]));'),
    ("string_lt",           'echo("a" < "b");'),
    ("string_eq",           'echo("a" == "a");'),

    # Range as variable
    ("range_as_var",        'r=[1:3]; echo(r);'),
    ("range_index",         'r=[1:3]; echo(r[0]);'),
    ("range_index2",        'r=[1:3]; echo(r[2]);'),
    ("range_is_not_list",   'echo(is_list([1:3]));'),
    ("range_len_undef",     'echo(is_undef(len([1:3])));'),
    ("range_zero_step",     'echo([1:0:5]);'),

    # len edge cases
    ("len_undef",   'echo(is_undef(len(undef)));'),
    ("len_num",     'echo(is_undef(len(42)));'),
    ("len_nested",  'echo(len([[1,2],[3,4]]));'),

    # min/max with single list arg
    ("min_list",    'echo(min([3,1,4,1,5]));'),
    ("max_list",    'echo(max([3,1,4,1,5]));'),

    # concat with strings (produces list, not concatenation)
    ("concat_strings",  'echo(concat("ab","cd"));'),

    # Nested list echo
    ("nested_list",         'echo([[1,2],[3,4]]);'),
    ("deeply_nested_list",  'echo([[[1]]]);'),

    # each in listcomp
    ("each_in_listcomp",    'echo([for (i=[[1,2],[3,4]]) each i]);'),

    # nested for in listcomp
    ("nested_for_listcomp", 'echo([for (i=[1:3]) for (j=[1:2]) [i,j]]);'),

    # $children in module
    ("dollar_children_zero",  'module m() { echo($children); } m();'),
    ("dollar_children_one",   'module m() { echo($children); } m() sphere(1);'),
    ("dollar_children_two",   'module m() { echo($children); } m() { sphere(1); cube(1); }'),

    # search: list match for string-in-list
    ("search_list_match",       'echo(search(["b"], ["a","b","c"]));'),
    ("search_list_not_found",   'echo(search(["zzz"], ["a","b","c"]));'),

    # EchoOp / AssertOp in expressions
    ("echo_op_passthrough",     'x = echo("side") 42; echo(x);'),
    ("assert_op_pass",          'x = assert(true) 99; echo(x);'),

    # echo() as module call with children (geometry suppressed, echo still fires)
    ("echo_module_with_children", 'echo("hi") cube(1);'),

    # echo multiple booleans
    ("echo_bool_multi",         'echo(true, false);'),

    # Conditional list elements
    ("cond_list_with_else",     'x = [if(true) 1 else 9, if(false) 2 else 8]; echo(x);'),
    ("cond_list_no_else",       'x = [if(true) 1, if(false) 2, if(true) 3]; echo(x);'),

    # is_function on function literal (named functions are a NeuSCAD extension)
    ("is_function_literal",     'fl = function(x) x*2; echo(is_function(fl));'),

    # for over scalar and undef
    ("for_over_scalar",         'echo([for (x=5) x]);'),
    ("for_over_undef",          'echo([for (x=undef) x]);'),

    # flatten via each
    ("flatten_each",  'function flatten(l) = [for (x=l) each x]; echo(flatten([[1,2,3],[4,5,6]]));'),

    # nested for/let in list comp
    ("grid_nested_for_let",     'grid = [for(h=[0:2]) [let(b=h) for(a=[0:2]) a+b]]; echo(grid);'),

    # each variants
    ("each_literal",    'echo([each [1,2,3]]);'),
    ("each_var",        'a=[1,2,3]; echo([each a]);'),

    # listcomp if/else and let+if
    ("listcomp_if_else",        'echo([for (i=[1:2]) if (i > 1) i*10 else i]);'),
    ("listcomp_let_if",         'echo([for (i=[1:4]) let(j=i*2) if (j > 4) j]);'),

    # let shadows outer variable
    ("let_shadows_outer",       'x = 10; echo([for (i=[1:2]) let(x=i) x]);'),

    # str with mixed types
    ("str_mixed_types",         'echo(str("hello", 42));'),

    # function with missing arg → undef
    ("function_missing_arg",    'function g(a, b) = b; echo(g(1));'),

    # rands: length and range (exact values differ between RNG implementations)
    ("rands_length",            'echo(len(rands(0, 1, 5)));'),
    ("rands_range_check",       'v = rands(10, 20, 3, 42); echo(v[0] >= 10 && v[0] <= 20);'),

    # each with nested lists
    ("each_nested_list",        'a = [[1,2,3],[4,5,6]]; b = [each a]; echo(b);'),
    ("each_no_double_flatten",  'a = [[[1,2],[3,4]]]; echo([each a]);'),

    # $children = 0 when module called with no children
    ("dollar_children_empty",   'module m() { echo($children); } m();'),

    # for loop body variable
    ("for_body_var",            'for (a=[1:3]) { x = a*2; echo(x); }'),

    # let with multiple bindings
    ("let_multi_bindings",      'echo(let(a=3, b=4) a + b);'),

    # calling a non-function variable returns undef
    ("call_non_function",       'x = [1,2,3]; echo(x());'),

    # $fn assignment then lookup
    ("dollar_fn_assign",        '$fn = 64; echo($fn);'),

    # Arithmetic variants
    ("modulo",                  'echo(10 % 3);'),
    ("division_float",          'echo(10 / 4);'),
    ("vec_scale_left",          'echo(3 * [1,2,3]);'),
    ("vec_subtract",            'echo([5,7,9] - [4,5,6]);'),

    # List literals and indexing
    ("vector_literal_spaces",   'echo([1, 2, 3]);'),
    ("vector_index_var",        'v = [10,20,30]; echo(v[1]);'),
    ("member_x_var",            'v = [10,20,30]; echo(v.x);'),
    ("member_y_var",            'v = [10,20,30]; echo(v.y);'),
    ("member_z_var",            'v = [10,20,30]; echo(v.z);'),
    ("string_neg_idx",          'echo(is_undef("hello"[-1]));'),
    ("index_outofbounds",       'echo([1,2,3][10]);'),
    ("member_bad",              'echo([1,2].w);'),
    ("index_nonlist",           'echo(5[0]);'),

    # Variable assignment
    ("var_simple",              'x = 7; echo(x);'),
    ("var_computed",            'a = 3; b = a * 2; echo(b);'),

    # Let edge cases
    ("let_xy",                  'echo(let(x=5, y=3) x + y);'),
    ("let_shadows",             'x = 1; echo(let(x=99) x);'),

    # Listcomp edge cases
    ("listcomp_nested_for",     'echo([for (i=[1:3]) [for (j=[1:2]) i*j]]);'),
    ("listcomp_let_multi",      'echo([for (i=[1:2]) let(a=i+1, b=i*3) [a,b]]);'),
    ("listcomp_each_scalar",    'echo([for (i=[1:3]) each i]);'),
    ("listcomp_if_never",       'echo([for (i=[1:3]) if(i>10) i]);'),
    ("listcomp_odd",            'echo([for (i=[1:5]) if(i%2==1) i]);'),
    ("listcomp_zero_step_iter", 'echo([for (i=[1:0:5]) i]);'),

    # each over nested list
    ("each_nested_2d",          'a = [[1,2],[3,4]]; echo([each a]);'),

    # Builtin variants
    ("ceil_decimal",            'echo(ceil(3.1));'),
    ("floor_decimal",           'echo(floor(3.9));'),
    ("pow_cubed",               'echo(pow(3,3));'),
    ("norm_3_4",                'echo(norm([3,4]));'),
    ("atan2_1_1",               'echo(atan2(1,1));'),
    ("is_string_hi",            'echo(is_string("hi"));'),
    ("abs_named_arg",           'echo(abs(x=-3));'),  # named arg ignored → undef
    ("min_3_args",              'echo(min(5,3,8));'),
    ("max_3_args",              'echo(max(5,3,8));'),

    # Lookup variants
    ("lookup_below",            'echo(lookup(-1, [[0,0],[1,10]]));'),
    ("lookup_mid",              'echo(lookup(0.5, [[0,0],[1,10]]));'),
    ("lookup_above",            'echo(lookup(5, [[0,0],[1,10]]));'),

    # Search string variants
    ("search_b_in_abc",         'echo(search("b", "abc"));'),
    ("search_ba_in_abcd",       'echo(search("ba", "abcd"));'),
    ("search_z_in_abc",         'echo(search("z", "abc"));'),

    # str variants
    ("str_multi_args",          'echo(str(1,"+",2,"=",3));'),
    ("str_nums",                'echo(str(1,2,3));'),
    ("str_list_arg",            'echo(str([1,2,3]));'),

    # intersection_for
    ("intersection_for",
     'intersection_for(i=[0:2]) { translate([i,0,0]) cube([2,2,2]); }'
    ),
]

# Cases that produce no echo (geometry only) — skip comparison
GEOMETRY_ONLY = {"intersection_for"}

# Cases where NeuSCAD intentionally deviates from OpenSCAD:
# - is_function(named_fn): NeuSCAD returns true (named functions are first-class),
#   OpenSCAD returns false (only function literals are first-class values).
# - rands with seed: different RNG implementations give different values.


def run_openscad(src: str) -> list[str]:
    with tempfile.NamedTemporaryFile(suffix=".scad", mode="w", delete=False) as f:
        f.write(src)
        scad_path = f.name
    echo_path = scad_path.replace(".scad", ".echo")
    try:
        result = subprocess.run(
            [OPENSCAD, "-o", echo_path, scad_path],
            capture_output=True, text=True, timeout=15
        )
        if os.path.exists(echo_path):
            lines = Path(echo_path).read_text().splitlines()
            # Filter out WARNING lines (they go to stderr in practice but
            # some versions include them in the echo file)
            return [l for l in lines if l.startswith("ECHO:")]
        return []
    finally:
        os.unlink(scad_path)
        if os.path.exists(echo_path):
            os.unlink(echo_path)


def run_neuscad(src: str) -> list[str]:
    echo_lines = []
    try:
        nodes = getASTfromString(src)
        root_scope = build_scopes(nodes)
        ev = Evaluator(echo_fn=lambda msg: echo_lines.append(msg))
        ev.evaluate(nodes, root_scope)
    except EvalError:
        pass
    return echo_lines


def main():
    passed = 0
    failed = 0
    skipped = 0
    failures = []

    for name, src in CASES:
        if name in GEOMETRY_ONLY:
            skipped += 1
            continue

        openscad_out = run_openscad(src)
        neuscad_out = run_neuscad(src)

        if openscad_out == neuscad_out:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            failures.append((name, src, openscad_out, neuscad_out))
            print(f"  FAIL  {name}")
            print(f"         src:      {src[:80]}")
            print(f"         openscad: {openscad_out}")
            print(f"         neuscad:  {neuscad_out}")

    print()
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped (geometry-only)")
    if failures:
        print()
        print("FAILURES SUMMARY:")
        for name, src, expected, got in failures:
            print(f"  {name}: expected {expected!r}, got {got!r}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
