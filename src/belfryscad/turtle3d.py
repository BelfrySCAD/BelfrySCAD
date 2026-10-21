"""BOSL2's `turtle3d()`, ported for the 3D turtle path editor's live preview.

Follows BOSL2's `turtle3d.scad` exactly, as `turtle.py` follows its 2D
`turtle()`: the same commands and compound commands, the same argument
grouping (`_turtle3d_command_len`), the same frame transforms, and the same
arc sampling (`segs()` under OpenSCAD's default `$fa`/`$fs`). Only the path
is ported: "twist", "grow" and "shrink" change the transforms a sweep would
use, never the points, so they are checked and ignored. Qt-free.

`run()` also records which path points each top-level command drew, and
returns the turtle's final frame, which the editor draws as an arrow.
"""
from __future__ import annotations

import math

import numpy as np

from belfryscad.turtle import TurtleError, _is_num, segs

#: Every 3D turtle command, in BOSL2's documentation order. A compound
#: command is a list instead, starting with "move" or "arc".
COMMANDS = (
    "move", "xmove", "ymove", "zmove", "xyzmove", "untilx", "untily", "untilz",
    "jump", "xjump", "yjump", "zjump", "left", "right", "up", "down", "roll",
    "xrot", "yrot", "zrot", "rot", "angle", "setdir", "length", "scale",
    "addlength", "repeat", "arcleft", "arcright", "arcup", "arcdown",
    "arcxrot", "arcyrot", "arczrot", "arcrot", "arctodir", "arcsteps",
)
_ONE_OR_TWO = ("arcleft", "arcright", "arcup", "arcdown", "arczrot", "arcyrot", "arcxrot")
#: Commands whose argument, if a list, is really the next (compound) command.
_NUM_OR_NOTHING = ("move", "xmove", "ymove", "zmove", "left", "right", "up", "down",
                   "xrot", "yrot", "zrot", "roll")
_EPS = 1e-9
RIGHT, BACK, UP = np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0])


def _is_vec3(v) -> bool:
    return isinstance(v, list) and len(v) == 3 and all(_is_num(x) for x in v)


def _is_mat4(v) -> bool:
    return (isinstance(v, list) and len(v) == 4
            and all(isinstance(r, list) and len(r) == 4 and all(_is_num(x) for x in r) for r in v))


def _is_compound(c) -> bool:
    return isinstance(c, list) and bool(c) and c[0] in ("move", "arc")


# -- The rows: BOSL2's _turtle3d_command_len ---------------------------------

def split(commands: list) -> list[tuple]:
    """`[(command, args), ...]`, grouped as BOSL2 groups them; a compound
    command is its list, with no args."""
    out, i, n = [], 0, len(commands)
    while i < n:
        c = commands[i]
        if isinstance(c, list):
            if not _is_compound(c):
                raise TurtleError(f'A compound command starts with "move" or "arc", at index {i}')
            out.append((c, []))
            i += 1
            continue
        if not isinstance(c, str) or c not in COMMANDS:
            raise TurtleError(f"Unknown turtle3d command {c!r} at index {i}")
        nxt = commands[i + 1] if i + 1 < n else None
        if c in ("repeat", "arctodir", "arcrot"):
            size = 3
        elif c in _ONE_OR_TWO and n > i + 2 and not isinstance(commands[i + 2], (str, list)):
            size = 3
        elif isinstance(nxt, str) or (c in _NUM_OR_NOTHING and isinstance(nxt, list)):
            size = 1
        else:
            size = 2
        out.append((c, list(commands[i + 1:i + size])))
        i += size
    return out


def join(rows: list[tuple]) -> list:
    """The flat command list `split` came from."""
    return [x for c, args in rows for x in [c, *args]]


def is_turtle3d(value) -> bool:
    """A non-empty list that is a valid 3D turtle command list."""
    if not isinstance(value, list) or not value or not isinstance(value[0], (str, list)):
        return False
    try:
        split(value)
    except TurtleError:
        return False
    return True


def uses_3d_only(commands: list) -> bool:
    """Whether the list has anything 2D `turtle()` does not: a 3D-only
    command, a compound command, or a 3-vector where 2D takes a 2-vector."""
    from belfryscad import turtle
    for c, args in split(commands):
        if isinstance(c, list) or c not in turtle.COMMANDS:
            return True
        if c in ("jump", "setdir") and args and isinstance(args[0], list) \
                and len(args[0]) == 3 and args[0][2] != 0:
            return True
    return False


