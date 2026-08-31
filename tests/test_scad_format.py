"""Tests for belfryscad.window.scad_format -- the "Reformat Selection"
context-menu action's parse-gate and pretty-printer.

Pure-function tests only (can_format/format_scad take/return plain strings;
can_format shells out to openscad_cpp_evaluator.parse via a temp file, no
Qt involved) -- driving the real CodeEditor widget's context menu itself
was verified via a throwaway script instead, not pytest (see
feedback_gl_qt_tests_crash_pytest memory).
"""

from belfryscad.window.scad_format import can_format, format_scad


class TestCanFormat:
    def test_valid_statement(self):
        assert can_format("cube(1);") is True

    def test_unclosed_paren_rejected(self):
        assert can_format("cube([1,2,3") is False

    def test_empty_rejected(self):
        assert can_format("") is False

    def test_whitespace_only_rejected(self):
        assert can_format("   \n  ") is False

    def test_bare_expression_fragment_rejected(self):
        assert can_format("a+b") is False

    def test_multiple_statements(self):
        assert can_format("x = 1;\ncube(x);") is True


class TestFormatScad:
    def test_one_statement_per_line(self):
        assert format_scad("cube(1); sphere(2);") == "cube(1);\nsphere(2);\n"

    def test_brace_placement_and_indent(self):
        out = format_scad("module foo(a){if(a>1){cube(1);}else{sphere(1);}}")
        assert out == (
            "module foo(a) {\n"
            "    if(a>1) {\n"
            "        cube(1);\n"
            "    } else {\n"
            "        sphere(1);\n"
            "    }\n"
            "}\n"
        )

    def test_indent_size_is_configurable(self):
        out = format_scad("module foo(){cube(1);}", indent_size=2)
        assert out == "module foo() {\n  cube(1);\n}\n"

    def test_inline_trailing_comment_stays_on_its_statement_line(self):
        out = format_scad("cube(1); // note\nsphere(2);")
        assert out == "cube(1);  // note\nsphere(2);\n"

    def test_standalone_comment_gets_its_own_line(self):
        out = format_scad("cube(1);\n// note\nsphere(2);")
        assert out == "cube(1);\n// note\nsphere(2);\n"

    def test_blank_lines_collapse_to_at_most_one(self):
        out = format_scad("cube(1);\n\n\n\nsphere(2);")
        assert out == "cube(1);\n\nsphere(2);\n"

    def test_no_leading_blank_line(self):
        out = format_scad("\n\ncube(1);")
        assert out == "cube(1);\n"

    def test_multiline_vector_literal_preserved_verbatim(self):
        # Anything inside ()/[] is untouched, newlines and all -- only
        # statement/block structure outside parens is reformatted.
        src = "x = [\n  1, 2,\n  3, 4\n];"
        assert format_scad(src) == "x = [\n  1, 2,\n  3, 4\n];\n"

    def test_braces_inside_string_are_not_structural(self):
        out = format_scad('echo("a{b};c");')
        assert out == 'echo("a{b};c");\n'

    def test_semicolon_inside_string_is_not_structural(self):
        out = format_scad('x = "a;b"; cube(1);')
        assert out == 'x = "a;b";\ncube(1);\n'

    def test_operator_spacing_preserved_verbatim(self):
        # Not reflowed -- whatever spacing the user had around operators
        # inside a statement is left alone.
        out = format_scad("x=1+2;")
        assert out == "x=1+2;\n"

    def test_for_loop_body_indented(self):
        out = format_scad("for(i=[0:3]){cube(i);}")
        assert out == "for(i=[0:3]) {\n    cube(i);\n}\n"


# ---------------------------------------------------------------------------
# A modifier's child goes on its own indented line
# ---------------------------------------------------------------------------

def test_a_modifier_and_its_child_are_separate_lines():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("translate([i,0,0])cube(1);") == (
        "translate([i, 0, 0])\n"
        "    cube(1);\n"
    )


def test_a_chain_indents_once_per_link():
    from belfryscad.window.scad_format import format_scad

    assert format_scad('color("red")rotate([0,0,45])cube(1);') == (
        'color("red")\n'
        "    rotate([0, 0, 45])\n"
        "        cube(1);\n"
    )


def test_a_block_stays_on_the_modifier_line():
    """K&R, as before -- a `{` is not a child to be indented under."""
    from belfryscad.window.scad_format import format_scad

    assert format_scad("translate([0,0,1]){cube(1);}") == (
        "translate([0, 0, 1]) {\n"
        "    cube(1);\n"
        "}\n"
    )


def test_a_declarations_parameters_are_not_a_child():
    """`module foo(a, b)`'s parens hold parameters, so what follows is the
    body rather than something to indent under a modifier."""
    from belfryscad.window.scad_format import format_scad

    assert format_scad("module foo(a,b){cube(1);}").startswith("module foo(a, b) {")
    assert format_scad("function f(x) = x*2;") == "function f(x) = x*2;\n"


def test_chain_indent_resets_at_the_next_statement():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("translate([1,0,0])cube(1);sphere(2);") == (
        "translate([1, 0, 0])\n"
        "    cube(1);\n"
        "sphere(2);\n"
    )


# ---------------------------------------------------------------------------
# Over-long lists are reflowed
# ---------------------------------------------------------------------------

