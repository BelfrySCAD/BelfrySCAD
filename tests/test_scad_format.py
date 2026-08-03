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
