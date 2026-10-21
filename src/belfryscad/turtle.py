"""BOSL2's 2D `turtle()`, ported for the turtle path editor's live preview.

Follows BOSL2's `drawing.scad` exactly: the same command set, the same
argument grouping (a command's argument is whatever follows it unless that is
a string), the same state (path, step vector, default turn angle, arcsteps)
and the same arc sampling, `segs(r)` under OpenSCAD's default `$fa`/`$fs`
when `arcsteps` is 0. Qt-free, like `nurbs.py`.

`run()` also records which path points each top-level command drew, which is
what lets the editor highlight a command's segment.
"""
from __future__ import annotations

import math

#: Every 2D turtle command, in BOSL2's documentation order.
COMMANDS = (
    "move", "xmove", "ymove", "xymove", "untilx", "untily", "jump", "xjump",
    "yjump", "turn", "left", "right", "angle", "setdir", "length", "scale",
    "addlength", "repeat", "arcleft", "arcright", "arcleftto", "arcrightto",
    "arcsteps",
)
_ARCS = ("arcleft", "arcright", "arcleftto", "arcrightto")


class TurtleError(ValueError):
    pass


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_vec2(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v)


def split(commands: list) -> list[tuple[str, list]]:
    """`[(command, args), ...]`, grouped by BOSL2's `_turtle_command_len`."""
    out, i, n = [], 0, len(commands)
    while i < n:
        c = commands[i]
        if not isinstance(c, str) or c not in COMMANDS:
            raise TurtleError(f"Unknown turtle command {c!r} at index {i}")
        if c == "repeat":
            size = 3
        elif c in _ARCS and n > i + 2 and not isinstance(commands[i + 2], str):
            size = 3
        elif i + 1 >= n or isinstance(commands[i + 1], str):
            size = 1
        else:
            size = 2
        out.append((c, list(commands[i + 1:i + size])))
        i += size
    return out


def join(rows: list[tuple[str, list]]) -> list:
    """The flat command list `split` came from."""
    return [x for c, args in rows for x in [c, *args]]


def is_turtle(value) -> bool:
    """A non-empty list that is a valid 2D turtle command list."""
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        return False
    try:
        split(value)
    except TurtleError:
        return False
    return True


def segs(r: float) -> int:
    """OpenSCAD's fragment count for radius `r` at `$fn=0, $fa=12, $fs=2`."""
    if r < 1e-5:
        return 3
    return int(math.ceil(max(min(360 / 12, r * 2 * math.pi / 2), 5)))