# -- Matrices, as BOSL2 builds them (degrees throughout) --------------------

def _move(v):
    m = np.eye(4)
    m[:3, 3] = v
    return m


def _rot_axis(u, ang):
    """affine3d_rot_by_axis."""
    if abs(ang) <= _EPS:
        return np.eye(4)
    u = np.asarray(u, dtype=float)
    u = u / np.linalg.norm(u)
    c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    c2 = 1 - c
    x, y, z = u
    return np.array([[x * x * c2 + c, x * y * c2 - z * s, x * z * c2 + y * s, 0],
                     [y * x * c2 + z * s, y * y * c2 + c, y * z * c2 - x * s, 0],
                     [z * x * c2 - y * s, z * y * c2 + x * s, z * z * c2 + c, 0],
                     [0, 0, 0, 1]])


def _rot(ang, axis, cp=None):
    m = _rot_axis(axis, ang) if abs(ang) > _EPS else np.eye(4)
    return m if cp is None else _move(cp) @ m @ _move(-np.asarray(cp, dtype=float))


def _axis_rot(which, ang, cp=None):
    """xrot/yrot/zrot, optionally about `cp`."""
    c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    m = np.eye(4)
    i, j = {"x": (1, 2), "y": (2, 0), "z": (0, 1)}[which]
    m[i, i], m[i, j], m[j, i], m[j, j] = c, -s, s, c
    return m if cp is None else _move(cp) @ m @ _move(-np.asarray(cp, dtype=float))


def _unit(v, default=None):
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n < _EPS:
        if default is None:
            raise TurtleError("Cannot normalize a zero vector")
        return np.asarray(default, dtype=float)
    return v / n


def _vector_angle(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return math.degrees(math.acos(max(-1.0, min(1.0, a @ b / (np.linalg.norm(a) * np.linalg.norm(b))))))


def _vector_axis(v1, v2):
    w1, w2 = _unit(v1), _unit(v2)
    eps = 1e-6
    if np.linalg.norm(w1 - w2) > eps and np.linalg.norm(w1 + w2) > eps:
        w3 = w2
    elif np.linalg.norm(np.abs(w2) - UP) > eps:
        w3 = UP
    else:
        w3 = RIGHT
    return _unit(np.cross(w1, w3))


def _rot_from_to(frm, to):
    """affine3d_rot_from_to."""
    frm, to = _unit(frm), _unit(to)
    if np.allclose(frm, to, atol=_EPS, rtol=0):
        return np.eye(4)
    if frm[2] == 0 and to[2] == 0:
        return _axis_rot("z", math.degrees(math.atan2(to[1], to[0]) - math.atan2(frm[1], frm[0])))
    return _rot_axis(_vector_axis(frm, to), _vector_angle(frm, to))


def _rot_decode(m):
    """BOSL2's rot_decode: [angle, axis, cp, translation]."""
    R = m[:3, :3]
    if abs(np.linalg.det(R) - 1) > 1e-6 or np.linalg.norm(R @ R.T - np.eye(3)) > 1e-6:
        raise TurtleError("Input matrix is not a rotation")
    translation = m[:3, 3]
    largest = int(np.argmax([R[0, 0], R[1, 1], R[2, 2]]))
    axis_matrix = R + R.T - (np.trace(R) - 1) * np.eye(3)
    q_im = axis_matrix[largest]
    q_re = R[(largest + 2) % 3, (largest + 1) % 3] - R[(largest + 1) % 3, (largest + 2) % 3]
    c_sin, c_cos = np.linalg.norm(q_im), abs(q_re)
    if c_sin <= _EPS:
        return 0.0, UP.copy(), np.zeros(3), translation
    angle = 2 * math.degrees(math.atan2(c_sin, c_cos))
    axis = (1 if q_re >= 0 else -1) * q_im / c_sin
    tproj = translation - (translation @ axis) * axis
    cp = (tproj + np.cross(axis, tproj) * c_cos / c_sin) / 2
    return angle, axis, cp, (translation @ axis) * axis


def _compute_spin(anchor_dir, spin_dir):
    native = (_rot_from_to(UP, anchor_dir) @ np.append(BACK, 0))[:3]
    spin = spin_dir - (spin_dir @ anchor_dir) * anchor_dir
    if np.allclose(spin, 0, atol=_EPS):
        raise TurtleError("Spin direction is parallel to anchor")
    sign = -1 if np.cross(native, spin) @ anchor_dir < 0 else 1
    return sign * _vector_angle(native, spin)


def _segs_angle(r, angle):
    return math.ceil(segs(r) * abs(angle) / 360 - 2e-15)


def _rotpart(T):
    R = T.copy()
    R[:3, 3] = 0
    return R


def _apply(T, p):
    return (T @ np.append(np.asarray(p, dtype=float), 1.0))[:3]


def _turn_rot(command, angle, center=None):
    """BOSL2's _turtle3d_rotation."""
    a = (-1 if command.endswith("right") or command.endswith("up") else 1) * angle
    for w in ("x", "y", "z"):
        if command.endswith(w + "rot"):
            return _axis_rot(w, a, center)
    return _axis_rot("z" if command.endswith(("right", "left")) else "y", a, center)


# -- Running ------------------------------------------------------------------

class _State:
    """BOSL2's turtle3d state, path transforms only: [transforms, step,
    angle, arcsteps]."""

    def __init__(self, T):
        self.T = [T]
        self.step, self.angle, self.arcsteps = 1.0, 90.0, 0


def initial_frame(state=None):
    """The turtle's starting transform for BOSL2's `state=`: a direction
    (default RIGHT) or a 4x4 matrix."""
    if state is None:
        state = [1, 0, 0]
    s = np.asarray(state, dtype=float)
    if s.shape == (4, 4):
        return s
    if s.shape != (3,) or np.linalg.norm(s) < _EPS:
        raise TurtleError("state= is a direction or a 4x4 transform")
    up = UP - (UP @ s) * s / (s @ s)
    z = BACK * -1 if np.linalg.norm(up) <= _EPS else up
    x = _unit(s)
    z = _unit(z)
    y = _unit(np.cross(z, x))
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2] = x, y, z
    return m


