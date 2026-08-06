"""AST-driven separator spacing in the reformatter (scad_format)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from belfryscad.window.scad_format import (  # noqa: E402
    _same_shape, _space_separators, can_format, format_scad,
)


def test_commas_get_one_space():
    assert _space_separators("cube([100,200,300],center=true);") == \
        "cube([100, 200, 300], center=true);"


def test_already_spaced_is_untouched():
    src = "cube([1, 2, 3]);"
    assert _space_separators(src) == src


def test_number_spelling_survives():
    # The whole reason this works off spans rather than a printer: a parsed
    # double has already lost how it was written.
    assert _space_separators("cube([1.500,1e3,0x10]);") == "cube([1.500, 1e3, 0x10]);"


def test_user_line_wrapping_is_preserved():
    # A gap containing a newline is a deliberate choice; don't collapse it.
    src = "translate([1,\n  2,\n  3]) cube(1);"
    assert _space_separators(src) == src


def test_comment_between_arguments_is_untouched():
    src = "f(a,/*why*/b);"
    assert _space_separators(src) == src


def test_never_rewrites_inside_a_string():
    # Regression: a string literal's span covers only its opening quote
    # (parser bug), which dragged an outer list's offsets into the string
    # and rewrote a comma in its CONTENT. Found on BOSL2's isosurface.scad.
    src = 'echo("h,l,height,length", x);'
    assert _space_separators(src) == src
    src2 = 'assert(false, "Cannot give degree,mult when you provide a list");'
    assert _space_separators(src2) == src2


def test_invalid_source_is_returned_unchanged():
    src = "cube("
    assert _space_separators(src) == src


def test_module_and_function_parameters():
    assert _space_separators("module m(a,b=2){cube(a);}") == "module m(a, b=2){cube(a);}"
    assert _space_separators("function f(a,b)=a+b;") == "function f(a, b)=a+b;"


def test_format_scad_applies_spacing():
    assert format_scad("module m(){cube([1,2],center=true);}") == \
        "module m() {\n    cube([1, 2], center=true);\n}\n"


def test_same_shape_detects_a_real_change():
    assert _same_shape("cube([1,2]);", "cube([1, 2]);")
    assert not _same_shape("cube([1,2]);", "cube([1,2,3]);")
    assert not _same_shape("cube([1,2]);", "cube(")      # unparseable side


def test_can_format_parses_a_string_directly():
    assert can_format("cube(1);")
    assert not can_format("cube(")
    assert not can_format("   ")
