"""Reformatting profiles: how freely a reformat may spend vertical space.

#466: "all I want it to do is fix indentation and curly brace positions. I
don't want it to expand out every parentheses ... I want to see as much code
as possible in my editor window without having to scroll."
"""
import pytest

from belfryscad.window.scad_format import (DEFAULT_PROFILE, PROFILES,
                                           WRAP_WIDTH, FormatProfile,
                                           format_scad)

SRC = """module m() {
    let(a = 1) cube(a);


    translate([1,0,0]) cuboid(10, rounding=2, anchor=TOP);
    path = [[0,0],[1,1],[2,4]];
}
"""


def fmt(name, width=80):
    return format_scad(SRC, 4, PROFILES[name], width)


def test_the_three_profiles_exist_in_menu_order():
    assert list(PROFILES) == ["Compact", "Default", "Expanded"]
    assert DEFAULT_PROFILE == "Default"


def test_default_is_unchanged_behaviour():
    """The whole point of a Default profile: nobody's reformat moves."""
    assert format_scad(SRC, 4) == format_scad(SRC, 4, PROFILES["Default"],
                                              WRAP_WIDTH)


# -- Compact: the ask ------------------------------------------------------

def test_compact_keeps_a_statement_and_its_child_on_one_line():
    out = fmt("Compact")
    assert "let(a = 1) cube(a);" in out, out
    assert "translate([1, 0, 0]) cuboid(10, rounding=2, anchor=TOP);" in out, out


def test_default_breaks_them_apart():
    out = fmt("Default")
    assert "let(a = 1)\n        cube(a);" in out, out


def test_compact_is_shorter_than_default():
    assert len(fmt("Compact").splitlines()) < len(fmt("Default").splitlines())


def test_compact_still_fixes_indentation_and_braces():
    """It is a reformatter, not a no-op -- that part was never the complaint."""
    out = format_scad("module m(){\ncube(1);\n}", 4, PROFILES["Compact"], 80)
    assert out == "module m() {\n    cube(1);\n}\n", repr(out)


def test_compact_still_wraps_a_genuinely_over_long_line():
    """"Don't explode what I wrote on one line" is not "never wrap"."""
    long_call = "cuboid(" + ", ".join(f"arg{i}=value{i}" for i in range(12)) + ");"
    out = format_scad(long_call, 4, PROFILES["Compact"], 80)
    assert "\n" in out.strip(), out


# -- Expanded --------------------------------------------------------------

def test_expanded_gives_every_argument_its_own_line():
    out = fmt("Expanded")
    assert "cuboid(\n            10,\n" in out, out


def test_expanded_wraps_a_call_that_is_not_over_long():
    """Default leaves it alone; that is the difference."""
    short = "cuboid(10, rounding=2);"
    assert "\n" not in format_scad(short, 4, PROFILES["Default"], 80).strip()
    assert "\n" in format_scad(short, 4, PROFILES["Expanded"], 80).strip()


def test_expanded_leaves_a_vector_of_data_alone():
    """`translate([1, 0, 0])` is one argument that happens to be a vector,
    and a 60-point path one number per line is three screens of scrolling."""
    out = fmt("Expanded")
    assert "path = [[0, 0], [1, 1], [2, 4]];" in out, out
    assert "translate([1, 0, 0])" in out, out


def test_expanded_keeps_blank_lines_the_others_collapse():
    assert "\n\n\n" in fmt("Expanded")
    assert "\n\n\n" not in fmt("Default")
    assert "\n\n\n" not in fmt("Compact")


def test_a_single_argument_is_not_spread_over_lines():
    """There is nothing to spread."""
    assert "\n" not in format_scad("cube(10);", 4, PROFILES["Expanded"], 80).strip()


# -- The width now comes from the caller -----------------------------------