def _command(c, args, st: _State, where):
    if isinstance(c, list):
        st.T += _compound(c, st, where)
        return
    raw = args[0] if args else None
    raw2 = args[1] if len(args) > 1 else None
    if c == "repeat":
        if not (isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0):
            raise TurtleError(f'"repeat" needs a nonnegative integer count {where}')
        if not isinstance(raw2, list):
            raise TurtleError(f'"repeat" needs a command list {where}')
        rows = split(raw2)
        for _ in range(raw):
            for k, (cc, aa) in enumerate(rows):
                _command(cc, aa, st, f"{where} (repeat item {k})")
        return
    p = None if isinstance(raw, str) or (isinstance(raw, list) and c in _NUM_OR_NOTHING) else raw
    p2 = raw2 if c in ("arctodir", "arcrot") else (raw2 if not isinstance(raw2, (str, list)) else None)
    if c in ("jump", "xyzmove", "setdir") and not _is_vec3(p):
        raise TurtleError(f'"{c}" needs a 3D vector {where}')
    if c in ("untilx", "untily", "untilz", "xjump", "yjump", "zjump", "angle", "length",
             "scale", "addlength") and not _is_num(p):
        raise TurtleError(f'"{c}" needs a number {where}')
    if c in _NUM_OR_NOTHING and p is not None and not _is_num(p):
        raise TurtleError(f'"{c}" needs a number {where}')
    if c == "rot" and not _is_mat4(p):
        raise TurtleError(f'"rot" needs a 4x4 transform {where}')
    T = st.T[-1]
    last = T[:3, 3].copy()
    d = 1 if p is None else p
    if c == "move":
        st.T.append(T @ _move([d * st.step, 0, 0]))
    elif c in ("untilx", "untily", "untilz"):
        n = np.eye(3)["xyz".index(c[-1])]
        direction = _apply(T, RIGHT) - last
        denom = n @ direction
        if abs(denom) <= _EPS:
            if abs(n @ last - p) > _EPS:
                raise TurtleError(f'"{c}" never reaches {p} {where}')
            t = 0.0
        else:
            t = (p - n @ last) / denom
            if t < -_EPS:
                raise TurtleError(f'"{c}" never reaches {p} {where}')
        st.T.append(T @ _move([max(t, 0.0) * 1.0, 0, 0]))
    elif c in ("xmove", "ymove", "zmove"):
        v = np.zeros(3)
        v["xyz".index(c[0])] = d * st.step
        st.T.append(_move(v) @ T)
    elif c == "xyzmove":
        st.T.append(_move(p) @ T)
    elif c == "jump":
        st.T.append(_move(np.asarray(p, dtype=float) - last) @ T)
    elif c in ("xjump", "yjump", "zjump"):
        target = last.copy()
        target["xyz".index(c[0])] = p
        st.T.append(_move(target - last) @ T)
    elif c == "angle":
        if p == 0:
            raise TurtleError(f'"angle" needs a nonzero argument {where}')
        st.angle = p
    elif c == "length":
        st.step = p
    elif c == "scale":
        st.step = p * st.step
    elif c == "addlength":
        st.step = st.step + p
    elif c == "arcsteps":
        if not (isinstance(p, int) and not isinstance(p, bool) and p >= 0):
            raise TurtleError(f'"arcsteps" needs a nonnegative integer {where}')
        st.arcsteps = p
    elif c == "roll":
        st.T[-1] = T @ _axis_rot("x", st.angle if p is None else p)
    elif c in ("right", "left", "up", "down"):
        st.T[-1] = T @ _turn_rot(c, st.angle if p is None else p)
    elif c in ("xrot", "yrot", "zrot", "rot", "setdir"):
        R = _rotpart(T)
        if c == "rot":
            turn = np.asarray(p, dtype=float)
        elif c == "setdir":
            turn = _rot_from_to(_apply(R, RIGHT), p)
        else:
            turn = _turn_rot(c, st.angle if p is None else p)
        st.T[-1] = _move(last) @ turn @ R
    elif c in ("arcleft", "arcright", "arcup", "arcdown"):
        if not _is_num(p):
            raise TurtleError(f'"{c}" needs a numeric radius {where}')
        radius = st.step * p
        angle = st.angle if p2 is None else p2
        if not _is_num(angle) or angle == 0:
            raise TurtleError(f'"{c}" needs a nonzero angle {where}')
        sgn = math.copysign(1, angle)
        center = sgn * np.array([0.0,
                                 radius if c == "arcleft" else -radius if c == "arcright" else 0,
                                 -radius if c == "arcdown" else radius if c == "arcup" else 0])
        steps = st.arcsteps or segs(abs(radius))
        st.T += [T @ _turn_rot(c, angle * n / steps, center) for n in range(1, steps + 1)]
    elif c in ("arcxrot", "arcyrot", "arczrot", "arctodir", "arcrot"):
        if not _is_num(p):
            raise TurtleError(f'"{c}" needs a numeric radius {where}')
        radius = st.step * p
        R = _rotpart(T)
        v = _apply(R, RIGHT)
        if c in ("arctodir", "arcrot"):
            if c == "arctodir" and not _is_vec3(p2):
                raise TurtleError(f'"arctodir" needs a direction vector {where}')
            if c == "arcrot" and not _is_mat4(p2):
                raise TurtleError(f'"arcrot" needs a 4x4 transform {where}')
            angle, axis, _, _ = _rot_decode(_rot_from_to(v, p2) if c == "arctodir"
                                            else np.asarray(p2, dtype=float))
            length = 2 * math.pi * radius * angle / 360
        else:
            angle = st.angle if p2 is None else p2
            if not _is_num(angle) or angle == 0:
                raise TurtleError(f'"{c}" needs a nonzero angle {where}')
            axis = np.eye(3)["xyz".index(c[3])]
            length = 2 * math.pi * radius * abs(angle) / 360
        projv = v - (axis @ v) * axis
        if np.allclose(projv, 0, atol=_EPS):
            raise TurtleError(f"The rotation acts as a twist, not an arc, {where}")
        sgn = 1 if c in ("arctodir", "arcrot") else math.copysign(1, angle)
        center = sgn * radius * _unit(np.cross(axis, projv), np.zeros(3))
        vshift = axis * (axis @ v / np.linalg.norm(projv)) * length
        steps = st.arcsteps or segs(abs(radius))
        st.T += [_move(last + vshift * n / steps)
                 @ (_rot(n / steps * angle, axis, center) if c in ("arctodir", "arcrot")
                    else _turn_rot(c, angle * n / steps, center))
                 @ R for n in range(1, steps + 1)]


