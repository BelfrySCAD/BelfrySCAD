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
    path, spans, _ = turtle.run(["move", 10, "turn", "move", 5, "right", 90, "move"])
    assert close(path, [[0, 0], [10, 0], [10, 5], [11, 5]])
    assert spans == [(0, 1), (1, 1), (1, 2), (2, 2), (2, 3)]


def test_quarter_arcs_end_where_they_should():
    left, _, _ = turtle.run(["arcleft", 5, 90])
    right, _, _ = turtle.run(["arcright", 5, 90])
    assert close([left[-1]], [[5, 5]])
    assert close([right[-1]], [[5, -5]])
    # segs(5) at the default $fa/$fs is 16 -- for any arc angle, as BOSL2 has it.
    assert len(left) == 17
    stepped, spans, _ = turtle.run(["arcsteps", 4, "arcleft", 5, 90])
    assert len(stepped) == 5 and spans[1] == (0, 4)


def test_arc_leaves_the_turtle_turned():
    path, _, heading = turtle.run(["arcleft", 5, 90, "move", 2])
    assert close([path[-1]], [[5, 7]])
    assert close([heading], [[0, 1]])


def test_repeat_and_default_angle():
    path, spans, _ = turtle.run(["angle", 120, "repeat", 3, ["move", 2, "turn"]])
    assert close([path[-1]], [[0, 0]])
    assert len(path) == 4 and spans[1] == (0, 3)


def test_untilx_and_unreachable_goal():
    path, _, _ = turtle.run(["setdir", [1, 1], "untilx", 4])
    assert close([path[-1]], [[4, 4]])
    with pytest.raises(turtle.TurtleError, match="never reaches"):
        turtle.run(["untilx", -1])


def test_is_turtle():
    assert turtle.is_turtle(["move", 10])
    assert not turtle.is_turtle([])
    assert not turtle.is_turtle([[0, 0], [1, 1]])
    assert not turtle.is_turtle(["fly", 10])


def test_line_to_turns_then_moves_to_the_point():
    cmds = turtle.line_to([0, 0], [1, 0], [3, 3])
    assert cmds == ["left", 45.0, "move", 4.24]
    path, _, _ = turtle.run(cmds)
    assert math.dist(path[-1], [3, 3]) < 0.01   # rounded, so within 0.01
    assert turtle.line_to([0, 0], [1, 0], [5, 0]) == ["move", 5.0]
    # `move` is scaled by the step length, so the distance is divided by it.
    assert turtle.line_to([0, 0], [2, 0], [5, 0]) == ["move", 2.5]


def test_jump_faces_along_the_line_and_lands_exactly():
    cmds = turtle.line_to([1, 1], [1, 0], [1, 4.567], jump=True)
    assert cmds == ["left", 90.0, "jump", [1.0, 4.57]]


def test_tangent_arc_ends_at_the_point():
    cmds = turtle.tangent_arc([0, 0], [1, 0], [5, 5])
    assert cmds == ["arcleft", 5.0, 90.0]
    path, _, _ = turtle.run(cmds)
    assert close([path[-1]], [[5, 5]])
    assert turtle.tangent_arc([0, 0], [1, 0], [5, -5]) == ["arcright", 5.0, 90.0]
    # Straight ahead is a move; straight behind no arc reaches.
    assert turtle.tangent_arc([0, 0], [1, 0], [4, 0]) == ["move", 4.0]
    assert turtle.tangent_arc([0, 0], [1, 0], [-4, 0]) == []


def test_snapped_line_angle_and_length():
    # 31 degrees snaps to 30; the distance along 30 degrees is rounded to 1.
    cmds = turtle.line_to([0, 0], [1, 0], [8.57, 5.15], snap_angle=15, snap_len=1)
    assert cmds == ["left", 30.0, "move", 10.0]
    jump = turtle.line_to([0, 0], [1, 0], [0.2, 3.4], jump=True, snap_angle=15, snap_len=1)
    assert jump == ["left", 90.0, "jump", [0.0, 3.0]]


def test_snapped_arc_angle():
    # 2 * atan2(5, 9.5) = 55.5 degrees; snapped to 15 it is 60.
    cmds = turtle.tangent_arc([0, 0], [1, 0], [9.5, 5], snap_angle=15)
    assert cmds[0] == "arcleft" and cmds[2] == 60.0
    path, _, heading = turtle.run(cmds)
    assert math.isclose(math.degrees(math.atan2(heading[1], heading[0])), 60, abs_tol=1e-9)


def test_snapped_arc_radius():
    cmds = turtle.tangent_arc([0, 0], [1, 0], [9.5, 5], snap_angle=15, snap_len=1)
    assert cmds[0] == "arcleft" and cmds[2] == 60.0 and cmds[1] == round(cmds[1])


def test_no_turn_when_it_rounds_to_zero():
    # 0.03 degrees rounds to 0.0 (or -0.0): no left/right, just the move.
    assert turtle.line_to([0, 0], [1, 0], [10, 0.005]) == ["move", 10.0]
    assert turtle.line_to([0, 0], [1, 0], [10, -0.005]) == ["move", 10.0]
    assert turtle.line_to([0, 0], [1, 0], [10, -0.005], jump=True) == ["jump", [10.0, 0.0]]
    assert turtle.line_to([0, 0], [1, 0], [10, 0.4], snap_angle=15, snap_len=1) == ["move", 10.0]


