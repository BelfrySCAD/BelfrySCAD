"""The Python port of BOSL2's turtle(), behind the turtle path editor.

The expected points were checked against BOSL2's own turtle() through the
evaluator when the port was written (22 command lists, every arc variant,
untilx/untily, setdir and nested repeat). These keep the cases that can be
worked out by hand, so the test needs no BOSL2.
"""
import math

import pytest

from belfryscad import turtle


def close(a, b):
    return len(a) == len(b) and all(math.isclose(x, y, abs_tol=1e-9)
                                    for p, q in zip(a, b) for x, y in zip(p, q))


def test_split_follows_bosl2_argument_grouping():
    cmds = ["move", "turn", 45, "arcleft", 5, "move", 2, "arcright", 3, 90,
            "repeat", 2, ["move"], "xmove"]
    rows = turtle.split(cmds)
    assert rows == [("move", []), ("turn", [45]), ("arcleft", [5]), ("move", [2]),
                    ("arcright", [3, 90]), ("repeat", [2, ["move"]]), ("xmove", [])]
    assert turtle.join(rows) == cmds


def test_moves_and_turns():
    path, spans = turtle.run(["move", 10, "turn", "move", 5, "right", 90, "move"])
    assert close(path, [[0, 0], [10, 0], [10, 5], [11, 5]])
    assert spans == [(0, 1), (1, 1), (1, 2), (2, 2), (2, 3)]


def test_quarter_arcs_end_where_they_should():
    left, _ = turtle.run(["arcleft", 5, 90])
    right, _ = turtle.run(["arcright", 5, 90])
    assert close([left[-1]], [[5, 5]])
    assert close([right[-1]], [[5, -5]])
    # segs(5) at the default $fa/$fs is 16 -- for any arc angle, as BOSL2 has it.
    assert len(left) == 17
    stepped, spans = turtle.run(["arcsteps", 4, "arcleft", 5, 90])
    assert len(stepped) == 5 and spans[1] == (0, 4)


def test_arc_leaves_the_turtle_turned():
    path, _ = turtle.run(["arcleft", 5, 90, "move", 2])
    assert close([path[-1]], [[5, 7]])


def test_repeat_and_default_angle():
    path, spans = turtle.run(["angle", 120, "repeat", 3, ["move", 2, "turn"]])
    assert close([path[-1]], [[0, 0]])
    assert len(path) == 4 and spans[1] == (0, 3)


def test_untilx_and_unreachable_goal():
    path, _ = turtle.run(["setdir", [1, 1], "untilx", 4])
    assert close([path[-1]], [[4, 4]])
    with pytest.raises(turtle.TurtleError, match="never reaches"):
        turtle.run(["untilx", -1])


def test_is_turtle():
    assert turtle.is_turtle(["move", 10])
    assert not turtle.is_turtle([])
    assert not turtle.is_turtle([[0, 0], [1, 1]])
    assert not turtle.is_turtle(["fly", 10])