_MOVE_KEYS = {"move": 0, "twist": 0, "grow": 1, "shrink": 1, "steps": 0, "roll": 0,
              "lrollto": 0, "rrollto": 0, "rollto": 0}
_ARC_KEYS = {"arc": 0, "up": 0, "down": 0, "left": 0, "right": 0, "twist": 0, "grow": 1,
             "shrink": 1, "steps": 0, "roll": 0, "lrollto": 0, "rrollto": 0, "rollto": 0,
             "rot": 0, "todir": 0, "xrot": 0, "yrot": 0, "zrot": 0}


def _compound(command, st: _State, where) -> list:
    """BOSL2's _turtle3d_list_command: the transforms one compound command adds."""
    rev = [i for i, x in enumerate(command) if x == "reverse"]
    if len(rev) > 1:
        raise TurtleError(f'Only one "reverse" is allowed {where}')
    if rev and rev[0] % 2:
        raise TurtleError(f"Error processing compound command {where}")
    for word in ("arc", "move"):
        if any(x == word for x in command[1:]):
            raise TurtleError(f'"move" or "arc" must begin the compound command {where}')
    cmd = [x for i, x in enumerate(command) if i not in rev]
    if len(cmd) % 2:
        raise TurtleError(f"Odd number of entries in the compound command {where}")
    keys = dict(_MOVE_KEYS if cmd[0] == "move" else _ARC_KEYS)
    seen = set()
    for k, val in zip(cmd[::2], cmd[1::2]):
        if k not in keys or k in seen:
            raise TurtleError(f"Unknown or repeated key {k!r} in the compound command {where}")
        seen.add(k)
        keys[k] = val
    T = st.T[-1]
    move = st.step * keys["move"] if cmd[0] == "move" else 0
    flip = np.diag([-1.0, 1, 1, 1]) if rev else np.eye(4)
    radius = st.step * (keys.get("arc") or 0)
    if not _is_num(radius):
        raise TurtleError(f'"arc" needs a numeric radius {where}')
    twist = keys["twist"]
    for g in ("grow", "shrink"):
        v = keys[g]
        if not (_is_num(v) or (isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v))):
            raise TurtleError(f'"{g}" needs a number or a 2D vector {where}')
    usersteps = keys["steps"]
    right, left, up, down = (keys.get(k, 0) or 0 for k in ("right", "left", "up", "down"))
    if cmd[0] == "arc":
        if not all(_is_num(x) for x in (right, left, up, down)):
            raise TurtleError(f'"left", "right", "up" and "down" need numbers {where}')
        if right and left:
            raise TurtleError(f'Cannot give both "left" and "right" {where}')
        if up and down:
            raise TurtleError(f'Cannot give both "up" and "down" {where}')
        if not ((up - down == 0 or abs(left - right) < 180) and (left - right == 0 or abs(up - down) < 180)):
            raise TurtleError(f"Mixed angles must all be below 180 {where}")
    lr, du = left - right, down - up
    newdir = _apply(_axis_rot("z", lr) @ _axis_rot("y", du), RIGHT)
    relaxis = BACK if lr == 0 else UP if du == 0 else np.cross(RIGHT, newdir)
    angle = (0 if cmd[0] == "move" else
             du + lr if lr == 0 or du == 0 else _vector_angle(RIGHT, newdir))
    center = -radius * (np.array([0, 0, math.copysign(1, du) if du else 0.0]) if lr == 0
                        else np.array([0, math.copysign(1, -lr), 0.0]) if du == 0
                        else _unit(np.cross(RIGHT, np.cross(RIGHT, newdir)), np.zeros(3)))
    R = _rotpart(T)
    shift = T[:3, 3].copy()
    v = _apply(R, RIGHT)
    absangle, absaxis = None, np.zeros(3)
    if cmd[0] == "arc":
        given = [k for k in ("xrot", "yrot", "zrot", "rot", "todir") if keys[k] != 0]
        if len(given) > 1:
            raise TurtleError(f'Only one of "xrot", "yrot", "zrot", "rot" and "todir" {where}')
        if given:
            k = given[0]
            if k == "rot":
                if not _is_mat4(keys["rot"]):
                    raise TurtleError(f'"rot" needs a 4x4 transform {where}')
                absangle, absaxis, _, _ = _rot_decode(np.asarray(keys["rot"], dtype=float))
            elif k == "todir":
                if not _is_vec3(keys["todir"]):
                    raise TurtleError(f'"todir" needs a 3D vector {where}')
                absangle, absaxis, _, _ = _rot_decode(_rot_from_to(v, keys["todir"]))
            else:
                if not _is_num(keys[k]):
                    raise TurtleError(f'"{k}" needs a number {where}')
                absangle, absaxis = keys[k], np.eye(3)["xyz".index(k[0])]
    projv = v - (absaxis @ v) * absaxis
    abscenter = vshift = None
    if absangle is not None:
        abscenter = math.copysign(1, absangle) * radius * _unit(np.cross(absaxis, projv), np.zeros(3))
        nproj = np.linalg.norm(projv)
        slope = absaxis @ v / nproj if nproj > 0 else 0
        vshift = absaxis * slope * 2 * math.pi * radius * abs(absangle) / 360
        if absangle == 0:
            raise TurtleError(f"Arc rotation with zero angle {where}")
        if angle != 0:
            raise TurtleError(f"Mixed relative and absolute rotations {where}")
        if np.allclose(projv, 0, atol=_EPS):
            raise TurtleError(f"The rotation acts as a twist, not an arc, {where}")
    if not (isinstance(usersteps, int) and not isinstance(usersteps, bool) and usersteps >= 0):
        raise TurtleError(f"Steps value {usersteps} invalid {where}")
    if cmd[0] == "arc" and (absangle if absangle is not None else angle) == 0:
        raise TurtleError(f'"arc" without the type and angle of its turn {where}')
    rollval, rrollto, lrollto, rollto = (keys[k] for k in ("roll", "rrollto", "lrollto", "rollto"))
    if sum(x != 0 for x in (rollval, rrollto, lrollto, rollto)) > 1:
        raise TurtleError(f"Only one of roll, rollto, rrollto and lrollto {where}")
    for x in (rrollto, lrollto, rollto):
        if x != 0 and not _is_vec3(x):
            raise TurtleError(f"rollto, rrollto and lrollto need 3D vectors {where}")
    if absangle is None:
        finalT = T @ flip @ _move([move, 0, 0]) @ (np.eye(4) if angle == 0 else _rot(angle, relaxis, center))
    else:
        finalT = _move(shift + vshift) @ _rot(absangle, absaxis, abscenter) @ R
    if rollval != 0:
        roll = rollval
    elif rrollto == 0 and lrollto == 0 and rollto == 0:
        roll = 0
    else:
        finaldir = _unit(_apply(_rotpart(finalT), RIGHT))
        finalup = _apply(_rotpart(finalT), UP)
        desired = np.asarray(rollto if rollto != 0 else rrollto if rrollto != 0 else lrollto, dtype=float)
        if abs(abs(_unit(desired) @ finaldir) - 1) <= _EPS:
            raise TurtleError(f"The roll is impossible: its direction is the turtle's travel direction {where}")
        delta = (_compute_spin(finaldir, desired) - _compute_spin(finaldir, finalup)) % 360
        roll = (delta if rrollto != 0 or delta == 0
                else delta - 360 if lrollto != 0 or delta > 180 else delta)
    effective = absangle if absangle is not None else angle
    if usersteps == 0 and cmd[0] == "move" and roll == 0 and twist == 0:
        steps = 1
    elif usersteps:
        steps = usersteps
    elif st.arcsteps:
        steps = st.arcsteps
    elif radius > 0 and effective != 0:
        steps = _segs_angle(radius, effective)
    else:
        steps = 5
    out = []
    for n in range(1, steps + 1):
        f = n / steps
        if absangle is None:
            m = T @ flip @ _move([f * move, 0, 0]) @ (np.eye(4) if angle == 0 else _rot(f * angle, relaxis, center))
        else:
            m = _move(shift + vshift * f) @ _rot(f * absangle, absaxis, abscenter) @ R
        out.append(m @ _axis_rot("x", f * roll))
    return out