def test_start_heading_reads_and_writes_a_leading_setdir():
    assert turtle.start_heading(["move", 10]) == 0
    assert turtle.start_heading(["setdir", 90, "move"]) == 90
    assert math.isclose(turtle.start_heading(["setdir", [0, -1]]), -90)
    assert turtle.with_start_heading(["move", 10], 45.04) == ["setdir", 45.0, "move", 10]
    assert turtle.with_start_heading(["setdir", 90, "move"], 270) == ["setdir", -90.0, "move"]
    assert turtle.with_start_heading(["move", 10], 360) == ["move", 10]
    assert turtle.with_start_heading(["setdir", 90, "move"], 0) == ["setdir", 0.0, "move"]


def test_run_from_a_start():
    path, _, _ = turtle.run(["setdir", 90, "move", 2], start=(3, 4))
    assert close(path, [[3, 4], [3, 6]])


def test_joints_are_the_drawing_commands():
    assert turtle.joints(["setdir", 90, "move", 5, "left", 30, "arcleft", 5, 90, "jump", [1, 2]]) == [1, 3, 4]
    assert turtle.joints(["move", "repeat", 2, ["move"]]) is None
    assert turtle.joints(["move", "setdir", 90]) is None


def _ends(cmds, start=(0, 0)):
    path, spans, heading = turtle.run(cmds, start=start)
    draws = turtle.joints(cmds)
    return [path[spans[d][1]] for d in draws], heading


def test_dragging_a_joint_keeps_the_later_joints_and_stays_tangent():
    cmds = ["move", 10, "arcleft", 10, 90, "arcright", 5, 90]
    before, _ = _ends(cmds)
    new = turtle.drag_joint(cmds, (0, 0), 1, [18, 12])
    after, _ = _ends(new)
    assert math.dist(after[1], [18, 12]) < 0.02
    assert math.dist(after[0], before[0]) < 1e-9          # joints before: untouched
    assert math.dist(after[2], before[2]) < 0.02          # joints after: where they were
    # Arcs refit as arcs with no turn between: tangent where they meet.
    assert [c for c, _ in turtle.split(new)] == ["move", "arcleft", "arcright"]


def test_dragging_a_move_turns_toward_the_point():
    new = turtle.drag_joint(["move", 10], (0, 0), 0, [10, 10])
    assert new == ["left", 45.0, "move", 14.14]


def test_a_long_flat_arc_lands_on_its_joint():
    # Rounding the angle to 0.1 degree would miss by ~0.4 at this radius.
    cmds = ["arcleft", 422.08, 0.9, "arcleft", 84.08, 8]
    before, _ = _ends(cmds)
    new = turtle.drag_joint(cmds, (0, 0), 0, [before[0][0], before[0][1] + 0.3])
    after, _ = _ends(new)
    assert math.dist(after[0], [before[0][0], before[0][1] + 0.3]) < 0.02
    assert math.dist(after[1], before[1]) < 0.02


def test_an_unreachable_joint_is_refused():
    # Straight behind the start, no tangent arc gets there.
    assert turtle.drag_joint(["arcleft", 5, 90], (0, 0), 0, [-5, 0]) is None


# -- Split and straighten (#837) ---------------------------------------------


_SHAPE = ["setdir", 30, "move", 10, "arcleft", 5, 90, "move", 4, "arcright", 3, 45,
          "jump", [2, 20], "move", 2]


def _end(c, start=(1, 2)):
    path, _, heading = turtle.run(c, start)
    return path[-1], heading


@pytest.mark.parametrize("row, near", [(1, 0.3), (2, 0.5), (3, 0.9), (4, 0.2), (5, 0.6)])
def test_a_split_draws_the_same_path(row, near):
    path, spans, _ = turtle.run(_SHAPE, (1, 2))
    a, b = spans[row]
    pick = path[a + round(near * (b - a))] if b - a > 1 else [
        path[a][k] + near * (path[b][k] - path[a][k]) for k in (0, 1)]
    new = turtle.split_at(_SHAPE, (1, 2), row, pick)
    assert new and len(turtle.split(new)) == len(turtle.split(_SHAPE)) + 1
    (p, h), (q, g) = _end(_SHAPE), _end(new)
    assert math.dist(p, q) < 1e-9 and math.dist(h, g) < 1e-9
    # the cut lands where it was asked, within drawing's rounding
    cut = turtle.run(new, (1, 2))[0][turtle.run(new, (1, 2))[1][row][1]]
    assert math.dist(cut, pick) < 0.05


def test_a_split_refuses_an_end_or_a_turn():
    path = turtle.run(_SHAPE, (1, 2))[0]
    assert turtle.split_at(_SHAPE, (1, 2), 0, path[0]) is None      # setdir
    assert turtle.split_at(_SHAPE, (1, 2), 1, path[0]) is None      # the move's start


@pytest.mark.parametrize("first, last", [(1, 1), (2, 2), (1, 4), (2, 6), (0, 6)])
def test_straightening_leaves_the_rest_in_place(first, last):
    new = turtle.straighten(_SHAPE, (1, 2), first, last)
    rows = turtle.split(new)
    assert all(c in ("left", "right", "move", "setdir", "arcleft", "arcright", "jump")
               for c, _ in rows)
    (p, h), (q, g) = _end(_SHAPE), _end(new)
    assert math.dist(p, q) < 0.01 and math.dist(h, g) < 1e-4
    # the replaced rows are one straight segment
    n = len(rows) - (len(turtle.split(_SHAPE)) - (last + 1))
    assert [c for c, _ in rows[first:n]].count("move") == 1


def test_straightening_refuses_what_a_line_cannot_stand_in_for():
    c = ["move", 5, "length", 2, "move", 3]
    assert turtle.straighten(c, (0, 0), 0, 1) is None
    assert turtle.straighten(c, (0, 0), 2, 2) is not None