def test_a_long_argument_list_wraps():
    from belfryscad.window.scad_format import format_scad, WRAP_WIDTH

    out = format_scad("cyl(l=40, d=40, chamfer=7, chamfang=30, from_end=false, "
                       "anchor=CENTER, spin=0, orient=UP);")
    assert out.startswith("cyl(\n")
    assert out.rstrip().endswith("\n);")
    assert all(len(line) <= WRAP_WIDTH for line in out.splitlines())
    assert "orient=UP" in out, "every argument survives"


def test_a_long_vector_wraps_and_fills():
    """Greedily filled, not one element per line: a long vector reads as a
    block of data, and one per line turns a 60-point path into three
    screens of scrolling."""
    from belfryscad.window.scad_format import format_scad, WRAP_WIDTH

    out = format_scad("pts = [[0,0],[1,0],[1,1],[0,1],[0.5,1.5],[2,2],[3,3],"
                       "[4,4],[5,5],[6,6],[7,7],[8,8]];")
    body = [ln for ln in out.splitlines() if ln.startswith("    ")]
    assert len(body) == 2, f"filled, not one per line: {body}"
    assert all(len(line) <= WRAP_WIDTH for line in out.splitlines())
    assert out.count("[") == 13, "twelve points plus the outer bracket"


def test_a_list_that_already_fits_is_left_alone():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("cyl(l=40, d=4);") == "cyl(l=40, d=4);\n"


def test_a_hand_wrapped_list_is_left_alone():
    """A newline between two items is a choice the user made."""
    from belfryscad.window.scad_format import format_scad

    src = "pts = [\n    [0, 0],\n    [1, 1]\n];\n"
    assert "[0, 0],\n" in format_scad(src)


# ---------------------------------------------------------------------------

def test_reformatting_is_idempotent():
    """Reformatting formatted source returns it unchanged. Without this the
    chain break was lost on a second pass -- a statement is joined onto one
    line before the break is applied, so an already-broken chain arrives
    with a newline where the first pass saw none."""
    from belfryscad.window.scad_format import format_scad

    for src in (
        "translate([i,0,0])cube(1);",
        "translate([0,0,1])cyl(l=40, d=40, chamfer=7, chamfang=30, from_end=false, anchor=CENTER);",
        "cyl(l=40, d=40, chamfer=7, chamfang=30, from_end=false, anchor=CENTER, spin=0, orient=UP);",
        "pts = [[0,0],[1,0],[1,1],[0,1],[0.5,1.5],[2,2],[3,3],[4,4],[5,5],[6,6],[7,7],[8,8]];",
        "for(i=[0:3])translate([i,0,0])cube(1);",
        "if(a){cube(1);}else{sphere(1);}",
        "cube(1);  // why",
    ):
        once = format_scad(src)
        assert format_scad(once) == once, f"not idempotent: {src!r}"


def test_wrapping_never_moves_a_trailing_comment():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("cube(1);  // why") == "cube(1);  // why\n"


# ---------------------------------------------------------------------------
# include/use end their own line
# ---------------------------------------------------------------------------
#
# `include <path>` has no terminating semicolon, so nothing else in the
# token pass ever ended the line -- the next statement was run onto the end
# of it: `include <BOSL2/std.scad>cube(1);`.

def test_include_ends_its_line():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("include <BOSL2/std.scad>\ncube(1);") == (
        "include <BOSL2/std.scad>\n"
        "cube(1);\n"
    )


def test_include_ends_its_line_even_when_the_source_ran_them_together():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("include <BOSL2/std.scad> cube(1);") == (
        "include <BOSL2/std.scad>\n"
        "cube(1);\n"
    )


def test_use_behaves_the_same_as_include():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("use <foo.scad>\nsphere(1);") == (
        "use <foo.scad>\n"
        "sphere(1);\n"
    )


def test_consecutive_includes_each_get_a_line():
    from belfryscad.window.scad_format import format_scad

    assert format_scad("include <a.scad>\ninclude <b.scad>\ncube(1);") == (
        "include <a.scad>\n"
        "include <b.scad>\n"
        "cube(1);\n"
    )


def test_a_less_than_is_not_an_include_path():
    """Only `include`/`use` take an angle-bracketed path, so `a < b` must
    not be mistaken for the start of one -- which would swallow the rest of
    the statement looking for a closing `>`."""
    from belfryscad.window.scad_format import format_scad

    assert format_scad("if(a<b)cube(1);") == "if(a<b)\n    cube(1);\n"
    assert format_scad("x = a<b;") == "x = a<b;\n"


def test_a_path_keeps_its_own_spacing():
    """Whitespace inside `<...>` is part of a filename, not something to
    normalise."""
    from belfryscad.window.scad_format import format_scad

    assert format_scad("include <my dir/a.scad>\ncube(1);").startswith(
        "include <my dir/a.scad>\n")


def test_include_formatting_is_idempotent():
    from belfryscad.window.scad_format import format_scad

    for src in ("include <BOSL2/std.scad>\ncube(1);",
                "include <a.scad> include <b.scad> cube(1);",
                "use <foo.scad>\nsphere(1);"):
        once = format_scad(src)
        assert format_scad(once) == once, f"not idempotent: {src!r}"