def run(commands: list, state=None):
    """`(path, spans, frame, step)`: the points `turtle3d(commands,
    state=state)` draws, deduplicated as BOSL2 does; for each top-level
    command `(first, last)`, the inclusive range of path indices from where
    it started to where it ended; the turtle's final 4x4 frame; and its
    final step length. Raises `TurtleError` where BOSL2 would assert."""
    st = _State(initial_frame(state))
    raw_spans = []
    for k, (c, args) in enumerate(split(commands)):
        first = len(st.T) - 1
        _command(c, args, st, f"at command {k}")
        raw_spans.append((first, len(st.T) - 1))
    pts = [T[:3, 3] for T in st.T]
    # deduplicate(): of a run of equal points, the LAST is kept.
    keep, index = [], [0] * len(pts)
    for i, p in enumerate(pts):
        if i == len(pts) - 1 or np.max(np.abs(p - pts[i + 1])) > _EPS:
            keep.append(i)
    j = 0
    for i in range(len(pts)):
        while keep[j] < i:
            j += 1
        index[i] = j
    path = [[float(x) for x in pts[i]] for i in keep]
    return path, [(index[a], index[b]) for a, b in raw_spans], st.T[-1], st.step


# -- Drawing: commands that take the turtle to a clicked point ---------------
#
# Lengths are written to 0.01 and directions to 4 decimals, so a preview drawn
# from these commands is exactly what a click appends.

