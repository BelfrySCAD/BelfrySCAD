"""Colour literals found in a line of source, for the editor's hover swatch.

Pure text -> colour, no widget: this runs on whatever line the mouse is
over, including half-typed ones the parser would reject.
"""
import pytest

from belfryscad.window.color_literals import (
    color_for_name, describe, find_color_literal, looks_like_color_vector,
)


@pytest.mark.parametrize("name, rgb", [
    ("red", (255, 0, 0)),
    ("SteelBlue", (70, 130, 180)),          # case does not matter to Qt
    ("#fff", (255, 255, 255)),
    ("#ffffff", (255, 255, 255)),
    ("#4080c0", (64, 128, 192)),
    # Qt takes it and so does the evaluator's own table (as opaque black,
    # since that table is rgb only) -- matching Qt is what keeps the two
    # agreeing, so this is not an exception to weed out.
    ("transparent", (0, 0, 0)),
])
def test_a_known_name_or_hex_resolves(name, rgb):
    c = color_for_name(name)
    assert c is not None and (c.red(), c.green(), c.blue()) == rgb


@pytest.mark.parametrize("text", [
    "notacolor", "", "ff0000",              # a bare hex needs its #
    "#ffff", "#ff", "#1234567",             # only 3 or 6 digits
    "#rrggbb",
    "rebeccapurple",                        # CSS3 has it, Qt and the
                                            # evaluator's table do not
    "/tmp/red", "red green",
])
def test_anything_else_is_not_a_colour(text):
    assert color_for_name(text) is None


@pytest.mark.parametrize("parts, rgba", [
    (["1", "0", "0"], (255, 0, 0, 255)),
    (["0", "0", "0"], (0, 0, 0, 255)),
    (["1.0", "1.0", "1.0"], (255, 255, 255, 255)),
    ([" .5", "0", "1 "], (128, 0, 255, 255)),
    (["1", "0", "0", "0.5"], (255, 0, 0, 128)),
])
def test_a_vector_of_components_in_range_is_a_colour(parts, rgba):
    c = looks_like_color_vector(parts)
    assert c is not None
    assert (c.red(), c.green(), c.blue(), c.alpha()) == pytest.approx(rgba, abs=1)


@pytest.mark.parametrize("parts", [
    ["1", "0"],                             # too few
    ["1", "0", "0", "1", "0"],              # too many
    ["1", "0", "2"],                        # out of range -- 0..1, not 0..255
    ["255", "0", "0"],
    ["-0.1", "0", "0"],
    ["r", "g", "b"],                        # not literals; needs evaluating
    ["w/2", "0", "0"],
    ["", "", ""],
])
def test_anything_else_is_not_a_colour_vector(parts):
    assert looks_like_color_vector(parts) is None


def at(line, needle, off=0):
    return find_color_literal(line, line.index(needle) + off)


CONTEXTS = [
    ('color("red") cube(1);', '"red"'),
    ('recolor("red") cube(1);', '"red"'),
    ('color_this("red") cube(1);', '"red"'),
    ('color([1, 0, 0]) cube(1);', '[1, 0, 0]'),
    ('    color("red") cube(1);', '"red"'),           # indented
    ('thecolor = [0.5, 0.75, 1.0];', '[0.5, 0.75, 1.0]'),
    ('wall_color = "SteelBlue";', '"SteelBlue"'),
    ('COLOUR2 = "red";', '"red"'),                    # either spelling, any case
    ('stroke(path, color = "red");', '"red"'),
    ('stroke(path, c="red");', '"red"'),              # BOSL2's short spelling
    ('rainbow(list, colour=[1, 0, 0]) cube(1);', '[1, 0, 0]'),
    ('color(mix([1, 0, 0], t)) cube(1);', '[1, 0, 0]'),   # nested inside color()
    ('difference() { color("red") cube(1); }', '"red"'),
]


@pytest.mark.parametrize("line, needle", CONTEXTS,
                         ids=[c[0][:28] for c in CONTEXTS])
def test_a_literal_in_a_colour_position_gets_a_swatch(line, needle):
    assert at(line, needle, 1) is not None