def _rot(v, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [c * v[0] - s * v[1], s * v[0] + c * v[1]]


def _norm(v):
    return math.hypot(v[0], v[1])


def _unit(v):
    n = _norm(v)
    if n == 0:
        raise TurtleError("The turtle's step has zero length")
    return [v[0] / n, v[1] / n]


def _arc_points(last, center, sweep, steps):
    """`steps` points after `last`, around `center` through `sweep` degrees."""
    rx, ry = last[0] - center[0], last[1] - center[1]
    r, a0 = math.hypot(rx, ry), math.atan2(ry, rx)
    return [[center[0] + r * math.cos(a0 + math.radians(sweep) * k / steps),
             center[1] + r * math.sin(a0 + math.radians(sweep) * k / steps)]
            for k in range(1, steps + 1)]


def _command(c, args, st, where):
    """Apply one command to the state dict `st` in place."""
    path, step = st["path"], st["step"]
    last = path[-1]
    p = args[0] if args else None
    p2 = args[1] if len(args) > 1 else None
    if c == "repeat":
        if not (isinstance(p, int) and not isinstance(p, bool) and p >= 0):
            raise TurtleError(f'"repeat" needs a nonnegative integer count {where}')
        if not isinstance(p2, list):
            raise TurtleError(f'"repeat" needs a command list {where}')
        rows = split(p2)
        for _ in range(p):
            for k, (cc, aa) in enumerate(rows):
                _command(cc, aa, st, f"{where} (repeat item {k})")
        return
    if c in ("jump", "xymove") and not _is_vec2(p):
        raise TurtleError(f'"{c}" needs a [x, y] vector {where}')
    if c in ("untilx", "untily", "xjump", "yjump", "angle", "length", "scale", "addlength") \
            and not _is_num(p):
        raise TurtleError(f'"{c}" needs a number {where}')
    if c == "move":
        d = 1 if p is None else p
        path.append([last[0] + d * step[0], last[1] + d * step[1]])
    elif c in ("untilx", "untily"):
        axis = 0 if c == "untilx" else 1
        if step[axis] == 0 or math.copysign(1, step[axis]) != math.copysign(1, p - last[axis]) \
                or p == last[axis]:
            raise TurtleError(f'"{c}" never reaches {p} {where}')
        t = (p - last[axis]) / step[axis]
        path.append([last[0] + t * step[0], last[1] + t * step[1]])
    elif c == "xmove":
        path.append([last[0] + (1 if p is None else p) * _norm(step), last[1]])
    elif c == "ymove":
        path.append([last[0], last[1] + (1 if p is None else p) * _norm(step)])
    elif c == "xymove":
        path.append([last[0] + p[0], last[1] + p[1]])
    elif c == "jump":
        path.append(list(p))
    elif c == "xjump":
        path.append([p, last[1]])
    elif c == "yjump":
        path.append([last[0], p])
    elif c in ("turn", "left"):
        st["step"] = _rot(step, st["angle"] if p is None else p)
    elif c == "right":
        st["step"] = _rot(step, -(st["angle"] if p is None else p))
    elif c == "angle":
        st["angle"] = p
    elif c == "setdir":
        if _is_num(p):
            st["step"] = [_norm(step) * math.cos(math.radians(p)),
                          _norm(step) * math.sin(math.radians(p))]
        elif isinstance(p, list) and len(p) in (2, 3) and all(_is_num(x) for x in p) \
                and (len(p) == 2 or p[2] == 0):
            u = _unit(p[:2])
            st["step"] = [_norm(step) * u[0], _norm(step) * u[1]]
        else:
            raise TurtleError(f'"setdir" needs an angle or a [x, y] vector {where}')
    elif c == "length":
        u = _unit(step)
        st["step"] = [p * u[0], p * u[1]]
    elif c == "scale":
        st["step"] = [p * step[0], p * step[1]]
    elif c == "addlength":
        u = _unit(step)
        st["step"] = [step[0] + p * u[0], step[1] + p * u[1]]
    elif c == "arcsteps":
        if not (isinstance(p, int) and not isinstance(p, bool) and p >= 0):
            raise TurtleError(f'"arcsteps" needs a nonnegative integer {where}')
        st["arcsteps"] = p
    elif c in ("arcleft", "arcright"):
        if not _is_num(p):
            raise TurtleError(f'"{c}" needs a numeric radius {where}')
        if p2 is not None and not _is_num(p2):
            raise TurtleError(f'"{c}" needs a numeric angle {where}')
        angle = st["angle"] if p2 is None else p2
        lr = 1 if c == "arcleft" else -1
        radius = p * (1 if angle > 0 else -1 if angle < 0 else 0)
        normal = _unit([-step[1], step[0]])   # BOSL2's line_normal([0,0], step)
        center = [last[0] + lr * radius * normal[0], last[1] + lr * radius * normal[1]]
        if angle != 0 and radius != 0:
            steps = st["arcsteps"] or segs(abs(radius))
            sweep = (1 if p > 0 else -1) * lr * angle
            path.extend(_arc_points(last, center, sweep, steps))
        st["step"] = _rot(step, lr * angle)
    elif c in ("arcleftto", "arcrightto"):
        if not _is_num(p) or not _is_num(p2):
            raise TurtleError(f'"{c}" needs a numeric radius and angle {where}')
        lr = 1 if c == "arcleftto" else -1
        normal = _unit([-step[1], step[0]])
        center = [last[0] + lr * p * normal[0], last[1] + lr * p * normal[1]]
        start = math.degrees(math.atan2(step[1], step[0])) % 360
        end = p2 % 360
        delta = -start + (end + lr * 360 if lr * end < lr * start else end)
        if delta != 0 and p != 0:
            steps = st["arcsteps"] or segs(abs(p))
            sweep = (1 if p > 0 else -1) * delta
            path.extend(_arc_points(last, center, sweep, steps))
        st["step"] = _rot(step, delta)


def run(commands: list, start=(0.0, 0.0)):
    """`(path, spans, heading)`: the points `turtle(commands)` draws; for
    each top-level command `(first, last)`, the inclusive range of path
    indices from where it started to where it ended (equal when it drew
    nothing); and the turtle's final step vector, the way it ends up facing.
    Raises `TurtleError` where BOSL2 would assert."""
    st = {"path": [list(start)], "step": [1.0, 0.0], "angle": 90, "arcsteps": 0}
    spans = []
    for k, (c, args) in enumerate(split(commands)):
        first = len(st["path"]) - 1
        _command(c, args, st, f"at command {k}")
        spans.append((first, len(st["path"]) - 1))
    return st["path"], spans, st["step"]


# -- Drawing: commands that take the turtle to a clicked point ---------------
#
# Rounded as the editor writes them (lengths to 0.01, angles to 0.1 degree),
# so a preview drawn from these commands is exactly what a click appends.

def _round_len(v):
    return round(float(v), 2)


def _round_ang(v):
    return round(float(v), 1)


def _signed_angle(a, b) -> float:
    """Degrees from direction `a` to direction `b`, in (-180, 180]."""
    return math.degrees(math.atan2(a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1]))