def _vec(v):
    return [round(float(x), 4) + 0.0 for x in v]


def line_to(frame, step: float, p1, jump: bool = False) -> list:
    """Commands taking the turtle from where `frame` puts it to `p1`:
    `setdir` toward it and `move`, or with `jump` the exact point. []
    when nothing moves."""
    p0 = frame[:3, 3]
    d = np.asarray(p1, dtype=float) - p0
    dist = float(np.linalg.norm(d))
    if dist < 1e-9:
        return []
    if jump:
        return ["jump", [round(float(x), 2) + 0.0 for x in p1]]
    cmds = []
    if np.linalg.norm(_unit(d) - _unit(frame[:3, 0])) > 1e-6:
        cmds = ["setdir", _vec(_unit(d))]
    return cmds + ["move", round(dist / (step or 1), 2)]


def tangent_arc(frame, step: float, p1) -> list:
    """The arc leaving the turtle tangent to its heading and ending at `p1`,
    in the plane of the heading and `p1`: one `arctodir` for a point ahead,
    two half arcs for one beside or behind (a single `arctodir` takes the
    shortest turn, which cannot reach those). A `move` for a point straight
    ahead; [] for one straight behind, which no arc reaches."""
    p0, t = frame[:3, 3], _unit(frame[:3, 0])
    d = np.asarray(p1, dtype=float) - p0
    dist = float(np.linalg.norm(d))
    if dist < 1e-9:
        return []
    perp = d - (d @ t) * t
    if np.linalg.norm(perp) < 1e-6 * dist:
        return ["move", round(float(d @ t) / (step or 1), 2)] if d @ t > 0 else []
    w = _unit(perp)
    sweep = 2 * _vector_angle(t, d)

    def toward(a):
        return _vec(math.cos(math.radians(a)) * t + math.sin(math.radians(a)) * w)

    r = round(dist / (2 * math.sin(math.radians(sweep / 2))) / (step or 1), 2)
    if sweep < 180 - 1e-6:
        return ["arctodir", r, toward(sweep)]
    return ["arctodir", r, toward(sweep / 2), "arctodir", r, toward(sweep)]


def start_direction(commands: list) -> list | None:
    """The direction a leading `setdir` points the turtle, or None."""
    rows = split(commands)
    if rows and rows[0][0] == "setdir" and rows[0][1] and _is_vec3(rows[0][1][0]):
        return [float(x) for x in rows[0][1][0]]
    return None


def with_start_direction(commands: list, direction) -> list:
    """`commands` starting with `setdir` to `direction`: the leading one
    changed, or one added."""
    rows = split(commands)
    row = ("setdir", [_vec(_unit(direction))])
    if rows and rows[0][0] == "setdir":
        rows[0] = row
    else:
        rows.insert(0, row)
    return join(rows)
