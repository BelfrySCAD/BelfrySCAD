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