@pytest.mark.parametrize("width, wrapped", [(200, False), (40, True)])
def test_the_wrap_width_is_a_parameter_not_a_constant(width, wrapped):
    """It used to be a hard-coded 80 whatever column guide you had set."""
    src = "cuboid(size=[10, 20, 30], rounding=2, edges=TOP, anchor=CENTER);"
    out = format_scad(src, 4, PROFILES["Default"], width)
    assert ("\n" in out.strip()) is wrapped, out


def test_the_editor_measures_against_its_rightmost_guide():
    import inspect
    from belfryscad.window.editor import CodeEditor
    src = inspect.getsource(CodeEditor._reformat_selection)
    assert "guide_width()" in src
    assert "WRAP_WIDTH" not in src


# -- Every profile must still be safe --------------------------------------

@pytest.mark.parametrize("name", list(PROFILES))
def test_no_profile_changes_the_parse_tree(name):
    """format_scad discards a rewrite that changes the shape of the AST, and
    that guard is profile-independent -- so a profile cannot corrupt code,
    only lay it out differently."""
    from belfryscad.window.scad_format import _same_shape
    assert _same_shape(SRC, fmt(name))


@pytest.mark.parametrize("name", list(PROFILES))
def test_reformatting_twice_gives_the_same_answer(name):
    once = fmt(name)
    assert format_scad(once, 4, PROFILES[name], 80) == once


def test_brace_style_is_not_a_setting():
    """K&R everywhere. A parameter with one value in every profile is just
    the behaviour."""
    assert not any("brace" in f for f in FormatProfile.__dataclass_fields__)
    for name in PROFILES:
        assert "module m() {" in fmt(name), name


# -- Switching profiles on already-formatted code --------------------------

SHAPES = {
    "chain": "module m() {\n    translate([1,0,0]) cuboid(10, rounding=2, anchor=TOP);\n}\n",
    "long": "cuboid(" + ", ".join(f"arg{i}=val{i}" for i in range(12)) + ");\n",
    "nested": "module m() {\n    f(a, [1, 2, 3], b);\n}\n",
    "data": "path = [[0, 0],\n        [1, 1],\n        [2, 4]];\n",
}


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.parametrize("second", list(PROFILES))
@pytest.mark.parametrize("first", list(PROFILES))
def test_any_profile_reformats_any_other_profiles_output(shape, first, second):
    """Reformatting Expanded output as Compact used to leave it Expanded:
    a multi-line argument list was copied through verbatim and then skipped
    by the wrap pass as "already wrapped by hand". A profile has to be
    reachable from wherever the text currently is, or the submenu is a
    one-way door."""
    src = SHAPES[shape]
    once = format_scad(src, 4, PROFILES[first], 80)
    assert format_scad(once, 4, PROFILES[second], 80) == \
        format_scad(src, 4, PROFILES[second], 80)


def test_compact_does_not_glue_a_call_to_its_child():
    """With the chain break off, nothing flushed at the `)`, and a newline
    at depth 0 added no space -- giving `translate([1, 0, 0])cuboid(`."""
    expanded = format_scad(SHAPES["chain"], 4, PROFILES["Expanded"], 80)
    out = format_scad(expanded, 4, PROFILES["Compact"], 80)
    assert ")cuboid" not in out, out
    assert "translate([1, 0, 0]) cuboid(10, rounding=2, anchor=TOP);" in out, out


def test_no_space_is_left_before_a_closing_bracket():
    expanded = format_scad(SHAPES["long"], 4, PROFILES["Expanded"], 80)
    out = format_scad(expanded, 4, PROFILES["Compact"], 200)
    assert " )" not in out and " ;" not in out, out


def test_a_hand_arranged_data_vector_keeps_its_rows():
    """The collapse is for argument layout, which a profile decides. The
    rows of a matrix, or a path a point per line, are the author's meaning
    and survive every profile."""
    for name in PROFILES:
        out = format_scad(SHAPES["data"], 4, PROFILES[name], 80)
        assert out.count("\n") >= 3, (name, out)
        assert "[1, 1]" in out
