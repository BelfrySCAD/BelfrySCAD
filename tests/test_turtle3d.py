"""The Python port of BOSL2's turtle3d(), behind the 3D turtle path editor.

The parity test runs every case through BOSL2's own turtle3d() in the
evaluator and has OpenSCAD itself measure the largest distance between its
points and the port's, which are passed in at full precision -- an echo of
the points would be rounded to 6 digits. Skipped where BOSL2 is not
installed; the rest needs no library.
"""
import math
import os

import numpy as np
import pytest

from belfryscad import turtle3d
from belfryscad.turtle import TurtleError
from belfryscad.window.data_viewer_turtle import format_turtle

CASES = [
    (["move", 10], None),
    (["move", 10, "left", "move", 5, "up", 30, "move", 4, "right", 45, "down", "move"], None),
    (["roll", 30, "left", 20, "move", 5, "xrot", 40, "move", 3, "yrot", 25, "zrot", -60, "move", 2], None),
    (["arcleft", 10, "arcright", 5, 45, "arcup", 8, 60, "arcdown", 4, -30], None),
    (["arcsteps", 7, "arcleft", 10, 120, "arcsteps", 0, "arcup", 3], None),
    (["up", 20, "arcxrot", 10, 90, "arcyrot", 5, -45, "arczrot", 6, 200], None),
    (["left", 30, "up", 20, "arctodir", 10, [0, 1, 1], "arctodir", 4, [-1, 0, 0.2]], None),
    (["arcrot", 7, [[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]], None),
    (["setdir", [1, 1, 1], "move", 5, "setdir", [0, 0, -1], "move", 3, "setdir", [-1, 0, 0], "move", 2], None),
    (["jump", [3, 4, 5], "xjump", 1, "yjump", -2, "zjump", 7, "xyzmove", [1, 1, 1],
      "xmove", 2, "ymove", "zmove", -1], None),
    (["left", 30, "up", 10, "untilx", 20, "untilz", 5], None),
    (["length", 2.5, "move", 2, "scale", 2, "move", "addlength", -1, "move", "angle", 45, "left", "move"], None),
    (["repeat", 3, ["move", 5, "left", 120]], None),
    (["move", 3, ["move", 5, "roll", 45], "up", 30, ["arc", 6, "left", 50],
      ["arc", 4, "up", 30, "right", 40]], None),
    ([["arc", 5, "zrot", 300], ["arc", 3, "xrot", 60], ["arc", 4, "todir", [0, 1, 0.5]]], None),
    ([["move", 4, "rollto", [0, 1, 1]], "up", 30, ["move", 2, "lrollto", [1, 1, 0]],
      ["arc", 5, "left", 30, "rrollto", [0, -1, 0]]], None),
    ([["move", 5, "twist", 40, "grow", 2], "move", 1, ["move", 3, "reverse"], "move", 2,
      ["arc", 6, "left", 90, "steps", 3]], None),
    # state= as a direction, straight up (the FWD fallback for "up"), and a matrix
    (["move", 5, "left", 90, "move", 5], [0, 1, 1]),
    (["move", 5, "up", 45, "move", 5], [0, 0, 1]),
    (["move", 5, "arcleft", 3, 90], [[1, 0, 0, 2], [0, 1, 0, 3], [0, 0, 1, 4], [0, 0, 0, 1]]),
]


def test_the_port_draws_what_bosl2_draws(tmp_path):
    from openscad_cpp_evaluator import Evaluator
    lines = ["include <BOSL2/std.scad>", "include <BOSL2/turtle3d.scad>"]
    for i, (c, s) in enumerate(CASES):
        mine = turtle3d.run(c, s)[0]
        b = "[" + ",".join("[" + ",".join(repr(x) for x in p) + "]" for p in mine) + "]"
        st = "" if s is None else f", state={format_turtle(s)}"
        lines.append(f'let(a = turtle3d({format_turtle(c)}{st}), b = {b}) echo(str("T{i}=", '
                     f'len(a) == len(b) ? max([for (k = idx(a)) norm(a[k] - b[k])]) : -1));')
    path = tmp_path / "t.scad"
    path.write_text("\n".join(lines))
    out = []
    try:
        Evaluator(echo_fn=out.append).evaluate(str(path))
    except Exception as e:
        if "not found" in str(e):
            pytest.skip("BOSL2 is not installed")
        raise
    got = {int(l.split('"T')[1].split("=")[0]): float(l.split("=")[1].rstrip('"'))
           for l in out if l.startswith('ECHO: "T')}
    if not got:
        pytest.skip("BOSL2 is not installed")
    assert got == {i: pytest.approx(0, abs=1e-9) for i in range(len(CASES))}


@pytest.mark.parametrize("commands, rows", [
    (["move", 5, "left"], [("move", [5]), ("left", [])]),
    # one_or_two_arg: the second argument only when it is not a string or list
    (["arcleft", 5, 90, "move"], [("arcleft", [5, 90]), ("move", [])]),
    (["arcleft", 5, ["move", 2]], [("arcleft", [5]), (["move", 2], [])]),
    # a list after a "move"-family command is the next (compound) command
    (["move", ["move", 2, "roll", 5]], [("move", []), (["move", 2, "roll", 5], [])]),
    (["arctodir", 3, [0, 1, 0]], [("arctodir", [3, [0, 1, 0]])]),
])
def test_split_groups_arguments_as_bosl2(commands, rows):
    assert turtle3d.split(commands) == rows
    assert turtle3d.join(rows) == commands


def test_is_turtle3d():
    assert turtle3d.is_turtle3d(["move", 5, "arcup", 3])
    assert turtle3d.is_turtle3d([["arc", 5, "left", 30]])
    assert not turtle3d.is_turtle3d([[0, 0, 0], [1, 1, 1]])     # a path
    assert not turtle3d.is_turtle3d(["move", 5, "turn", 3])     # 2D only


def test_uses_3d_only():
    assert not turtle3d.uses_3d_only(["move", 5, "left", 30, "arcleft", 3])
    assert turtle3d.uses_3d_only(["move", 5, "up", 30])
    assert turtle3d.uses_3d_only([["move", 5, "roll", 3]])
    assert turtle3d.uses_3d_only(["jump", [1, 2, 3]])


def test_spans_follow_bosl2s_deduplication():
    """A turn draws nothing, and its span is the point the turtle is on."""
    path, spans, frame, step = turtle3d.run(["move", 5, "up", 90, "move", 2])
    assert path == [[0, 0, 0], [5, 0, 0], [5, 0, 2]]
    assert spans == [(0, 1), (1, 1), (1, 2)]
    assert np.allclose(frame[:3, 0], [0, 0, 1]) and step == 1


def test_errors_are_reported_not_raised_as_anything_else():
    for bad in (["setdir", 5], ["arcleft", 5, 0], ["untilx", -3], [["move", 2, "bogus", 1]],
                ["arcxrot", 3, 90]):        # a turn about the heading is a twist
        with pytest.raises(TurtleError):
            turtle3d.run(bad)


def _end(commands, start_cmds=()):
    path, _, frame, _ = turtle3d.run(list(start_cmds) + commands)
    return np.array(path[-1]), frame


START = (["roll", 30, "up", 20, "left", 10, "length", 2.5, "move", 1],
         ["arcup", 3, 40, "arcleft", 2, 70])


@pytest.mark.parametrize("start", START)
@pytest.mark.parametrize("target", [[10, 3, 2], [-4, 6, 1], [1, -8, -5], [0, 0, 9]])
def test_drawn_commands_reach_the_clicked_point(start, target):
    """Each kind of click, from frames with roll and a changed step: a line
    lands within its rounding, a jump exactly, and a tangent arc -- one or
    two halves, whichever the point needs -- on the point, still tangent to
    where the turtle was heading."""
    p0, frame = _end([], start)
    _, _, _, step = turtle3d.run(list(start))
    for extra, tol in ((turtle3d.line_to(frame, step, target), 0.02),
                       (turtle3d.line_to(frame, step, target, jump=True), 1e-9),
                       (turtle3d.tangent_arc(frame, step, target), 0.05)):
        assert extra, extra
        end, _ = _end(extra, start)
        assert np.linalg.norm(end - target) < tol, (extra, end)
    arc = turtle3d.tangent_arc(frame, step, target)
    path, spans, _, _ = turtle3d.run(list(start) + arc)
    first = np.array(path[spans[len(turtle3d.split(list(start)))][0] + 1]) - p0
    assert np.dot(first / np.linalg.norm(first), frame[:3, 0]) > 0.95, "leaves tangent"


def test_a_point_straight_ahead_is_a_move_and_behind_is_unreachable():
    frame = turtle3d.initial_frame()
    assert turtle3d.tangent_arc(frame, 1, [7, 0, 0]) == ["move", 7]
    assert turtle3d.tangent_arc(frame, 1, [-7, 0, 0]) == []


def test_start_direction_round_trips():
    cmds = turtle3d.with_start_direction(["move", 5], [0, 2, 0])
    assert cmds == ["setdir", [0, 1, 0], "move", 5]
    assert turtle3d.start_direction(cmds) == [0, 1, 0]
    assert turtle3d.with_start_direction(cmds, [1, 0, 0])[:2] == ["setdir", [1, 0, 0]]


# -- Which editor a list opens in, and where its start comes from ------------

from belfryscad.window.data_viewers import (find_editable_literals, turtle3d_call_context,
                                            turtle_write_back)


def _kinds(src, at="move"):
    found = find_editable_literals(src, src.index(at) + 1)
    return {k for k in ("turtle", "turtle3d") if k in found}


@pytest.mark.parametrize("src, kinds", [
    ('p = turtle3d(["move", 10, "left", 30]);', {"turtle3d"}),
    ('p = turtle(["move", 10, "left", 30]);', {"turtle"}),
    ('c = ["move", 10, "left", 30];', {"turtle", "turtle3d"}),   # either reads it
    ('c = ["move", 10, "up", 30];', {"turtle3d"}),               # "up" is 3D only
    ('c = ["move", 10, "jump", [1, 2, 3]];', {"turtle3d"}),      # so is a 3D point
    ('c = ["move", 10, ["arc", 5, "left", 30]];', {"turtle3d"}),
])
def test_the_editor_offered_fits_the_list(src, kinds):
    assert _kinds(src) == kinds


def _ctx(src):
    s = src.index('["move"')
    e = src.index("]", src.index('"move"')) + 1
    return turtle3d_call_context(src, s, e), s, e


@pytest.mark.parametrize("state, start, direction", [
    ("", [0, 0, 0], None),
    (", state=move([1, 2, 3])", [1, 2, 3], None),
    (", move([1, 2])", [1, 2, 0], None),
    (", state=UP", None, [0, 0, 1]),
    (", state=[0, 1, 0]", None, [0, 1, 0]),
    (", state=frame", None, None),       # worked out by the script
])
def test_turtle3d_state_is_read_back(state, start, direction):
    ctx, _, _ = _ctx(f'p = turtle3d(["move", 10]{state});')
    assert (ctx["start"], ctx["dir"]) == (start, direction)


@pytest.mark.parametrize("src, moved, out", [
    ('p = turtle3d(["move", 10]);', [1, 2, 3],
     'p = turtle3d(state=move([1, 2, 3]), ["move", 5]);'),
    ('p = turtle3d(["move", 10], state=move([1, 2, 3]));', [4, 5, 6],
     'p = turtle3d(["move", 5], state=move([4, 5, 6]));'),
    ('p = turtle3d(["move", 10]);', [0, 0, 0], 'p = turtle3d(["move", 5]);'),
    # a direction state is the script's, and stays
    ('p = turtle3d(["move", 10], state=UP);', [0, 0, 0], 'p = turtle3d(["move", 5], state=UP);'),
])
def test_turtle3d_start_is_written_back_as_move(src, moved, out):
    ctx, s, e = _ctx(src)
    new, a, b = turtle_write_back(src, s, e, '["move", 5]', ctx, [float(v) for v in moved],
                                  wrap="move({})")
    assert src[:a] + new + src[b:] == out