def _turn(angle) -> list:
    angle = _round_ang(angle)
    return ["left", angle] if angle > 0 else ["right", -angle] if angle < 0 else []


def _snap(v, step):
    return round(v / step) * step if step else v


def line_to(p0, heading, p1, jump: bool = False,
            snap_angle: float | None = None, snap_len: float | None = None) -> list:
    """Commands turning the turtle to face `p1` from `p0` and going there:
    `left`/`right` then `move` (scaled by the step length, as `move` is), or
    with `jump` the exact point, so no rounding drift. `snap_angle` snaps the
    turn, the distance then measured along the snapped direction to the
    point nearest `p1`; `snap_len` snaps that distance. [] when nothing moves."""
    d = [p1[0] - p0[0], p1[1] - p0[1]]
    step = math.hypot(heading[0], heading[1])
    t = [heading[0] / step, heading[1] / step] if step else [1.0, 0.0]
    if math.hypot(*d) < 1e-9:
        return []
    angle = _snap(_signed_angle(t, d), snap_angle)
    u = _rot(t, angle)
    dist = _snap(d[0] * u[0] + d[1] * u[1], snap_len)
    if dist < 1e-9:
        return []
    cmds = _turn(angle)
    if jump:
        return cmds + ["jump", [_round_len(p0[0] + u[0] * dist), _round_len(p0[1] + u[1] * dist)]]
    return cmds + ["move", _round_len(dist / (step or 1))]


def tangent_arc(p0, heading, p1, snap_angle: float | None = None,
                snap_len: float | None = None) -> list:
    """The arc leaving `p0` tangent to `heading` and ending at `p1`:
    `arcleft`/`arcright` with its radius and angle. With `snap_angle`, the
    arc's angle is snapped and the arc ends on its new end ray, nearest `p1`;
    `snap_len` snaps the radius. A `move` when the arc would be straight; []
    for a point no arc reaches (on the tangent line behind the turtle)."""
    step = math.hypot(heading[0], heading[1])
    t = [heading[0] / step, heading[1] / step] if step else [1.0, 0.0]
    d = [p1[0] - p0[0], p1[1] - p0[1]]
    dist = math.hypot(*d)
    if dist < 1e-9:
        return []
    half = _signed_angle(t, d)
    if snap_angle:
        sweep = _snap(2 * half, snap_angle)
        if abs(sweep) >= 360:
            sweep = math.copysign(360 - snap_angle, sweep)
        ray = _rot(t, sweep / 2)
        chord = d[0] * ray[0] + d[1] * ray[1]
        if chord <= 0:
            return []
        half = sweep / 2
    else:
        chord = dist
    if abs(half) < 0.05:   # straight ahead: no arc, just a move
        ahead = _snap(d[0] * t[0] + d[1] * t[1], snap_len)
        return ["move", _round_len(ahead / (step or 1))] if ahead > 0 else []
    s = math.sin(math.radians(abs(half)))
    if s < 1e-6:
        return []
    radius = _round_len(_snap(chord / (2 * s), snap_len))
    if radius <= 0:
        return []
    return ["arcleft" if half > 0 else "arcright", radius, _round_ang(abs(2 * half))]


