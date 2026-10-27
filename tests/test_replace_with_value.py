"""Replace with Value (#932): the computed-assignment finder and the
literal it writes back."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from belfryscad.scad_literals import (  # noqa: E402
    is_literal_value, rounded, scad_literal, scad_literal_block,
)
from belfryscad.window.scad_format import find_computed_assignment  # noqa: E402

SRC = '''x = 5;
y = [1, -2, [3, "s"]];
surface = [ for (i = [0:2]) [i, ";"] ];
module m() { z = sin(3); }
'''


def test_finds_only_computed_top_level_assignments():
    name, start, end = find_computed_assignment(SRC, SRC.index("for"))
    assert name == "surface"
    assert SRC[start:end] == '[ for (i = [0:2]) [i, ";"] ]'
    assert find_computed_assignment(SRC, SRC.index("x =")) is None    # literal
    assert find_computed_assignment(SRC, SRC.index("y =")) is None    # literal list
    assert find_computed_assignment(SRC, SRC.index("z =")) is None    # not top level
    assert find_computed_assignment("a = ;", 0) is None               # does not parse


def test_literal_is_exact():
    assert scad_literal(0.1 + 0.2) == "0.30000000000000004"
    assert scad_literal(3.0) == "3"
    assert scad_literal('a"b\\c') == '"a\\"b\\\\c"'
    assert scad_literal(float("inf")) == "1/0"
    assert scad_literal([None, True, -1.5]) == "[undef, true, -1.5]"


def test_rows_of_points_break_one_row_per_line():
    grid = [[[0.0, 0.0], [1.0, 0.0]], [[0.0, 1.0], [1.0, 1.0]]]
    assert scad_literal_block(grid, "  ") == (
        "[\n"
        "      [[0, 0], [1, 0]],\n"
        "      [[0, 1], [1, 1]]\n"
        "  ]")
    path = [[0.0, 0.0], [1.0, 0.5]]
    assert scad_literal_block(path) == "[[0, 0], [1, 0.5]]"


def test_value_read_back_through_the_evaluator(tmp_path):
    from openscad_cpp_evaluator import Evaluator
    f = tmp_path / "v.scad"
    f.write_text("a = [for (i = [0:2]) [cos(i * 25), 1/3]];\n$__probe = a;\n")
    ev = Evaluator()
    ev.evaluate(str(f), generate=False)
    v = ev.dyn["$__probe"]
    assert is_literal_value(v)
    assert scad_literal(v) == ("[[1, 0.3333333333333333], [0.9063077870366499, "
                               "0.3333333333333333], [0.6427876096865393, 0.3333333333333333]]")


def test_rounded_to_six_significant_digits():
    assert scad_literal(rounded([12.990381056766578, 1 / 3, 123456789.0, "x", None])) == \
        '[12.9904, 0.333333, 123457000, "x", undef]'