NON_CONTEXTS = [
    ('translate([0.5, 0, 0.2]) cube(1);', '[0.5, 0, 0.2]'),
    ('cube([1, 1, 1]);', '[1, 1, 1]'),
    ('p1 = [0.5, 0.75, 1.0];', '[0.5, 0.75, 1.0]'),   # a point, not a colour
    ('size = [1, 1, 1];', '[1, 1, 1]'),
    ('c = [0.5, 0.5, 0.5];', '[0.5, 0.5, 0.5]'),      # c= is an ARGUMENT name
    ('echo("red");', '"red"'),
    ('include <red/std.scad>', 'red'),
    ('font = "red";', '"red"'),
    ('scale([1, 1, 1]) color("blue") cube(1);', '[1, 1, 1]'),
    ('// color("red") is how you do it', '"red"'),    # in a comment
    ('echo("color(\"red\")");', '\"red\"'),           # inside a string
]


@pytest.mark.parametrize("line, needle", NON_CONTEXTS,
                         ids=[c[0][:28] for c in NON_CONTEXTS])
def test_a_literal_anywhere_else_gets_nothing(line, needle):
    """A colour and a point are the same three numbers; only the position
    tells them apart."""
    assert at(line, needle, 1) is None


def test_the_colour_call_can_be_further_out():
    """`color(...)` anywhere in the open-call chain counts, so a literal
    nested inside another call within it still resolves."""
    assert at('color(concat(c, [1, 0, 0])) cube(1);', '[1, 0, 0]', 1) is not None


def test_the_literal_the_offset_sits_in_is_the_one_found():
    line = '    color("red") cube(1);'
    start, end, c = at(line, '"red"', 2)
    assert line[start:end] == '"red"'
    assert c.red() == 255


@pytest.mark.parametrize("off", [0, 1, 3, 5])
def test_anywhere_in_the_literal_counts(off):
    assert at('color("red")', '"red"', off) is not None


def test_outside_every_literal_is_nothing():
    line = 'color("red") cube(1);'
    assert find_color_literal(line, 0) is None
    assert find_color_literal(line, len(line) - 2) is None


def test_a_vector_is_found_by_position_too():
    line = 'color([0.2, 0.4, 0.9]) sphere(1);'
    start, end, c = at(line, '[0.2', 1)
    assert line[start:end] == '[0.2, 0.4, 0.9]'
    assert (c.red(), c.green(), c.blue()) == (51, 102, 230)


def test_the_innermost_vector_wins():
    """Hovering one triple of a list of them answers about that triple."""
    line = 'color(pick([[0, 0, 0], [1, 1, 1]], i)) cube(1);'
    _s, _e, c = at(line, '[1, 1, 1]', 1)
    assert (c.red(), c.green(), c.blue()) == (255, 255, 255)


def test_a_string_inside_a_vector_is_read_as_the_string():
    line = 'color(["red", 1]);'
    start, end, c = at(line, '"red"', 1)
    assert line[start:end] == '"red"'
    assert c.red() == 255


def test_a_non_colour_literal_offers_nothing_rather_than_the_line():
    """The match is the literal under the offset, so a miss is a miss --
    it must not fall through and answer about some other literal."""
    assert at('color("nope") cube([1, 0, 0]);', '"nope"', 1) is None
    assert at('color([1, 2, 3]) cube(1);', '[1, 2, 3]', 1) is None


def test_an_unterminated_string_is_not_a_colour():
    """Half-typed lines are exactly what this runs on."""
    assert find_color_literal('color("red', 8) is None


def test_describe_names_the_colour_and_spells_it_out():
    text = describe(color_for_name("red"))
    assert "red" in text and "#ff0000" in text and "[1, 0, 0]" in text


def test_describe_shows_alpha_only_when_there_is_some():
    assert "0.5" in describe(looks_like_color_vector(["1", "0", "0", "0.5"]))
    assert describe(looks_like_color_vector(["1", "0", "0"])).endswith("[1, 0, 0]")


def test_an_assignment_counts_only_when_the_literal_IS_the_value():
    """"Direct" is the rule: a palette's members are not each the value of
    the assignment, so they get nothing. Inside `color(...)` nesting is
    fine -- there the whole call is about colour however deep it goes."""
    assert at('thecolors = [[1, 0, 0], [0, 1, 0]];', '[1, 0, 0]', 1) is None
    assert at('thecolor = [1, 0, 0];', '[1, 0, 0]', 1) is not None