# -- The start: position from turtle()'s state=, heading from a leading setdir

def start_heading(commands: list) -> float:
    """The direction, in degrees, a leading `setdir` points the turtle; 0
    (BOSL2's +X) without one."""
    rows = split(commands)
    if rows and rows[0][0] == "setdir" and rows[0][1]:
        p = rows[0][1][0]
        if _is_num(p):
            return float(p)
        if isinstance(p, list) and len(p) >= 2 and all(_is_num(x) for x in p[:2]) and any(p[:2]):
            return math.degrees(math.atan2(p[1], p[0]))
    return 0.0


def with_start_heading(commands: list, angle: float) -> list:
    """`commands` with the turtle starting at `angle` degrees: the leading
    `setdir` changed, or one added. Turning back to 0 keeps an existing one
    (written as 0) rather than deleting a command the author wrote."""
    angle = _round_ang((angle + 180) % 360 - 180)
    if angle == 0:
        angle = 0.0   # not -0.0
    rows = split(commands)
    if rows and rows[0][0] == "setdir":
        rows[0] = ("setdir", [angle])
    elif angle != 0:
        rows.insert(0, ("setdir", [angle]))
    return join(rows)


# -- Dragging a joint: the point where a drawing command ends ---------------

_DRAW = ("move", "arcleft", "arcright", "jump")
_TURNS = ("left", "right", "turn")


def joints(commands: list) -> list[int] | None:
    """The rows of the drawing commands (`move`, `arcleft`/`arcright`,
    `jump`), whose end points are the joints a drag can move -- or None
    when the list uses anything else, whose effect a refit could not keep
    (`repeat`, `untilx`, a changed step length, `setdir` after the start)."""
    rows = split(commands)
    for i, (c, a) in enumerate(rows):
        ok = (c == "setdir" and i == 0 and len(a) == 1
              or c in _TURNS and (not a or (len(a) == 1 and _is_num(a[0])))
              or c == "move" and (not a or (len(a) == 1 and _is_num(a[0])))
              or c in ("arcleft", "arcright") and len(a) == 2 and all(_is_num(x) for x in a)
              or c == "jump" and len(a) == 1 and _is_vec2(a[0]))
        if not ok:
            return None
    return [i for i, (c, _) in enumerate(rows) if c in _DRAW]


def drag_joint(commands: list, start, j: int, target) -> list | None:
    """`commands` with joint `j` (the end of the j-th drawing command) moved
    to `target` and every later joint where it was. From joint j on, each
    drawing command is refitted to reach its joint from wherever the turtle
    now is and the way it now faces: an arc as the tangent arc there, so the
    path stays smooth; a move as a turn and a move; a jump as itself. Turns
    before an arc or jump are kept: they are corners the author made.
    None when some command cannot reach its joint (a point on the line
    behind an arc's start, say), or the list is not one `joints` accepts.

    Angles are snapped to 0.1 degree and the arc or move then ends on that
    direction, nearest its joint -- rather than rounding the angle and
    missing by the radius times the rounding, which on a long flat arc
    would be a visible miss."""
    draws = joints(commands)
    if draws is None or not 0 <= j < len(draws):
        return None
    rows = split(commands)
    path, spans, _ = run(commands, start)
    ends = [path[spans[d][1]] for d in draws]
    ends[j] = [float(target[0]), float(target[1])]
    first = draws[j - 1] + 1 if j else 0
    out = rows[:first]
    for s in range(j, len(draws)):
        d = draws[s]
        group = rows[(draws[s - 1] + 1 if s else 0):d]
        kind = rows[d][0]
        pre = [r for r in group if r[0] == "setdir"] if kind == "move" else group
        path, _, heading = run(join(out + pre), start)
        if kind == "jump":
            new = ["jump", [_round_len(ends[s][0]), _round_len(ends[s][1])]]
        elif kind == "move":
            new = line_to(path[-1], heading, ends[s], snap_angle=0.1)
        else:
            new = tangent_arc(path[-1], heading, ends[s], snap_angle=0.1)
            # tangent_arc's stand-in for "behind the turtle" when snapping:
            # a whole turn less one snap, on a huge radius. Not a refit.
            if new and new[0] != "move" and abs(new[2]) >= 359.9:
                return None
        if not new or not any(c in _DRAW for c, _ in split(new)):
            return None
        out += pre + split(new)
    return join(out)


