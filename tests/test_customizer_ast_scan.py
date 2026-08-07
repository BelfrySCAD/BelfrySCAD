"""AST-driven Customizer parameter scanning."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from belfryscad.window.customizer import (  # noqa: E402
    _scan_parameters_lines, scan_parameters,
)


def _tup(params):
    return [(p.name, p.default, p.description, p.tab, p.constraint, p.line_num) for p in params]


# -- cases the old line scanner could not handle -------------------------

def test_multi_line_vector_literal():
    got = _tup(scan_parameters("// Nozzles\nsizes = [\n  0.4,\n  0.6\n];\n"))
    assert got == [("sizes", [0.4, 0.6], "Nozzles", "Parameters", "", 1)]
    assert _scan_parameters_lines("sizes = [\n  0.4\n];\n") == []   # old path misses it


def test_assignment_spanning_lines():
    assert _tup(scan_parameters("width =\n   20;\n")) == \
        [("width", 20, "width", "Parameters", "", 0)]


def test_comment_inside_the_assignment():
    assert _tup(scan_parameters("width = /* mm */ 20;\n")) == \
        [("width", 20, "width", "Parameters", "", 0)]


# -- semantics that must not drift ---------------------------------------

def test_int_vs_float_follows_the_source_spelling():
    # Drives the spin box's step size, and is lost the moment a value is
    # read back as a double -- hence reading the literal's own span text.
    got = _tup(scan_parameters("a = 20;\nb = 20.0;\nc = 1e3;\n"))
    assert [(n, type(v).__name__) for n, v, *_ in got] == \
        [("a", "int"), ("b", "float"), ("c", "float")]


def test_strings_bools_and_negatives():
    got = _tup(scan_parameters('s = "txt";\nb = true;\nn = -5;\n'))
    assert [(n, v) for n, v, *_ in got] == [("s", "txt"), ("b", True), ("n", -5)]


def test_trailing_constraint_is_recovered():
    # The parser drops a trailing comment, so it comes from the source
    # after the statement's own span.
    got = _tup(scan_parameters('// W\nwidth = 20; // [1:50]\nname = "x"; // 12\n'))
    assert [(n, c) for n, _, _, _, c, _ in got] == [("width", "[1:50]"), ("name", "12")]


def test_tab_groups_and_hidden():
    src = "/* [Sizes] */\n// W\nwidth = 20;\n/* [Hidden] */\nsecret = 9;\n"
    got = _tup(scan_parameters(src))
    assert [(n, t) for n, _, _, t, _, _ in got] == [("width", "Sizes")]


def test_description_must_be_adjacent():
    # A commented-out line of code two lines up is not a description.
    # BlankLine nodes only appear between comment runs, so the gap is read
    # from the source. Regression from ntest4.scad.
    assert scan_parameters("// Width\nwidth = 20;")[0].description == "Width"
    assert scan_parameters("//skip=true;\n\nwidth = 20;")[0].description == "width"


def test_nested_assignments_are_not_parameters():
    # Top-level-ness comes from the grammar now, not brace counting.
    got = _tup(scan_parameters("module m() { inner = 1; }\nwidth = 20;\n"))
    assert [n for n, *_ in got] == ["width"]


def test_string_containing_braces_or_slashes():
    got = _tup(scan_parameters('label = "a{b}c";\nurl = "http://x";\n'))
    assert [(n, v) for n, v, *_ in got] == [("label", "a{b}c"), ("url", "http://x")]


# -- the fallback --------------------------------------------------------

def test_unparseable_source_falls_back_to_the_line_scanner():
    # Runs on every keystroke, so the source is incomplete much of the
    # time; the form must stay populated rather than empty out.
    src = "// Width\nwidth = 20; // [1:50]\nbroken = ;\n"
    got = _tup(scan_parameters(src))
    assert [(n, v, c) for n, v, _, _, c, _ in got] == [("width", 20, "[1:50]")]