def drag_joint_local(commands: list, start, j: int, target) -> list | None:
    """`commands` with joint `j` moved to `target` and nothing else: only
    the two drawing commands touching it change. Command j is refitted into
    `target` as `drag_joint` does; command j+1 is refitted from there back
    to its own end -- a turn and a move, the tangent arc from the new
    heading, or the same jump -- and a turn after it restores the heading
    the turtle had there, so every later command draws exactly as before.
    That turn is folded into a numeric `left`/`right` already following,
    so dragging again does not pile up turns.

    Command j+1 and the turn are written to 0.001, finer than drawing's
    rounding, since the rest of the path hangs off them. None when
    `drag_joint` would refuse, or command j+1 cannot reach its end (an arc
    to a point straight behind)."""
    draws = joints(commands)
    if draws is None or not 0 <= j < len(draws):
        return None
    if j == len(draws) - 1:
        return drag_joint(commands, start, j, target)
    rows = split(commands)
    path, spans, _ = run(commands, start)
    end = path[spans[draws[j + 1]][1]]
    h_end = run(join(rows[:draws[j + 1] + 1]), start)[2]
    head = drag_joint(join(rows[:draws[j] + 1]), start, j, target)
    if head is None:
        return None
    out = split(head)
    group = rows[draws[j] + 1:draws[j + 1]]
    c, a = rows[draws[j + 1]]
    if c == "move":
        out += [r for r in group if r[0] not in _TURNS]
    else:
        out += group
    p0, h0 = run(join(out), start)[0][-1], run(join(out), start)[2]
    step = math.hypot(*h0)
    t = [h0[0] / step, h0[1] / step]
    d = [end[0] - p0[0], end[1] - p0[1]]
    dist = math.hypot(*d)
    if c == "jump":
        out.append((c, a))
    elif dist < 1e-9:
        return None
    elif c == "move":
        turn = _fine(_signed_angle(t, d))
        out += [("left", [turn])] if turn > 0 else [("right", [-turn])] if turn < 0 else []
        out.append(("move", [_fine(dist / step)]))
    else:
        half = _signed_angle(t, d)
        if abs(half) > 179.9:
            return None
        if abs(half) < 1e-4:
            out.append(("move", [_fine(dist / step)]))
        else:
            radius = dist / (2 * math.sin(math.radians(abs(half))))
            out.append(("arcleft" if half > 0 else "arcright", [_fine(radius), _fine(abs(2 * half))]))
    back = _signed_angle(run(join(out), start)[2], h_end)
    rest = rows[draws[j + 1] + 1:]
    if rest and rest[0][0] in ("left", "right") and len(rest[0][1]) == 1 and _is_num(rest[0][1][0]):
        back += rest[0][1][0] if rest[0][0] == "left" else -rest[0][1][0]
        rest = rest[1:]
    back = _fine(back)
    out += [("left", [back])] if back > 0 else [("right", [-back])] if back < 0 else []
    return join(out + rest)


# -- Reshaping a section: split a command, or flatten a run of them (#837) ----

def _fine(v, places=3):
    v = round(float(v), places)
    return int(v) if v.is_integer() else v


def split_at(commands: list, start, row: int, point) -> list | None:
    """`commands` with row `row` cut in two where its drawing passes nearest
    `point`, each piece drawing its part of the same line or arc, so the
    path is unchanged and nothing after it moves. A `move` (with a numeric
    or no argument), an `arcleft`/`arcright` with a radius and an angle, or
    a `jump`; None for anything else, or a point at either end."""
    rows = split(commands)
    if not 0 <= row < len(rows):
        return None
    c, a = rows[row]
    if not (c == "move" and (not a or (len(a) == 1 and _is_num(a[0])))
            or c in ("arcleft", "arcright") and len(a) == 2 and all(_is_num(x) for x in a)
            or c == "jump" and len(a) == 1 and _is_vec2(a[0])):
        return None
    path, spans, _ = run(commands, start)
    pts = path[spans[row][0]:spans[row][1] + 1]
    # The nearest point on the drawn polyline, as a fraction of its length:
    # an arc's points are evenly spaced, so that is also its angle's fraction.
    lens = [math.dist(p, q) for p, q in zip(pts, pts[1:])]
    total = sum(lens)
    if total < 1e-9:
        return None
    best, along = None, 0.0
    for p, q, n in zip(pts, pts[1:], lens):
        if n:
            u = max(0.0, min(1.0, ((point[0] - p[0]) * (q[0] - p[0])
                                   + (point[1] - p[1]) * (q[1] - p[1])) / n ** 2))
            d = math.dist(point, (p[0] + u * (q[0] - p[0]), p[1] + u * (q[1] - p[1])))
            if best is None or d < best[0]:
                best = (d, (along + u * n) / total)
        along += n
    t = best[1]
    if c == "jump":
        p0, p1 = pts[0], pts[-1]
        mid = [_round_len(p0[0] + t * (p1[0] - p0[0])), _round_len(p0[1] + t * (p1[1] - p0[1]))]
        if mid in (p0, p1):
            return None
        new = [("jump", [mid]), ("jump", a)]
    else:
        whole = a[-1] if a else 1
        first = (_round_len if c == "move" else _round_ang)(whole * t)
        if not 0 < abs(first) < abs(whole):
            return None
        rest = _fine(whole - first, 6)
        new = [(c, [first]), (c, [rest])] if c == "move" else [(c, [a[0], first]), (c, [a[0], rest])]
    return join(rows[:row] + new + rows[row + 1:])


#: Commands that change state a later command reads, other than the turtle's
#: position and heading -- which a straight line could not stand in for.
_STATEFUL = ("angle", "arcsteps", "length", "scale", "addlength", "repeat")


def straighten(commands: list, start, first: int, last: int) -> list | None:
    """`commands` with rows `first`..`last` replaced by a straight line
    between where they start and end: a turn to face along it, a `move`,
    and a turn back to the way those rows left the turtle facing -- so
    nothing after them moves. Written to 0.001, finer than drawing's
    rounding, since the rest of the path hangs off it. None when the rows
    change anything else a later command reads (the step length, the
    default angle, `arcsteps`, a `repeat`)."""
    rows = split(commands)
    if not 0 <= first <= last < len(rows) or any(c in _STATEFUL for c, _ in rows[first:last + 1]):
        return None
    before = join(rows[:first])
    p0, h0 = run(before, start)[0][-1], run(before, start)[2]
    path, _, h1 = run(join(rows[:last + 1]), start)
    p1, step = path[-1], math.hypot(*h0)
    d = [p1[0] - p0[0], p1[1] - p0[1]]
    new, facing = [], h0
    if math.hypot(*d) > 1e-9:
        turn = _fine(_signed_angle(h0, d))
        new += [("left", [turn])] if turn > 0 else [("right", [-turn])] if turn < 0 else []
        facing = _rot(h0, turn)
        new.append(("move", [_fine(math.hypot(*d) / step)]))
    back = _fine(_signed_angle(facing, h1))
    new += [("left", [back])] if back > 0 else [("right", [-back])] if back < 0 else []
    return join(rows[:first] + new + rows[last + 1:])
