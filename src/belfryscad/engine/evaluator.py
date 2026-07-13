"""
AST evaluator: walks the openscad_lalr_parser AST and produces Manifold geometry.
Returns (manifold_body, id_to_node, colored_meshes) or raises EvalError.
"""
from __future__ import annotations
import math
import random
import threading
from itertools import product as _product
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field, replace

import manifold3d as m3d
import numpy as np
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import BasePen
from PySide6.QtGui import QColor
from shapely_polyskel import skeletonize

from openscad_lalr_parser import to_openscad, findLibraryFile, getASTfromFile, build_scopes
from openscad_lalr_parser.nodes import (
    ASTNode, Assignment, Identifier,
    NumberLiteral, BooleanLiteral, StringLiteral, UndefinedLiteral,
    CommentedExpr,
    ListComprehension, ListCompFor, ListCompCFor, ListCompIf, ListCompIfElse, ListCompLet, ListCompEach,
    PositionalArgument, NamedArgument,
    AdditionOp, SubtractionOp, MultiplicationOp, DivisionOp, ModuloOp, ExponentOp,
    UnaryMinusOp,
    LogicalAndOp, LogicalOrOp, LogicalNotOp,
    EqualityOp, InequalityOp, GreaterThanOp, GreaterThanOrEqualOp, LessThanOp, LessThanOrEqualOp,
    TernaryOp,
    PrimaryCall, PrimaryIndex, PrimaryMember,
    RangeLiteral,
    ModularCall, ModularIf, ModularIfElse, ModularFor, ModularLet,
    ModularEcho, ModularAssert, ModularIntersectionFor,
    ModularModifierShowOnly, ModularModifierHighlight,
    ModularModifierBackground, ModularModifierDisable,
    ModuleDeclaration, FunctionDeclaration, ParameterDeclaration,
    UseStatement,
    VectorElement,
    LetOp, EchoOp, AssertOp,
    FunctionLiteral,
)


class EvalError(Exception):
    pass


def _is_flat_numeric(v):
    if not v:
        return False
    for x in v:
        t = type(x)
        if t is not int and t is not float:
            return False
    return True


# numpy array creation has ~3-5µs fixed overhead; list comprehensions
# cost ~30ns/element.  Crossover is around 100-200 elements.
_NP_VEC_THRESHOLD = 128


def _scale(scalar, value):
    if type(value) is list:
        if _is_flat_numeric(value):
            if len(value) >= _NP_VEC_THRESHOLD:
                return (scalar * np.asarray(value)).tolist()
            return [scalar * x for x in value]
        return [_scale(scalar, v) for v in value]
    if type(scalar) is bool or type(value) is bool:
        return None
    try:
        return scalar * value
    except TypeError:
        return None


def _div_scale(value, divisor):
    if type(value) is list:
        if _is_flat_numeric(value):
            if len(value) >= _NP_VEC_THRESHOLD:
                arr = np.asarray(value, dtype=np.float64)
                if divisor == 0:
                    return np.where(arr == 0, np.nan, np.copysign(np.inf, arr)).tolist()
                return (arr / divisor).tolist()
            if divisor == 0:
                return [float('nan') if x == 0 else math.copysign(float('inf'), x) for x in value]
            return [x / divisor for x in value]
        return [_div_scale(v, divisor) for v in value]
    if type(value) is bool:
        return None
    try:
        if divisor == 0:
            return float('nan') if value == 0 else math.copysign(float('inf'), value)
        return value / divisor
    except TypeError:
        return None


def _vec_add(a, b):
    if type(a) is list and type(b) is list:
        if _is_flat_numeric(a) and _is_flat_numeric(b):
            if len(a) >= _NP_VEC_THRESHOLD:
                n = min(len(a), len(b))
                return (np.asarray(a[:n]) + np.asarray(b[:n])).tolist()
            return [x + y for x, y in zip(a, b)]
        return [_vec_add(x, y) for x, y in zip(a, b)]
    if type(a) is bool or type(b) is bool:
        return None
    if type(a) is str or type(b) is str:
        return None
    try:
        return a + b
    except TypeError:
        return None


def _point_seg_dist(p, a, b):
    """Euclidean distance from 2D point `p` to segment `a`-`b`."""
    ab = b - a
    denom = np.dot(ab, ab)
    t = np.dot(p - a, ab) / denom if denom else 0.0
    t = max(0.0, min(1.0, t))
    return float(np.linalg.norm(p - (a + t * ab)))


def _point_in_poly_evenodd(p, edges):
    """Even-odd ray-casting point-in-polygon test against a flat list of (a, b) edges."""
    x, y = p
    inside = False
    for a, b in edges:
        x1, y1 = a
        x2, y2 = b
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


# ---------------------------------------------------------------------------
# Straight-skeleton roof() helpers
# ---------------------------------------------------------------------------

_ROOF_MITER_LIMIT = 1e5


def _ccw_polygon(poly: np.ndarray) -> np.ndarray:
    """Return `poly` (Nx2) reordered to counter-clockwise winding."""
    n = len(poly)
    area2 = sum(poly[k][0] * poly[(k + 1) % n][1] - poly[(k + 1) % n][0] * poly[k][1] for k in range(n))
    return poly[::-1].copy() if area2 < 0 else poly


def _ear_clip(poly: np.ndarray) -> list[tuple[int, int, int]]:
    """Ear-clipping triangulation of a simple CCW polygon (may be concave).

    Returns CCW index triples into `poly`. Raises RuntimeError if no ear can
    be found (degenerate/self-intersecting input).
    """
    n = len(poly)
    idx = list(range(n))

    def is_convex(a, b, c):
        ax, ay = poly[a]
        bx, by = poly[b]
        cx, cy = poly[c]
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) > 0

    def point_in_tri(p, a, b, c):
        def sign(p1, p2, p3):
            return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
        d1, d2, d3 = sign(p, a, b), sign(p, b, c), sign(p, c, a)
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))

    tris = []
    while len(idx) > 3:
        n = len(idx)
        for i in range(n):
            a, b, c = idx[(i - 1) % n], idx[i], idx[(i + 1) % n]
            if not is_convex(a, b, c):
                continue
            if any(point_in_tri(poly[j], poly[a], poly[b], poly[c]) for j in idx if j not in (a, b, c)):
                continue
            tris.append((a, b, c))
            idx.pop(i)
            break
        else:
            raise RuntimeError("ear clipping failed")
    tris.append((idx[0], idx[1], idx[2]))
    return tris


def _miter_vertex_velocities(poly: np.ndarray) -> np.ndarray:
    """Per-vertex velocity under `offset(-d, Miter)`: moving `poly[k]` by
    `d * v_k` reproduces the mitered inward offset by `d`.
    """
    n = len(poly)
    vel = np.zeros((n, 2))
    for k in range(n):
        prev_dir = poly[k] - poly[(k - 1) % n]
        next_dir = poly[(k + 1) % n] - poly[k]
        prev_dir = prev_dir / np.linalg.norm(prev_dir)
        next_dir = next_dir / np.linalg.norm(next_dir)
        n1 = np.array([-prev_dir[1], prev_dir[0]])
        n2 = np.array([-next_dir[1], next_dir[0]])
        denom = 1 + np.dot(n1, n2)
        vel[k] = (n1 + n2) / denom
    return vel


def _offset_collapse_distance(cs: m3d.CrossSection, d_hi: float, tol: float) -> float:
    """Binary search for the largest `d` in `[0, d_hi]` where the mitered
    inward offset of `cs` by `d` still has positive area."""
    lo, hi = 0.0, d_hi
    for _ in range(40):
        mid = (lo + hi) / 2
        area = cs.offset(-mid, m3d.JoinType.Miter, _ROOF_MITER_LIMIT).area()
        if area > tol:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _offset_is_stable(cs: m3d.CrossSection, d_max: float, n: int) -> bool:
    """True if the mitered offset of `cs` stays a single `n`-vertex polygon
    for a range of distances up to `d_max` (i.e. no intermediate
    collapse/split events)."""
    for f in (0.25, 0.5, 0.75, 0.9):
        polys = cs.offset(-d_max * f, m3d.JoinType.Miter, _ROOF_MITER_LIMIT).to_polygons()
        if len(polys) != 1 or len(polys[0]) != n:
            return False
    return True


def _skeleton_roof(cs: m3d.CrossSection) -> Optional[m3d.Manifold]:
    """Build an exact straight-skeleton roof for a simple polygon whose
    mitered offset collapses to a point/ridge with no intermediate topology
    events. Returns None if `cs` doesn't qualify (multi-contour, degenerate,
    or an unstable/multi-event collapse) or mesh construction fails.
    """
    try:
        polys = cs.to_polygons()
        if len(polys) != 1:
            return None
        p0 = _ccw_polygon(np.asarray(polys[0], dtype=np.float64))
        n = len(p0)
        if n < 3:
            return None

        minx, miny, maxx, maxy = cs.bounds()
        d_hi = max(maxx - minx, maxy - miny)
        if d_hi <= 0:
            return None
        tol = (d_hi ** 2) * 1e-12
        d_max = _offset_collapse_distance(cs, d_hi, tol)
        if d_max <= 0:
            return None
        if not _offset_is_stable(cs, d_max, n):
            return None

        vel = _miter_vertex_velocities(p0)
        p1 = p0 + d_max * vel

        raw_verts = [(p[0], p[1], 0.0) for p in p0] + [(p[0], p[1], d_max) for p in p1]
        merge_tol = 1e-4
        final_verts: list[tuple[float, float, float]] = []
        idx_map: dict[int, int] = {}
        for i, v in enumerate(raw_verts):
            matched = None
            for ridx, rv in enumerate(final_verts):
                if abs(rv[0] - v[0]) < merge_tol and abs(rv[1] - v[1]) < merge_tol and abs(rv[2] - v[2]) < merge_tol:
                    matched = ridx
                    break
            if matched is None:
                matched = len(final_verts)
                final_verts.append(v)
            idx_map[i] = matched

        tris = []
        for (i, j, k) in _ear_clip(p0):
            tris.append((idx_map[k], idx_map[j], idx_map[i]))
        for k in range(n):
            k1 = (k + 1) % n
            a, b, c, d = idx_map[k], idx_map[k1], idx_map[n + k1], idx_map[n + k]
            if c == d:
                tris.append((a, b, c))
            else:
                tris.append((a, b, c))
                tris.append((a, c, d))

        mesh = m3d.Mesh(
            vert_properties=np.array(final_verts, dtype=np.float32),
            tri_verts=np.array(tris, dtype=np.uint32),
        )
        body = m3d.Manifold(mesh)
        if body.status() != m3d.Error.NoError or body.is_empty():
            return None
        return body
    except Exception:
        return None


def _build_skeleton_graph_with_holes(
    p0: np.ndarray,
    hole_arrays: list[np.ndarray],
) -> Optional[tuple]:
    """Build the planar straight-skeleton graph for CCW outer polygon `p0`
    with zero or more CW hole polygons.

    polyskel winding convention (y-axis down): outer must be CW-in-math
    (so pass `p0[::-1]`); holes must be CCW-in-math (so pass each
    `hole[::-1]` since holes from manifold are CW-in-math).

    Returns `(heights, adjacency, p0_keys, hole_keys_list, key_fn, degenerate_holes)`
    or None.
      heights          : position-key → offset-distance (0 on boundary)
      adjacency        : position-key → [neighbour keys]  (undirected)
      p0_keys          : keys for p0 vertices in traversal order
      hole_keys_list   : list of key lists, one list per hole in order
      key_fn           : snap function `(x, y) → position-key`
      degenerate_holes : list of bool, parallel to hole_keys_list — True for
                         holes whose skeleton was computed in isolation (no
                         connection from the main polyskel run), meaning their
                         boundary must be traced in reversed order (see
                         `_skeleton_roof_component`)
    """
    try:
        all_pts = np.vstack([p0] + hole_arrays) if hole_arrays else p0
        d_hi = max(all_pts[:, 0].max() - all_pts[:, 0].min(),
                   all_pts[:, 1].max() - all_pts[:, 1].min())
        if d_hi <= 0:
            return None
        tol = d_hi * 1e-6

        heights: dict[tuple, float] = {}

        def key(x, y):
            for k in heights:
                if abs(k[0] - x) < tol and abs(k[1] - y) < tol:
                    return k
            return (float(x), float(y))

        adjacency: dict[tuple, list] = {}

        def add_edge(a, b):
            if a != b:
                adjacency.setdefault(a, [])
                adjacency.setdefault(b, [])
                if b not in adjacency[a]:
                    adjacency[a].append(b)
                if a not in adjacency[b]:
                    adjacency[b].append(a)

        # Outer polygon boundary (CCW in math)
        n0 = len(p0)
        p0_keys = []
        for x, y in p0:
            k = key(x, y)
            heights[k] = 0.0
            adjacency.setdefault(k, [])
            p0_keys.append(k)
        for i in range(n0):
            add_edge(p0_keys[i], p0_keys[(i + 1) % n0])

        # Hole boundaries (CW in math)
        hole_keys_list: list[list] = []
        for hole in hole_arrays:
            nh = len(hole)
            hkeys = []
            for x, y in hole:
                k = key(x, y)
                heights[k] = 0.0
                adjacency.setdefault(k, [])
                hkeys.append(k)
            for i in range(nh):
                add_edge(hkeys[i], hkeys[(i + 1) % nh])
            hole_keys_list.append(hkeys)

        # polyskel: outer as CW-in-math, holes as CCW-in-math
        outer_pts = [(float(x), float(y)) for x, y in p0[::-1]]
        holes_pts = [[(float(x), float(y)) for x, y in h[::-1]] for h in hole_arrays]
        # polyskel can hang (infinite loop) on degenerate polygon configurations
        # (e.g. exact axis-aligned vertices that trigger numerical edge cases in
        # the skeleton sweep algorithm).  Run it in a daemon thread and abort if
        # it doesn't finish in time; then retry with a tiny deterministic jitter
        # to break the degeneracy, which typically lets polyskel converge.
        import threading as _threading
        import random as _random

        def _run_skeletonize(outer, holes, timeout=2.0):
            _res: list = [None]
            def _run():
                _res[0] = skeletonize(outer, holes if holes else None)
            _t = _threading.Thread(target=_run, daemon=True)
            _t.start()
            _t.join(timeout=timeout)
            return None if _t.is_alive() else _res[0]

        subtrees = _run_skeletonize(outer_pts, holes_pts)
        if subtrees is None:
            # Retry with a tiny deterministic jitter to break numerical degeneracy.
            _rng = _random.Random(0xBEEF)
            _j = tol * 0.1  # < tol so key() snaps back; still breaks numeric degeneracy
            outer_jit = [(x + _rng.uniform(-_j, _j), y + _rng.uniform(-_j, _j))
                         for x, y in outer_pts]
            holes_jit = [[(x + _rng.uniform(-_j, _j), y + _rng.uniform(-_j, _j))
                          for x, y in h] for h in holes_pts]
            subtrees = _run_skeletonize(outer_jit, holes_jit)
        if not subtrees:
            return None

        for st in subtrees:
            s = key(st.source.x, st.source.y)
            heights[s] = st.height
            adjacency.setdefault(s, [])
            # Group sinks by angle from source.  When polyskel places multiple
            # sinks on the same ray (collinear), adding all as direct edges
            # creates same-angle neighbour pairs that confuse _trace_face's
            # angle-sort.  Instead chain them: add source→closest only, then
            # closest→next, … so the path is a sequence of short hops.
            by_angle: dict[float, list] = {}
            for sink in st.sinks:
                t = key(sink.x, sink.y)
                if t == s:
                    continue  # skip self-loop sinks
                heights.setdefault(t, 0.0)
                adjacency.setdefault(t, [])
                dx, dy = t[0] - s[0], t[1] - s[1]
                ang = round(math.atan2(dy, dx), 9)
                dist2 = dx * dx + dy * dy
                by_angle.setdefault(ang, []).append((dist2, t))
            for ang, group in by_angle.items():
                group.sort()  # ascending distance
                # Connect source → closest (one short hop).
                prev = s
                for _, t in group:
                    add_edge(prev, t)
                    prev = t  # chain: each step only goes one hop further

        # Post-process: resolve same-angle neighbour pairs that arise when two
        # different subtrees share the same source vertex but each contributes a
        # sink along the same ray.  The per-subtree chain above only fixes
        # within-subtree duplicates; this pass fixes the cross-subtree case.
        # For each vertex V with two neighbours A (closer) and B (farther) at
        # the same angle, replace the V→B shortcut with a chain hop A→B and
        # remove V→B.  Repeat until the graph is stable.
        changed = True
        while changed:
            changed = False
            for v in list(adjacency):
                by_ang: dict[float, list] = {}
                for w in list(adjacency[v]):
                    dx, dy = w[0] - v[0], w[1] - v[1]
                    ang = round(math.atan2(dy, dx), 9)
                    dist2 = dx * dx + dy * dy
                    by_ang.setdefault(ang, []).append((dist2, w))
                for ang, group in by_ang.items():
                    if len(group) < 2:
                        continue
                    group.sort()
                    changed = True
                    prev = v
                    for i, (_, w) in enumerate(group):
                        if i == 0:
                            prev = w
                            continue
                        if w in adjacency.get(v, []):
                            adjacency[v].remove(w)
                        if v in adjacency.get(w, []):
                            adjacency[w].remove(v)
                        add_edge(prev, w)
                        prev = w

        # Post-process: for each hole whose boundary vertices have no interior
        # skeleton connections, compute the hole's isolated straight skeleton
        # and inject it.  polyskel sometimes fails to generate wavefront events
        # for a hole when the hole is a simple degenerate shape (e.g. a triangular
        # counter whose three bisectors converge simultaneously), producing no
        # skeleton vertices attached to the hole.  Without an interior apex, all
        # three hole-edge face traces cycle around the same flat triangle, making
        # each hole edge appear four times in the final mesh → NotManifold.
        # Running polyskel on the hole in isolation always works (e.g. gives the
        # incenter for a triangle) and produces the correct roof faces.
        #
        # The isolated skeleton is computed by treating the hole polygon as its
        # own mini *outer* polygon (CW-in-math, matching polyskel's convention),
        # so its interior apex sits on the opposite side from a normal hole's
        # skeleton (which faces the surrounding solid material). Consequently
        # `_trace_face` must walk these particular holes' boundary edges in
        # *reversed* (CCW) order — like an outer boundary trace — instead of the
        # natural CW order used for holes with a genuine connection to the main
        # skeleton. `degenerate_holes` records which holes need this treatment.
        degenerate_holes: list[bool] = [False] * len(hole_keys_list)
        for hidx, (hole_arr, hkeys) in enumerate(zip(hole_arrays, hole_keys_list)):
            hole_key_set = set(hkeys)
            has_interior = any(
                heights.get(w, 0.0) > 0.0
                for v in hkeys
                for w in adjacency.get(v, [])
                if w not in hole_key_set
            )
            if has_interior:
                continue
            degenerate_holes[hidx] = True
            # Hole vertices are CW-in-math = what polyskel expects for an outer polygon.
            hole_pts_iso = [(float(x), float(y)) for x, y in hole_arr]
            iso = _run_skeletonize(hole_pts_iso, [])
            if not iso:
                continue
            for st in iso:
                s = key(st.source.x, st.source.y)
                heights[s] = st.height
                adjacency.setdefault(s, [])
                by_angle_iso: dict[float, list] = {}
                for sink in st.sinks:
                    t = key(sink.x, sink.y)
                    if t == s:
                        continue
                    heights.setdefault(t, 0.0)
                    adjacency.setdefault(t, [])
                    dx, dy = t[0] - s[0], t[1] - s[1]
                    ang = round(math.atan2(dy, dx), 9)
                    dist2 = dx * dx + dy * dy
                    by_angle_iso.setdefault(ang, []).append((dist2, t))
                for ang, group in by_angle_iso.items():
                    group.sort()
                    prev = s
                    for _, t in group:
                        add_edge(prev, t)
                        prev = t

        return heights, adjacency, p0_keys, hole_keys_list, key, degenerate_holes
    except Exception:
        return None


def _trace_face(adjacency: dict, u: tuple, v: tuple) -> Optional[list]:
    """Trace the bounded face to the left of directed edge `(u, v)` in
    `adjacency` (a CCW polygon's boundary edge `u -> v` keeps the polygon's
    interior, and thus this roof face, on its left). At each vertex, the next
    edge is the neighbor immediately before the incoming vertex in
    angle-sorted (CCW) order, i.e. the next edge clockwise.

    Returns the ordered list of face-vertex positions, or `None` if the trace
    doesn't close within a bounded number of steps.
    """
    start = (u, v)
    face = [u]
    cur_u, cur_v = u, v
    for _ in range(2 * len(adjacency) + 4):
        face.append(cur_v)
        neighbors = adjacency.get(cur_v)
        if not neighbors or len(neighbors) < 2:
            return None
        ordered = sorted(neighbors, key=lambda w: math.atan2(w[1] - cur_v[1], w[0] - cur_v[0]))
        try:
            idx = ordered.index(cur_u)
        except ValueError:
            return None
        nxt = ordered[(idx - 1) % len(ordered)]
        cur_u, cur_v = cur_v, nxt
        if (cur_u, cur_v) == start:
            return face[:-1]
    return None


def _triangulate_planar_face(face_pts3d: np.ndarray) -> Optional[list[tuple[int, int, int]]]:
    """Triangulate a planar roof face given as 3D points (CCW order, all
    coplanar). The normal is estimated via Newell's method (a sum over all
    vertex pairs, not just the first 3): for an exactly planar CCW polygon
    this is identical in direction to `cross(p1-p0, p2-p0)`, but it stays
    numerically stable when the first few vertices happen to be near-collinear
    (common along a straight or gently-curved boundary run), which the
    3-point cross product cannot handle. The projection basis (`u` along the
    first edge projected onto the fitted plane, `v = normal x u`) makes
    `_ear_clip`'s output map directly to outward-facing 3D triangles, with no
    winding reversal.

    Returns `None` if the face is degenerate (fewer than 3 points, near-zero
    normal or first edge), not planar within tolerance, or ear-clipping fails.
    """
    n = len(face_pts3d)
    if n < 3:
        return None
    nx = ny = nz = 0.0
    for i in range(n):
        x0, y0, z0 = face_pts3d[i]
        x1, y1, z1 = face_pts3d[(i + 1) % n]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    normal = np.array([nx, ny, nz])
    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return None
    normal = normal / norm_len

    p0 = face_pts3d[0]
    edge = face_pts3d[1] - p0
    edge = edge - np.dot(edge, normal) * normal
    edge_len = np.linalg.norm(edge)
    if edge_len < 1e-12:
        return None
    u_axis = edge / edge_len
    v_axis = np.cross(normal, u_axis)

    span = max(float(np.linalg.norm(face_pts3d.max(axis=0) - face_pts3d.min(axis=0))), 1e-9)
    # Looser than the old 1e-4: real (but still flat, per straight-skeleton
    # theory) facets can accumulate a bit more numerical noise from polyskel
    # over many vertices than a simple 3-point check tolerated.
    tol = span * 2e-3
    pts2d = np.zeros((n, 2))
    for i, p in enumerate(face_pts3d):
        rel = p - p0
        if abs(np.dot(rel, normal)) > tol:
            return None
        pts2d[i] = (np.dot(rel, u_axis), np.dot(rel, v_axis))

    try:
        return _ear_clip(pts2d)
    except RuntimeError:
        return None


def _split_pinched_face(face: list, classify, add_face_triangles) -> bool:
    """Decompose a `_trace_face` result that spans a straight-skeleton "pinch"
    — a thin-stroke split/collision event where a ridge directly touches a
    *different* stretch of boundary — into its correct flat sub-facets, and
    add each via `add_face_triangles`.

    `face[0]` is the vertex the trace started from, so the maximal run of
    consecutive (non-SKEL) boundary points containing it is the face's "home"
    run — the boundary edge(s) actually being traced. Any *other* maximal
    boundary run found elsewhere in the cyclic face is foreign, even if it
    shares the home run's ring (`classify(pt) -> (kind, ring_id)`): two
    non-adjacent stretches of the very same outer or hole ring can end up in
    one merged trace just as easily as outer-vs-hole can (e.g. where a bowl's
    curve pinches against an unrelated stem). Each foreign run, plus the one
    skeleton point flanking it on each side, forms its own small flat facet
    (`add_face_triangles([before] + run + [after])`); the remainder of `face`
    — with each such run replaced by a direct edge between its two flanking
    points — forms the main flat facet. When only the home run is found this
    reduces to triangulating `face` unchanged (the common, non-pinched case).
    Returns False if any resulting sub-facet fails to triangulate (e.g. still
    non-planar, or degenerates below 3 points). Used as a middle fallback tier
    in `_build_roof_mesh`/`_skeleton_roof_component`, between ownership-
    constrained tracing and plain unconstrained tracing.
    """
    n = len(face)
    cats = [classify(p) for p in face]

    runs: list[tuple[int, int]] = []
    idx = 0
    while idx < n:
        cat = cats[idx]
        if cat[0] != "SKEL":
            start = idx
            j = idx
            while j + 1 < n and cats[j + 1] == cat:
                j += 1
            runs.append((start, j))
            idx = j + 1
        else:
            idx += 1

    home_run = next(r for r in runs if r[0] <= 0 <= r[1])
    foreign_runs = [r for r in runs if r != home_run]

    if not foreign_runs:
        return add_face_triangles(face)

    skip = set()
    for (s, e) in foreign_runs:
        skip.update(range(s, e + 1))
    big_face = [face[k] for k in range(n) if k not in skip]

    ok = len(big_face) >= 3 and add_face_triangles(big_face)
    for (s, e) in foreign_runs:
        before = face[(s - 1) % n]
        after = face[(e + 1) % n]
        small_face = [before] + face[s:e + 1] + [after]
        ok = add_face_triangles(small_face) and ok
    return ok


class _UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _edge_line(a: tuple, b: tuple) -> Optional[tuple]:
    """Return `(point, unit_direction, unit_interior_normal)` for boundary
    edge `a -> b`, where the normal points to the LEFT of `a -> b` — the
    interior side for a CCW outer edge, and (per the polygon-winding
    convention used throughout this module) also the interior/solid side for
    a CW hole edge. `None` if the edge is degenerate (zero length).
    """
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return None
    ux, uy = dx / length, dy / length
    return (ax, ay), (ux, uy), (-uy, ux)


def _assign_edge_ownership(
    adjacency: dict,
    heights: dict,
    p0_keys: list,
    hole_keys_list: list,
) -> tuple[dict, dict]:
    """Determine, for every graph vertex, which original boundary edge(s) its
    roof facet belongs to — the information the plain undirected-adjacency
    graph doesn't otherwise retain, and which a naive angle-sort walk
    (`_trace_face`) can silently cross between when several boundary edges'
    facets get merged by a "pass-through" vertex (no ridge to separate them).

    Two edges are first grouped together (via union-find) whenever a shared
    boundary vertex between them has no interior connection at all (a genuine
    "no ridge here, these edges' facets are contiguous" case — most commonly a
    flat or gently-curved boundary run). Then, per straight-skeleton theory, a
    point's height equals its perpendicular distance to the line of whichever
    edge(s) it belongs to — a "ridge" vertex is assigned to every edge group
    whose line it sits at height-equal distance from (usually one, but
    genuinely two or more at real ridge/valley junctions where facets meet).

    Returns `(owners, group_of)`:
      owners   : vertex-key -> set of edge-group ids that own it
      group_of : edge_id ('OUTER', i) or ('HOLE', j, i) -> its group id
    """
    all_edge_ids: list = []
    edge_lines: dict = {}
    n0 = len(p0_keys)
    for i in range(n0):
        eid = ("OUTER", i)
        all_edge_ids.append(eid)
        line = _edge_line(p0_keys[i], p0_keys[(i + 1) % n0])
        if line is not None:
            edge_lines[eid] = line
    for j, hkeys in enumerate(hole_keys_list):
        nh = len(hkeys)
        for i in range(nh):
            eid = ("HOLE", j, i)
            all_edge_ids.append(eid)
            line = _edge_line(hkeys[i], hkeys[(i + 1) % nh])
            if line is not None:
                edge_lines[eid] = line

    uf = _UnionFind(all_edge_ids)

    def union_ring(ring_keys, kind, ring_idx=None):
        n = len(ring_keys)
        for i in range(n):
            v = ring_keys[i]
            nbrs = adjacency.get(v, [])
            prev_pt, next_pt = ring_keys[(i - 1) % n], ring_keys[(i + 1) % n]
            if len(nbrs) != 2 or not all(w in (prev_pt, next_pt) for w in nbrs):
                continue
            # No ridge separates this vertex's two edges — but that only means
            # they share one flat facet if they're actually collinear. A
            # coarsely-segmented curve (e.g. low $fn) can have a real bend at
            # a vertex with no ridge event; merging across it would fold two
            # differently-angled edges' facets into one non-planar "facet".
            d1x, d1y = v[0] - prev_pt[0], v[1] - prev_pt[1]
            d2x, d2y = next_pt[0] - v[0], next_pt[1] - v[1]
            len1, len2 = math.hypot(d1x, d1y), math.hypot(d2x, d2y)
            if len1 < 1e-12 or len2 < 1e-12:
                continue
            cross = (d1x * d2y - d1y * d2x) / (len1 * len2)
            if abs(cross) > 0.02:  # ~1.1 degrees
                continue
            prev_id = ("OUTER", (i - 1) % n) if kind == "OUTER" else ("HOLE", ring_idx, (i - 1) % n)
            cur_id = ("OUTER", i) if kind == "OUTER" else ("HOLE", ring_idx, i)
            uf.union(prev_id, cur_id)

    union_ring(p0_keys, "OUTER")
    for j, hkeys in enumerate(hole_keys_list):
        union_ring(hkeys, "HOLE", j)

    group_of = {eid: uf.find(eid) for eid in all_edge_ids}

    p0_index = {k: i for i, k in enumerate(p0_keys)}
    hole_index = [{k: i for i, k in enumerate(hkeys)} for hkeys in hole_keys_list]

    owners: dict[tuple, set] = {}
    for v, h in heights.items():
        owned: set = set()
        if h <= 1e-9:
            if v in p0_index:
                i = p0_index[v]
                owned.add(group_of[("OUTER", (i - 1) % n0)])
                owned.add(group_of[("OUTER", i)])
            else:
                for j, hi in enumerate(hole_index):
                    if v in hi:
                        i = hi[v]
                        nh = len(hole_keys_list[j])
                        owned.add(group_of[("HOLE", j, (i - 1) % nh)])
                        owned.add(group_of[("HOLE", j, i)])
                        break
        else:
            best_diff = float("inf")
            best_groups: set = set()
            for eid, (pt, _ud, un) in edge_lines.items():
                dist = (v[0] - pt[0]) * un[0] + (v[1] - pt[1]) * un[1]
                if dist < -1e-6:
                    continue  # vertex is on the wrong side of this edge's line
                diff = abs(dist - h)
                gid = group_of[eid]
                if diff <= max(1e-6, h * 1e-4):
                    owned.add(gid)
                if diff < best_diff:
                    best_diff = diff
                    best_groups = {gid}
            if not owned:
                owned = best_groups
        owners[v] = owned
    return owners, group_of


def _trace_owned_face(adjacency: dict, owners: dict, group_id, u: tuple, v: tuple) -> Optional[list]:
    """Like `_trace_face`, but only follows the "immediately clockwise"
    neighbour while it's owned by `group_id` (see `_assign_edge_ownership`).
    As soon as the next vertex belongs to a different facet, that's this
    facet's natural boundary — return what's traced so far (an implicitly
    closed polygon: the caller connects its last point straight back to `u`),
    rather than blindly continuing into someone else's geometry the way a
    plain angle-sort walk would.
    """
    start = (u, v)
    face = [u]
    cur_u, cur_v = u, v
    for _ in range(2 * len(adjacency) + 4):
        face.append(cur_v)
        neighbors = adjacency.get(cur_v)
        if not neighbors or len(neighbors) < 2:
            return face if len(face) >= 3 else None
        ordered = sorted(neighbors, key=lambda w: math.atan2(w[1] - cur_v[1], w[0] - cur_v[0]))
        try:
            idx = ordered.index(cur_u)
        except ValueError:
            return face if len(face) >= 3 else None
        nxt = ordered[(idx - 1) % len(ordered)]
        if nxt != u and group_id not in owners.get(nxt, ()):
            return face if len(face) >= 3 else None
        cur_u, cur_v = cur_v, nxt
        if (cur_u, cur_v) == start:
            return face[:-1]
    return None


def _build_roof_mesh(
    p0: np.ndarray,
    hole_arrs: list[np.ndarray],
    heights: dict,
    adjacency: dict,
    p0_keys: list,
    hole_keys_list: list,
    degenerate_holes: list,
    key,
    strategy: str,
) -> Optional[m3d.Manifold]:
    """Build one candidate roof+floor mesh for a component, using one of three
    face-tracing strategies. Returns a closed Manifold or None on any failure.

    No single strategy handles every thin-stroke pinch/collision pattern a
    real font can produce, so `_skeleton_roof_component` tries all three (in
    order of how much cross-facet protection they offer) and keeps whichever
    actually produces a valid manifold:

    - `"owned"`: `_assign_edge_ownership` + `_trace_owned_face`. Computes,
      from straight-skeleton geometry alone (a vertex's height must equal its
      distance to the edge(s) it belongs to), which facet every vertex
      actually belongs to, and refuses to trace across into a different
      facet's territory. Handles most pinches (outer-vs-hole, two
      non-adjacent runs of the same ring) correctly and precisely — but when
      a ridge point is legitimately, exactly equidistant from *several*
      boundary edges at once (a real hip/valley junction, common along
      *repeatedly*-pinched thin strokes), each edge's trace can independently
      give up right there and close via a "virtual chord" back to its own
      start, with no guarantee some other trace produces the exact opposite
      chord to pair with it — leaving the mesh open.
    - `"split"`: plain `_trace_face` + `_split_pinched_face`. Traces without
      any ownership constraint (so it can merge multiple facets into one
      loop), then decomposes that loop after the fact by finding the "home"
      run of boundary points containing the trace's start and treating any
      other boundary run as a pinch to excise into its own small facet. Less
      precise than `"owned"` (a coarser, single-pass heuristic) but doesn't
      have the open-mesh failure mode, so it succeeds on some geometries
      `"owned"` doesn't.
    - `"plain"`: plain `_trace_face`, no cross-facet handling at all. Works
      whenever a component simply has no such pinch to worry about.
    """
    try:
        from shapely.geometry import Polygon as _SPoly
        from shapely import constrained_delaunay_triangles as _cdt

        n0 = len(p0_keys)
        heights = dict(heights)
        adjacency = {k: list(v) for k, v in adjacency.items()}

        final_verts: list[tuple[float, float, float]] = []
        idx_map: dict[tuple, int] = {}

        def vert_index(pos):
            if pos not in idx_map:
                idx_map[pos] = len(final_verts)
                final_verts.append((pos[0], pos[1], heights.get(pos, 0.0)))
            return idx_map[pos]

        tris = []

        # --- Floor tessellation (at z=0, normal pointing downward) ---
        if not hole_arrs:
            # No holes: ear-clip gives consistent CCW triangles → reverse for -z normal.
            for (i, j, k2) in _ear_clip(p0):
                tris.append((vert_index(p0_keys[k2]), vert_index(p0_keys[j]), vert_index(p0_keys[i])))
        else:
            # Holes: constrained Delaunay (respects polygon boundary edges) +
            # centroid filter. Winding check ensures a downward (-z) normal.
            outer_2d = [(float(p[0]), float(p[1])) for p in p0]
            holes_2d = [[(float(p[0]), float(p[1])) for p in h] for h in hole_arrs]
            shape = _SPoly(outer_2d, holes_2d)
            for tri in _cdt(shape).geoms:
                if not shape.contains(tri.centroid):
                    continue
                coords = list(tri.exterior.coords)[:3]
                ax, ay = coords[0]
                bx, by = coords[1]
                cx, cy = coords[2]
                # Signed area: positive = CCW from above (upward normal), so reverse for -z.
                signed_area2 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
                if signed_area2 > 0:
                    coords = list(reversed(coords))
                fi = []
                for (fx, fy) in coords:
                    k = key(fx, fy)
                    heights.setdefault(k, 0.0)
                    adjacency.setdefault(k, [])
                    fi.append(vert_index(k))
                tris.append(tuple(fi))

        # --- Roof faces for outer boundary edges (CCW: interior on left) ---
        # Holes are CW-in-math; the LEFT of each natural CW directed edge is
        # the exterior (roofable) region, so trace in the natural direction.
        # Degenerate holes (isolated skeleton, no connection to the main
        # polyskel run) are the exception: their interior apex sits on the
        # opposite side of the natural direction (the isolated skeleton was
        # computed by treating the hole as its own tiny outer polygon), so
        # `_trace_face` must be called on the reversed edge to find the correct
        # face at all — but the found face's point order then comes out with
        # the base edge in the same direction as the floor triangulation's
        # (which always walks holes in their natural order), so it must be
        # reversed again before triangulating to get the opposing winding a
        # manifold mesh requires.
        if strategy == "owned":
            owners, group_of = _assign_edge_ownership(adjacency, heights, p0_keys, hole_keys_list)
        elif strategy == "split":
            p0_key_set = set(p0_keys)
            hole_key_sets = [set(hkeys) for hkeys in hole_keys_list]

            def classify(pt):
                if pt in p0_key_set:
                    return ("OUTER", -1)
                for j, hs in enumerate(hole_key_sets):
                    if pt in hs:
                        return ("HOLE", j)
                return ("SKEL", -1)

        consumed: set[tuple] = set()

        def add_face_triangles(face_pts_keys):
            face_pts3d = np.array([(p[0], p[1], heights.get(p, 0.0)) for p in face_pts_keys])
            face_tris = _triangulate_planar_face(face_pts3d)
            if face_tris is None:
                return False
            face_idx = [vert_index(p) for p in face_pts_keys]
            for (a, b, c) in face_tris:
                tris.append((face_idx[a], face_idx[b], face_idx[c]))
            nf = len(face_pts_keys)
            for k in range(nf):
                consumed.add((face_pts_keys[k], face_pts_keys[(k + 1) % nf]))
            return True

        def process_boundary_edge(edge_id, u, v):
            if (u, v) in consumed:
                return True
            if strategy == "owned":
                face = _trace_owned_face(adjacency, owners, group_of[edge_id], u, v)
                if face is None or len(face) < 3:
                    return False
                return add_face_triangles(face)
            face = _trace_face(adjacency, u, v)
            if face is None or len(face) < 3:
                return False
            if strategy == "split":
                return _split_pinched_face(face, classify, add_face_triangles)
            return add_face_triangles(face)

        for i in range(n0):
            if not process_boundary_edge(("OUTER", i), p0_keys[i], p0_keys[(i + 1) % n0]):
                return None

        for j, (hkeys, is_degenerate) in enumerate(zip(hole_keys_list, degenerate_holes)):
            nh = len(hkeys)
            if len(set(hkeys)) != nh:
                return None
            for i in range(nh):
                u, v = hkeys[i], hkeys[(i + 1) % nh]
                if is_degenerate:
                    if (u, v) in consumed:
                        continue
                    face = _trace_face(adjacency, v, u)
                    if face is not None:
                        face = list(reversed(face))
                    if face is None or len(face) < 3:
                        return None
                    if not add_face_triangles(face):
                        return None
                else:
                    if not process_boundary_edge(("HOLE", j, i), u, v):
                        return None

        if not tris or not final_verts:
            return None
        mesh = m3d.Mesh(
            vert_properties=np.array(final_verts, dtype=np.float32),
            tri_verts=np.array(tris, dtype=np.uint32),
        )
        body = m3d.Manifold(mesh)
        if body.status() != m3d.Error.NoError or body.is_empty():
            return None
        return body
    except Exception:
        return None


def _skeleton_roof_component(
    outer_arr: np.ndarray,
    hole_arrs: list[np.ndarray],
) -> Optional[m3d.Manifold]:
    """Build a straight-skeleton roof for one connected component: a CCW outer
    polygon and zero or more CW hole polygons. Returns a closed Manifold or None.

    Tries `_build_roof_mesh`'s three tracing strategies in order — `"owned"`,
    `"split"`, `"plain"` (see its docstring) — since each fails on a different,
    non-overlapping class of thin-stroke geometry; trying all three covers far
    more real fonts/glyphs than any single one alone.
    """
    p0 = _ccw_polygon(outer_arr)
    n0 = len(p0)
    if n0 < 3:
        return None

    graph = _build_skeleton_graph_with_holes(p0, hole_arrs)
    if graph is None:
        return None
    heights, adjacency, p0_keys, hole_keys_list, key, degenerate_holes = graph
    if len(set(p0_keys)) != n0:
        return None

    for strategy in ("owned", "split", "plain"):
        body = _build_roof_mesh(
            p0, hole_arrs, heights, adjacency, p0_keys, hole_keys_list,
            degenerate_holes, key, strategy,
        )
        if body is not None:
            return body
    return None


def _skeleton_roof_general(cs: m3d.CrossSection) -> Optional[m3d.Manifold]:
    """Build an exact straight-skeleton roof for `cs`, handling any combination
    of outer contours and holes. Separates polygons into connected components
    (each outer + its direct holes), builds a skeleton roof per component via
    `_skeleton_roof_component`, and returns their union. Returns None on failure.
    """
    try:
        from shapely.geometry import Polygon as _SPoly, Point as _SPoint

        polys = cs.to_polygons()
        if not polys:
            return None

        # Separate outer (CCW, area2 > 0) from hole (CW, area2 < 0) polygons
        outer_arrs: list[np.ndarray] = []
        hole_arrs: list[np.ndarray] = []
        for poly in polys:
            arr = np.asarray(poly, dtype=np.float64)
            n = len(arr)
            area2 = float(np.sum(
                arr[:, 0] * np.roll(arr[:, 1], -1)
                - np.roll(arr[:, 0], -1) * arr[:, 1]
            ))
            if area2 > 0:
                outer_arrs.append(arr)
            elif area2 < 0:
                hole_arrs.append(arr)

        if not outer_arrs:
            return None

        # Group each hole with the smallest containing outer polygon
        outer_shapes = [_SPoly([(p[0], p[1]) for p in arr]) for arr in outer_arrs]
        components: list[tuple[np.ndarray, list[np.ndarray]]] = [
            (arr, []) for arr in outer_arrs
        ]
        for hole_arr in hole_arrs:
            cx = float(np.mean(hole_arr[:, 0]))
            cy = float(np.mean(hole_arr[:, 1]))
            pt = _SPoint(cx, cy)
            best_i, best_area = None, float('inf')
            for i, shape in enumerate(outer_shapes):
                if shape.contains(pt) and shape.area < best_area:
                    best_i, best_area = i, shape.area
            if best_i is not None:
                components[best_i][1].append(hole_arr)

        pieces: list[m3d.Manifold] = []
        for (outer_arr, comp_holes) in components:
            b = _skeleton_roof_component(outer_arr, comp_holes)
            if b is None:
                return None  # partial failure — let caller fall back to SDF
            pieces.append(b)

        if not pieces:
            return None
        body = pieces[0]
        for b in pieces[1:]:
            body = body + b
        return body
    except Exception:
        return None


def _vec_sub(a, b):
    if type(a) is list and type(b) is list:
        if _is_flat_numeric(a) and _is_flat_numeric(b):
            if len(a) >= _NP_VEC_THRESHOLD:
                n = min(len(a), len(b))
                return (np.asarray(a[:n]) - np.asarray(b[:n])).tolist()
            return [x - y for x, y in zip(a, b)]
        return [_vec_sub(x, y) for x, y in zip(a, b)]
    if type(a) is bool or type(b) is bool:
        return None
    try:
        return a - b
    except TypeError:
        return None


def _osc_type_name(v) -> str:
    """OpenSCAD's name for `v`'s type, as used in 'undefined operation (...)' warnings."""
    if v is None:
        return "undefined"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "vector"
    if isinstance(v, OscObject):
        return "object"
    return "undefined"


def _object_arg_type_name(v) -> str:
    """Type name as used in `object()`'s own argument-validation warnings
    (`<number>`, `<string>`, `<list>`, ... `<undef>`) — distinct spelling from
    `_osc_type_name()`'s `undefined`/`vector`."""
    if v is None:
        return "undef"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, OscRange):
        return "range"
    if isinstance(v, OscObject):
        return "object"
    if isinstance(v, (FunctionDeclaration, FunctionLiteral)):
        return "function"
    return "undef"


def _osc_equal(a, b) -> bool:
    ta, tb = type(a), type(b)
    if (ta is bool) != (tb is bool):
        return False
    if ta is list and tb is list:
        return len(a) == len(b) and all(_osc_equal(x, y) for x, y in zip(a, b))
    if ta is OscObject and tb is OscObject:
        pairs_a, pairs_b = list(a.items()), list(b.items())
        return len(pairs_a) == len(pairs_b) and all(
            ka == kb and _osc_equal(va, vb)
            for (ka, va), (kb, vb) in zip(pairs_a, pairs_b)
        )
    return a == b


def _osc_comparable(a, b) -> bool:
    ta, tb = type(a), type(b)
    if ta is bool or tb is bool:
        return ta is bool and tb is bool
    if (ta is int or ta is float) and (tb is int or tb is float):
        return True
    if ta is str and tb is str:
        return True
    if ta is list and tb is list:
        return True
    return False


def _format_number(v: float) -> str:
    """Format a number the way OpenSCAD's `echo()`/`str()` do.

    Differs from Python's `f"{v:g}"` in two ways:
    - exponents drop their leading zero (`1e+08` -> `1e+8`, `1e-07` -> `1e-7`)
    - small numbers stay in fixed notation one digit further than `%g`
      (`1e-5` -> `0.00001`, where `%g` would give `1e-05`); fixed notation
      covers exponents in `[-5, 5]`, vs. `%g`'s `[-4, 5]`.
    Both still show at most 6 significant digits, and `-0.0` -> `"0"`.
    """
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    if v == 0:
        return "0"

    neg = v < 0
    av = abs(v)
    exp = math.floor(math.log10(av))
    mantissa = round(av / (10 ** exp), 5)
    if mantissa >= 10:
        mantissa /= 10
        exp += 1

    if -5 <= exp <= 5:
        decimals = max(0, 5 - exp)
        s = f"{av:.{decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
    else:
        m = f"{mantissa:.5f}".rstrip("0").rstrip(".")
        s = f"{m}e{'+' if exp >= 0 else '-'}{abs(exp)}"
    return ("-" + s) if neg else s


def _matmul(a, b):
    a_is_mat = bool(a) and isinstance(a[0], list)
    b_is_mat = bool(b) and isinstance(b[0], list)
    try:
        if not a_is_mat and not b_is_mat:
            n = len(a)
            if n != len(b):
                return None
            if n >= _NP_VEC_THRESHOLD:
                return np.dot(np.asarray(a), np.asarray(b)).tolist()
            s = 0
            for i in range(n):
                s += a[i] * b[i]
            return s
        na = np.asarray(a)
        nb = np.asarray(b)
        if na.dtype == object or nb.dtype == object:
            return None
        return np.dot(na, nb).tolist()
    except (TypeError, ValueError, IndexError):
        return None


class OscRange:
    """Lazy OpenSCAD range value — echoes as [start : step : end], iterable, indexable."""
    __slots__ = ("start", "step", "end")

    def __init__(self, start: float, step: float, end: float):
        self.start = start
        self.step = step
        self.end = end

    def __iter__(self):
        if self.step == 0:
            return
        v = self.start
        if self.step > 0:
            while v <= self.end + 1e-10:
                yield v
                v += self.step
        else:
            while v >= self.end - 1e-10:
                yield v
                v += self.step

    def __getitem__(self, idx: int):
        # OpenSCAD indexes a range as its 3 components, not its iterated values:
        # `[2:3:11][0]` -> 2 (start), `[1]` -> 3 (step), `[2]` -> 11 (end).
        return (self.start, self.step, self.end)[idx] if 0 <= idx <= 2 else None

    def __repr__(self):
        return f"OscRange({self.start}, {self.step}, {self.end})"


class OscObject:
    """OpenSCAD `object()` value — an ordered string-keyed map."""
    __slots__ = ("data",)

    def __init__(self, data: dict):
        self.data = data

    def __iter__(self):
        return iter(self.data)  # keys, in insertion order

    def __len__(self):
        return len(self.data)

    def get(self, key):
        return self.data.get(key)  # missing key -> None (undef)

    def items(self):
        return self.data.items()

    def __repr__(self):
        return f"OscObject({self.data!r})"


_FONT_PATH = Path(__file__).parent.parent / "resources" / "fonts" / "LiberationSans-Regular.ttf"
_font_table_cache: dict[tuple[str, int], dict] = {}  # (path, ttc_index) → font tables


def _font_tables_from_path(path: str, ttc_index: int = 0) -> dict:
    """Load font tables from a file path, using a module-level cache."""
    key = (path, ttc_index)
    if key not in _font_table_cache:
        font = TTFont(path, fontNumber=ttc_index)
        # Some fonts store glyph outlines in CFF rather than glyf.
        glyf_table = font.get("glyf")
        hmtx_table = font.get("hmtx")
        name_table = font.get("name")
        family_name = name_table.getBestFamilyName() if name_table else None
        style_name = name_table.getBestSubFamilyName() if name_table else None
        _font_table_cache[key] = {
            "cmap": font.getBestCmap() or {},
            "hmtx": hmtx_table,
            "glyf": glyf_table,
            "units_per_em": font["head"].unitsPerEm,
            "head": font["head"],
            "hhea": font.get("hhea"),
            "glyph_set": font.getGlyphSet(),
            "path": path,
            "ttc_index": ttc_index,
            "family_name": family_name or "Liberation Sans",
            "style_name": style_name or "Regular",
        }
    return _font_table_cache[key]


def _load_default_font() -> dict:
    """Load the bundled Liberation Sans font."""
    return _font_tables_from_path(str(_FONT_PATH), 0)


_font_spec_cache: dict[str, dict] = {}  # font-spec string → font tables


def _resolve_font(font_spec: str) -> dict:
    """Resolve an OpenSCAD/fontconfig font spec to font tables.

    Uses `fc-match` to find the best-matching system font for `font_spec`
    (e.g. `"Times New Roman:style=Bold"`).  Falls back to the bundled
    Liberation Sans if `fc-match` is not available or the spec is empty.
    """
    if not font_spec:
        return _load_default_font()
    if font_spec in _font_spec_cache:
        return _font_spec_cache[font_spec]
    try:
        import subprocess as _sp
        result = _sp.run(
            ["fc-match", "--format=%{file}:%{index}\n", font_spec],
            capture_output=True, text=True, timeout=3,
        )
        line = result.stdout.strip()
        if line and ":" in line:
            parts = line.rsplit(":", 1)
            fpath, idx_str = parts[0], parts[1]
            ttc_index = int(idx_str) if idx_str.isdigit() else 0
            if Path(fpath).exists():
                tables = _font_tables_from_path(fpath, ttc_index)
                _font_spec_cache[font_spec] = tables
                return tables
    except Exception:
        pass
    tables = _load_default_font()
    _font_spec_cache[font_spec] = tables
    return tables


def _glyph_bounds(gname: str, font: dict) -> tuple[float, float, float, float] | None:
    """Return (xMin, yMin, xMax, yMax) in font units for glyph `gname`, or None
    if the glyph is empty/whitespace.  Works for both TrueType (glyf table) and
    CFF (glyph_set pen draw) fonts."""
    glyf = font.get("glyf")
    if glyf is not None:
        g = glyf[gname]
        if g.numberOfContours == 0:
            return None
        return g.xMin, g.yMin, g.xMax, g.yMax
    # CFF/OTF: derive bounds by drawing the glyph contours
    glyph_set = font["glyph_set"]
    if gname not in glyph_set:
        return None
    xs: list[float] = []
    ys: list[float] = []

    class _BoundsPen(BasePen):
        def _moveTo(self, pt):
            xs.append(pt[0]); ys.append(pt[1])
        def _lineTo(self, pt):
            xs.append(pt[0]); ys.append(pt[1])
        def _curveToOne(self, bcp1, bcp2, pt):
            for p in (bcp1, bcp2, pt):
                xs.append(p[0]); ys.append(p[1])
        def _closePath(self):
            pass
        def _endPath(self):
            pass

    glyph_set[gname].draw(_BoundsPen(glyph_set))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _measure_text(text: str, size: float, spacing: float, font: dict | None = None) -> dict:
    """Lay out `text` left-to-right and return its ink-bbox/advance metrics
    in OpenSCAD units, scaled for `size` (see docs/evaluator.md for the
    scale-factor and per-glyph layout derivation).

    Returns a dict with `ascent`, `descent`, `ink_min_x`, `ink_max_x`,
    `advance_x`, and `glyphs` (a list of `(glyph_name, pen_x_scaled)` for
    each renderable glyph, used by `text()`) — aggregates are all `0` and
    `glyphs` is empty if `text` contains no measurable glyphs.
    """
    if font is None:
        font = _load_default_font()
    cmap, hmtx = font["cmap"], font["hmtx"]
    scale = size * (100 / 72) / font["units_per_em"]

    pen_x = 0.0
    ascent = descent = ink_min_x = ink_max_x = 0.0
    has_ink = False
    glyphs = []
    for ch in text:
        gname = cmap.get(ord(ch))
        if gname is None:
            continue
        advance, _lsb = hmtx[gname]
        bounds = _glyph_bounds(gname, font)
        if bounds is not None:
            xmin, ymin, xmax, ymax = bounds
            left = pen_x * scale + xmin * scale
            right = pen_x * scale + xmax * scale
            top = ymax * scale
            bottom = ymin * scale
            if not has_ink:
                ink_min_x, ink_max_x, ascent, descent = left, right, top, bottom
                has_ink = True
            else:
                ink_min_x = min(ink_min_x, left)
                ink_max_x = max(ink_max_x, right)
                ascent = max(ascent, top)
                descent = min(descent, bottom)
            glyphs.append((gname, pen_x * scale))
        pen_x += advance * spacing

    return {
        "ascent": ascent,
        "descent": descent,
        "ink_min_x": ink_min_x,
        "ink_max_x": ink_max_x,
        "advance_x": pen_x * scale,
        "glyphs": glyphs,
    }


def _text_align_offset(halign: str, valign: str, m: dict) -> tuple[float, float]:
    """Compute the `(offset_x, offset_y)` translation for `halign`/`valign`,
    given the dict returned by `_measure_text`. Shared by `_builtin_textmetrics`
    (which reports it) and `_builtin_text` (which applies it)."""
    advance_x, ascent, descent = m["advance_x"], m["ascent"], m["descent"]
    offset_x = -{"left": 0.0, "center": 0.5, "right": 1.0}.get(halign, 0.0) * advance_x
    offset_y = {
        "top": -ascent,
        "center": -(ascent + descent) / 2,
        "baseline": 0.0,
        "bottom": -descent,
    }.get(valign, 0.0)
    return offset_x, offset_y


class _FlattenPen(BasePen):
    """A `BasePen` that flattens glyph outlines — quadratic Bezier curves
    (TrueType `glyf` glyphs) and cubic Bezier curves (CFF/OTF glyphs) alike —
    into polygon contours, for building a `m3d.CrossSection`."""

    def __init__(self, glyphSet, segs: int):
        super().__init__(glyphSet)
        self.segs = segs
        self.contours: list[list[tuple[float, float]]] = []
        self._contour: list[tuple[float, float]] = []

    def _moveTo(self, pt):
        self._contour = [pt]

    def _lineTo(self, pt):
        self._contour.append(pt)

    def _qCurveToOne(self, pt1, pt2):
        p0 = self._contour[-1]
        for i in range(1, self.segs + 1):
            t = i / self.segs
            x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * pt1[0] + t ** 2 * pt2[0]
            y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * pt1[1] + t ** 2 * pt2[1]
            self._contour.append((x, y))

    def _curveToOne(self, pt1, pt2, pt3):
        p0 = self._contour[-1]
        for i in range(1, self.segs + 1):
            t = i / self.segs
            mt = 1 - t
            a, b, c, d = mt ** 3, 3 * mt ** 2 * t, 3 * mt * t ** 2, t ** 3
            x = a * p0[0] + b * pt1[0] + c * pt2[0] + d * pt3[0]
            y = a * p0[1] + b * pt1[1] + c * pt2[1] + d * pt3[1]
            self._contour.append((x, y))

    def _closePath(self):
        if self._contour:
            self.contours.append(self._contour)
        self._contour = []

    def _endPath(self):
        self._closePath()


# Cached as raw contour point-lists rather than `m3d.CrossSection` objects:
# nanobind-bound objects held in a module-level cache for the life of the
# process get reported as "leaked" at interpreter shutdown (finalization
# order races the manifold3d module's own teardown).
# Key: (font_path, ttc_index, glyph_name, segs)
_glyph_contour_cache: dict[tuple, list[np.ndarray]] = {}


def _glyph_cross_section(gname: str, segs: int, font: dict | None = None) -> m3d.CrossSection:
    """Return the (unscaled, font-units) `m3d.CrossSection` for glyph `gname`,
    flattening curves into `segs` segments. Contours cached per font+glyph+segs."""
    if font is None:
        font = _load_default_font()
    key = (font.get("path", ""), font.get("ttc_index", 0), gname, segs)
    contours = _glyph_contour_cache.get(key)
    if contours is None:
        glyph_set = font["glyph_set"]
        pen = _FlattenPen(glyph_set, segs)
        glyph_set[gname].draw(pen)
        contours = [np.array(c, dtype=np.float64) for c in pen.contours]
        _glyph_contour_cache[key] = contours
    if contours:
        return m3d.CrossSection(contours, m3d.FillRule.NonZero)
    return m3d.CrossSection()


@dataclass
class ColoredBody:
    """A Manifold body (3D) or CrossSection (2D) paired with an optional RGBA color."""
    body: Optional[m3d.Manifold] = None
    color: Optional[tuple[float, float, float, float]] = None  # RGBA 0-1
    section: Optional[m3d.CrossSection] = None  # set for 2D primitives
    flat_preview: bool = False  # thin extrusion standing in for a 2D shape (see to_renderable_bodies)
    role: str = "normal"  # "normal" | "highlight" (#, real geom) | "highlight_ghost" (#, inside CSG) | "background" (%) | "show_only" (!)


@dataclass
class CSGNode:
    """One node in the persistent, coarse-grained CSG tree. Complements —
    does not replace — id_to_node (Manifold originalID -> AST node), which
    stays the fine-grained per-triangle provenance table used for WYSIWYG
    ray-cast picking.

    Built in two passes: the AST walk (Evaluator._eval_statement) resolves
    every node — plain data only, no Manifold calls — and populates `params`
    and `children`, leaving `bodies` empty; Evaluator.generate_tree() then
    walks the completed tree bottom-up and populates `bodies` by calling
    each node's generate_fn. Can be re-run on any (possibly partial) tree at
    any time — e.g. to render a live partial result at a debugger breakpoint
    (Phase 3) — since resolve never depends on any node's generated bodies.

    kind is a human-readable label (a ModularCall's call name, e.g. "cube",
    "union"; or "highlight"/"background"/"show_only"/"intersection_for" for
    the four non-ModularCall wrapper kinds). User-module calls never get a
    CSGNode of their own (Evaluator._eval_statement splices their resolved
    body directly into the enclosing node's children instead), so kind is
    always a builtin's own name or an unrecognized module name -- never a
    user module's, shadowed or not.
    """
    kind: str
    node: ASTNode
    bodies: list[ColoredBody]
    is_builtin: bool = True
    children: list["CSGNode"] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    # True if this node's own resolve step (or any descendant's) called
    # rands() without accounting for global RNG state -- see
    # Evaluator._rands_call_count. Tainted nodes are never cache-hit by
    # ManifoldCache, since their resolved params aren't a pure function of
    # their own content (the actual rands() output also depends on every
    # earlier rands() call's position in the script's evaluation order).
    uncacheable: bool = False


# Thin extrusion height used to display top-level 2D results (e.g. `circle();`)
# in the 3D viewport — the renderer/exporter only know how to handle Manifold
# meshes, and real OpenSCAD's flat 2D preview has no Manifold equivalent.
_TOP_LEVEL_2D_HEIGHT = 1e-3


def to_renderable_bodies(bodies: list[ColoredBody]) -> list[ColoredBody]:
    """Convert top-level 2D-only results (`body is None`, `section` set —
    e.g. `circle();`) into thin-extruded Manifolds, so the renderer/exporter
    (which only handle Manifold meshes) can display them. 3D bodies pass
    through unchanged."""
    return [
        ColoredBody(body=m3d.Manifold.extrude(cb.section, _TOP_LEVEL_2D_HEIGHT),
                    color=cb.color, flat_preview=True, role=cb.role)
        if cb.body is None and cb.section is not None else cb
        for cb in bodies
    ]


def flatten_csg_tree(tree: list[CSGNode]) -> list[ColoredBody]:
    """Concatenate every top-level node's already-computed .bodies (NOT
    recursing into .children — a parent's .bodies already is the fully
    resolved/combined result of its children, e.g. a union node's .bodies
    is the merged CSG result, not to be added on top of its children's
    individual bodies too). Reproduces evaluate()'s returned body list
    exactly for any script with no top-level `!` (show_only) — evaluate()'s
    own post-hoc show_only filter is applied once across the whole flat
    result and is not itself represented by any single tree node."""
    return [b for node in tree for b in node.bodies]


def _summarize_param(value, max_items: int = 6, max_len: int = 40) -> str:
    """Compact one-line repr for a CSGNode.params value, used by
    format_csg_tree — collapses long lists/dicts/arrays (e.g. polyhedron
    points, imported STL verts, surface() height grids) to "<... of N>"
    instead of dumping them in full, since numpy's own repr can span
    multiple lines and would otherwise break the one-line-per-node dump.
    Collapsing is purely size-based (item count), not "contains a nested
    container" or "is a numpy array" — a small list/dict/array is shown
    in full with each element/value itself recursively summarized, so
    e.g. translate's args={0: [1.0,2.0,3.0]} (one entry whose value is a
    short flat list) displays completely instead of collapsing to an
    opaque "<dict of 1>" just because that one entry happens to be a
    list, and a small polyhedron's own points/faces (its actual
    user-authored content, unlike sphere/cylinder's auto-generated
    tessellation — see _DUMP_TESSELLATION_KEYS, which excludes the
    latter from the dump entirely by key) are visible rather than
    reduced to a bare shape."""
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        if len(value) > max_items:
            kind = "tuple" if isinstance(value, tuple) else "list"
            return f"<{kind} of {len(value)}>"
        return "[" + ", ".join(_summarize_param(v) for v in value) + "]"
    if isinstance(value, dict):
        if len(value) > max_items:
            return f"<dict of {len(value)}>"
        return "{" + ", ".join(f"{k!r}: {_summarize_param(v)}" for k, v in value.items()) + "}"
    text = repr(value)
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


# Params keys never shown in format_csg_tree's per-node summary, regardless
# of kind -- pure internal bookkeeping (op/name duplicate the node's own
# kind; group_sizes is _generate_csg's private re-chunking data) or already
# represented structurally elsewhere in the tree (color() gets its own
# wrapping CSGNode, same as translate/rotate, so every descendant leaf
# additionally carrying its own inherited "color" param -- needed at
# generate time, not a display bug -- would otherwise be shown twice).
_DUMP_HIDDEN_PARAM_KEYS = frozenset({"color", "op", "name", "group_sizes"})

# Params keys holding auto-generated tessellation data (not user-authored
# content) for every kind except polyhedron, where the equivalent data
# *is* the user's own points/faces input and is worth seeing (still
# collapsed to <... of N> by _summarize_param if it's large, same as any
# other param -- this only controls whether the key is shown at all).
_DUMP_TESSELLATION_KEYS = frozenset({"verts", "tris", "tri_arr"})

# Params keys renamed for display only (the underlying dict key stays
# "segs" everywhere it's actually used -- _generate_cylinder/_generate_2d/
# etc. all read params["segs"]) -- "segs" is every _resolve_X's own name
# for the circular-segment count it resolved from $fn/$fa/$fs (via _fn()),
# shared by cylinder/circle/offset/text; "$fn=" reads as what it actually
# represents to someone looking at the dump, rather than exposing the
# resolve step's internal variable name.
_DUMP_KEY_RENAMES = {"segs": "$fn"}


def _format_call_args(args: dict) -> str:
    """Render a _resolve_args()-shaped dict ({0: v0, 1: v1, 'name': v, ...}
    -- positional args keyed by index, named args keyed by name) as
    OpenSCAD call-argument syntax ("v0, v1, name=v") instead of Python
    dict syntax. Used by format_csg_tree for a transform's "args" param,
    so e.g. translate([2,2,-1])'s dump reads translate([2.0, 2.0, -1.0])
    the way the user actually wrote it, not translate(args={0: [2.0,
    2.0, -1.0]})."""
    parts = []
    for k, v in args.items():
        if isinstance(k, int):
            parts.append(_summarize_param(v))
        else:
            parts.append(f"{k}={_summarize_param(v)}")
    return ", ".join(parts)


def format_csg_tree(tree: list[CSGNode], indent: int = 0) -> str:
    """Human-readable recursive dump of a resolved CSG tree — kind and a
    compact params summary (see _summarize_param and the _DUMP_*_KEYS
    filters above). Used by the Design menu's "Dump CSG Tree to Console"
    command.

    Represents geometry, not the code that produced it: neither
    children() calls nor user-module calls get a node of their own (see
    _eval_statement) -- their resolved subtree is spliced directly into
    the enclosing node's children, so e.g. a user module wrapping a cube
    shows up as just "cube(...)", not "mymodule(...) > cube(...)".

    Deliberately omits a generated-body count: once Evaluator's
    ManifoldCache (see evaluate()/generate_tree()) reuses a cached
    ancestor's result, it skips recursing into that ancestor's children
    entirely, leaving their own .bodies at the empty default from
    construction -- not because they produced no geometry, but simply
    because generate_tree never visited them on that pass. A count that
    reads "0" in that case would be actively misleading, not just
    uninformative, so this only ever describes resolved structure
    (which is always complete/reliable regardless of caching), never
    generated output.

    Indent is +1 unit for every non-root line (i.e. depth 1 gets two
    indent units, not one): the console (ConsoleWidget._append_foldable)
    displays multi-line output with a "<arrow> " prefix on the first
    line only (2 display columns) and no prefix on the rest -- without
    this compensating offset, a depth-1 child's own indent would land in
    the same column the root's text starts at (right after the arrow),
    making it look like a sibling of the root rather than its child."""
    lines = []
    pad = "  " * (indent + 1) if indent > 0 else ""
    for node in tree:
        shown = {
            k: v for k, v in node.params.items()
            if k not in _DUMP_HIDDEN_PARAM_KEYS
            and not (k in _DUMP_TESSELLATION_KEYS and node.kind != "polyhedron")
        }
        parts = [
            _format_call_args(v) if k == "args" and isinstance(v, dict)
            else f"{_DUMP_KEY_RENAMES.get(k, k)}={_summarize_param(v)}"
            for k, v in shown.items()
        ]
        params_str = ", ".join(parts)
        lines.append(f"{pad}{node.kind}({params_str})")
        if node.children:
            lines.append(format_csg_tree(node.children, indent + 1))
    return "\n".join(lines)


def _canon(value):
    """Recursively convert a CSGNode.params-shaped value (numbers, strings,
    bools, None, and nested lists/tuples/dicts/numpy arrays — params is
    documented as plain data, never Manifold objects) into a hashable
    canonical form, for use in ManifoldCache's cache key. Lists/tuples and
    numpy arrays become tuples; dict keys are sorted by str(key) (params
    dicts commonly mix int positional-arg keys with str named-arg keys —
    e.g. `cube(10, center=true)` — sorting the raw keys directly would
    raise TypeError comparing int to str)."""
    if isinstance(value, np.ndarray):
        return ("__ndarray__", value.shape, tuple(value.flatten().tolist()))
    if isinstance(value, (list, tuple)):
        return tuple(_canon(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted(((k, _canon(v)) for k, v in value.items()), key=lambda kv: str(kv[0])))
    return value


class ManifoldCache:
    """Content-hash cache of already-generated CSGNode subtrees, so
    generate_tree() can skip re-running Manifold work for a subtree whose
    resolved content (kind/params/children, see Evaluator._cache_key)
    hasn't changed since a previous render/debugger pause. Lives outside
    any single Evaluator/evaluate() call's lifetime — owned by MainWindow
    and passed into each new Evaluator() via its manifold_cache= kwarg, so
    it survives across the fresh Evaluator/AST/CSGNode objects every
    render creates. Thread-safe (renders and debug sessions run on
    background QThreads and can genuinely overlap)."""

    def __init__(self):
        self._entries: dict[tuple, list[ColoredBody]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple) -> list[ColoredBody] | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: tuple, bodies: list[ColoredBody]) -> None:
        with self._lock:
            self._entries[key] = bodies

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_DEFAULT_DOLLAR = {"$fn": 0, "$fa": 12.0, "$fs": 2.0, "$t": 0.0, "$parent_modules": 0}


class EvalContext:
    """Mutable evaluation state threaded through recursive calls."""
    __slots__ = ('scope', 'dyn', 'let', 'dyn_positions', 'dyn_explicit', 'color',
                 'children_nodes', 'children_caller_ctx')

    def __init__(self, scope, dyn=None, let=None, dyn_positions=None, dyn_explicit=None, color=None,
                 children_nodes=None, children_caller_ctx=None):
        self.scope = scope
        self.dyn = dyn if dyn is not None else dict(_DEFAULT_DOLLAR)
        self.let = let if let is not None else {}
        self.dyn_positions = dyn_positions if dyn_positions is not None else {}
        # Names the *script itself* assigned via a `$var = ...;` statement,
        # as opposed to names merely present in `dyn` because they were
        # seeded from the current viewport state (see Evaluator.evaluate's
        # viewport_params) -- lets callers distinguish "the script set
        # $vpt" from "the evaluator pre-populated $vpt from the current
        # camera," which look identical in `dyn` alone.
        self.dyn_explicit = dyn_explicit if dyn_explicit is not None else set()
        self.color = color
        self.children_nodes = children_nodes if children_nodes is not None else []
        self.children_caller_ctx = children_caller_ctx

    def child_ctx(self, scope=None, dyn=None, let=None, color=None,
                  children_nodes=None, children_caller_ctx=None):
        """Like every other field here, an unspecified children_nodes/
        children_caller_ctx means "inherit from self" -- NOT "this call has
        no deferred children," which is what defaulting to []/None would
        mean. child_ctx() is for tweaking ambient state (color, a $-var
        override) without leaving the current statement's evaluation, so a
        children() call evaluated under the result must still be able to
        forward to *this* context's caller. (Entering an actual new module
        body, where children_nodes legitimately changes to that call's own
        children, goes through call_ctx() instead, whose callers always
        pass real values explicitly.) Forgetting to thread these through
        previously broke any user module of the form `color(c) children();`
        (silently swallowing the children() call's own forwarded geometry)
        the moment _resolve_color's `ctx.child_ctx(color=rgba)` reset them —
        _resolve_transform never hit this, since it evaluates children
        against the original ctx directly rather than deriving a new one."""
        return EvalContext(
            scope=scope if scope is not None else self.scope,
            dyn=dyn if dyn is not None else dict(self.dyn),
            let=let if let is not None else dict(self.let),
            dyn_positions={} if dyn is None else self.dyn_positions,
            dyn_explicit=set(self.dyn_explicit),
            color=color if color is not None else self.color,
            children_nodes=children_nodes if children_nodes is not None else self.children_nodes,
            children_caller_ctx=children_caller_ctx if children_caller_ctx is not None else self.children_caller_ctx,
        )

    def let_child_ctx(self):
        ctx = EvalContext.__new__(EvalContext)
        ctx.scope = self.scope
        ctx.dyn = self.dyn
        ctx.let = dict(self.let)
        ctx.dyn_positions = self.dyn_positions
        ctx.dyn_explicit = self.dyn_explicit
        ctx.color = self.color
        ctx.children_nodes = self.children_nodes
        ctx.children_caller_ctx = self.children_caller_ctx
        return ctx

    def call_ctx(self, scope=None, color=None,
                 children_nodes=None, children_caller_ctx=None):
        return EvalContext(
            scope=scope if scope is not None else self.scope,
            dyn=dict(self.dyn),
            let={},
            dyn_positions={},
            dyn_explicit=set(self.dyn_explicit),
            color=color if color is not None else self.color,
            children_nodes=children_nodes if children_nodes is not None else [],
            children_caller_ctx=children_caller_ctx,
        )


class Evaluator:
    def __init__(self, echo_fn=None, debug_hook=None, error_break_fn=None, return_hook=None,
                 manifold_cache: "ManifoldCache | None" = None):
        self.id_to_node: dict[int, ASTNode] = {}
        self.csg_tree: list[CSGNode] = []
        self._tree_stack: list[list[CSGNode]] = [self.csg_tree]
        # Opt-in (None by default, so every existing bare Evaluator(...)
        # call site/test is unaffected) content-hash cache shared across
        # renders/debugger pauses -- see ManifoldCache and generate_tree().
        self._manifold_cache = manifold_cache
        # Incremented by _builtin_rands -- lets _eval_statement detect
        # whether rands() was called anywhere while resolving a given
        # CSGNode, to taint it (and its ancestors) as uncacheable. See
        # CSGNode.uncacheable.
        self._rands_call_count = 0
        # Every geometry-producing builtin kind (== CSGNode.kind) is
        # registered here (Phase 2, complete — see docs/evaluator.md "CSG
        # tree"). resolve_fn parses arguments and recursively resolves
        # children as plain data (no Manifold calls); generate_fn does the
        # actual Manifold/CrossSection work, called later by generate_tree()
        # in a separate bottom-up pass. Kinds with no entry (user-module
        # calls, unknown module names) fall back to _resolve_fallback_call /
        # generate_tree()'s default child-concatenation behavior.
        self._RESOLVE_DISPATCH = {
            "cube": self._resolve_cube,
            "sphere": self._resolve_sphere,
            "cylinder": self._resolve_cylinder,
            "polyhedron": self._resolve_polyhedron,
            "circle": self._resolve_2d,
            "square": self._resolve_2d,
            "polygon": self._resolve_2d,
            "text": self._resolve_text,
            "translate": self._resolve_transform,
            "rotate": self._resolve_transform,
            "scale": self._resolve_transform,
            "mirror": self._resolve_transform,
            "resize": self._resolve_transform,
            "multmatrix": self._resolve_transform,
            "color": self._resolve_color,
            "hull": self._resolve_hull,
            "minkowski": self._resolve_minkowski,
            "offset": self._resolve_offset,
            "projection": self._resolve_projection,
            "union": self._resolve_csg,
            "difference": self._resolve_csg,
            "intersection": self._resolve_csg,
            "intersection_for": self._resolve_intersection_for,
            "linear_extrude": self._resolve_linear_extrude,
            "rotate_extrude": self._resolve_rotate_extrude,
            "roof": self._resolve_roof,
            "surface": self._resolve_surface,
            "import": self._resolve_import,
            "render": self._resolve_render,
            "children": self._resolve_children_call,
            "breakpoint": self._resolve_breakpoint,
            "highlight": self._resolve_modifier_child,
            "background": self._resolve_modifier_child,
            "show_only": self._resolve_modifier_child,
        }
        self._GENERATE_DISPATCH = {
            "cube": self._generate_cube,
            "sphere": self._generate_sphere,
            "cylinder": self._generate_cylinder,
            "polyhedron": self._generate_polyhedron,
            "circle": self._generate_2d,
            "square": self._generate_2d,
            "polygon": self._generate_2d,
            "text": self._generate_text,
            "translate": self._generate_transform,
            "rotate": self._generate_transform,
            "scale": self._generate_transform,
            "mirror": self._generate_transform,
            "resize": self._generate_transform,
            "multmatrix": self._generate_transform,
            "color": self._generate_color,
            "hull": self._generate_hull,
            "minkowski": self._generate_minkowski,
            "offset": self._generate_offset,
            "projection": self._generate_projection,
            "union": self._generate_csg,
            "difference": self._generate_csg,
            "intersection": self._generate_csg,
            "intersection_for": self._generate_intersection_for,
            "linear_extrude": self._generate_linear_extrude,
            "rotate_extrude": self._generate_rotate_extrude,
            "roof": self._generate_roof,
            "surface": self._generate_surface,
            "import": self._generate_import,
            "highlight": self._generate_highlight,
            "background": self._generate_background,
            "show_only": self._generate_show_only,
        }
        self._errors: list[str] = []
        self._echo_fn = echo_fn or (lambda msg: print(msg))
        self._call_stack: list = []
        self._frame_ctxs: list = []
        self._debug_hook = debug_hook
        self._debugging = debug_hook is not None
        self._error_break_fn = error_break_fn
        self._return_hook = return_hook
        self._last_locals: dict = {}
        self._last_children_positions: Optional[list[tuple[Optional[str], int]]] = None
        self._last_all_frame_locals: list = []
        self._last_ctx: EvalContext | None = None
        self._root_ctx: EvalContext | None = None
        self._expr_depth: int = 0
        self._math_fns = {
            "abs": abs, "sign": lambda x: (1 if x > 0 else -1 if x < 0 else 0),
            "ceil": lambda x: x if (math.isnan(x) or math.isinf(x)) else math.ceil(x),
            "floor": lambda x: x if (math.isnan(x) or math.isinf(x)) else math.floor(x),
            "round": lambda x: x if (math.isnan(x) or math.isinf(x))
                else (math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)),
            "sqrt": lambda x: float('nan') if x < 0 else math.sqrt(x),
            "ln": lambda x: float('-inf') if x == 0 else (float('nan') if x < 0 else math.log(x)),
            "log": lambda x: float('-inf') if x == 0 else (float('nan') if x < 0 else math.log10(x)),
            "exp": math.exp,
            "sin": self._builtin_sin,
            "cos": self._builtin_cos,
            "tan": self._builtin_tan,
            "asin": lambda x: float('nan') if abs(x) > 1 else math.degrees(math.asin(x)),
            "acos": lambda x: float('nan') if abs(x) > 1 else math.degrees(math.acos(x)),
            "atan": lambda x: math.degrees(math.atan(x)),
            "atan2": lambda y, x: math.degrees(math.atan2(y, x)),
            "max": self._builtin_max, "min": self._builtin_min,
            "pow": self._builtin_pow,
            "norm": lambda v: math.sqrt(sum(x*x for x in v)),
            "cross": self._builtin_cross,
            "rands": self._builtin_rands,
            "concat": lambda *args: sum((list(a) if isinstance(a, list) else [a] for a in args), []),
            "len": lambda x: len(x) if isinstance(x, (list, str, OscObject)) else None,
            "str": lambda *a: "".join(x if isinstance(x, str) else self._fmt_val(x) for x in a),
            "chr": self._builtin_chr,
            "ord": lambda s: ord(s[0]) if isinstance(s, str) and len(s) >= 1 else None,
            "is_undef": lambda x: x is None,
            "is_num": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and not math.isnan(x),
            "is_bool": lambda x: isinstance(x, bool),
            "is_string": lambda x: isinstance(x, str),
            "is_list": lambda x: isinstance(x, list),
            "is_function": lambda x: isinstance(x, (FunctionDeclaration, FunctionLiteral)),
            "is_object": lambda x: isinstance(x, OscObject),
            "search": self._builtin_search,
            "lookup": self._builtin_lookup,
            "has_key": lambda obj, key: (key in obj.data) if isinstance(obj, OscObject) else None,
            "version": lambda: [2025, 1, 1],
            "version_num": lambda: 20250101,
            "parent_module": self._builtin_parent_module,
        }
        self._BUILTIN_FN_NAMES = frozenset(self._math_fns) | {"object", "textmetrics", "fontmetrics"}

    def _build_frame_locals(self, ctx: EvalContext):
        local_scope: dict = {}
        dyn_names: set = set()
        for k, v in ctx.let.items():
            local_scope[k] = v
            dyn_names.add(k)
        for k, v in ctx.dyn.items():
            if k.startswith('$'):
                local_scope[k] = v

        outer_scope: dict = {}
        if self._call_stack and self._root_ctx is not None:
            for k, v in self._root_ctx.let.items():
                if k not in local_scope:
                    outer_scope[k] = v

        current_frame = {"local_scope": local_scope, "outer_scope": outer_scope, "dyn_names": dyn_names,
                          "dyn_explicit": set(ctx.dyn_explicit)}
        all_frame_locals = [current_frame]
        for frame_ctx in reversed(self._frame_ctxs[:-1]):
            p_local: dict = {}
            p_dyn: set = set()
            for k, v in frame_ctx.let.items():
                p_local[k] = v
                p_dyn.add(k)
            for k, v in frame_ctx.dyn.items():
                if k.startswith('$'):
                    p_local[k] = v
            all_frame_locals.append({"local_scope": p_local, "outer_scope": {}, "dyn_names": p_dyn,
                                     "dyn_explicit": set(frame_ctx.dyn_explicit)})

        if self._call_stack:
            toplevel_frame = {
                "local_scope": dict(outer_scope),
                "outer_scope": {},
                "dyn_names": set(),
                "dyn_explicit": set(),
            }
            all_frame_locals.append(toplevel_frame)

        self._last_locals = {n: v for n, v in local_scope.items() if n in dyn_names}
        self._last_all_frame_locals = all_frame_locals
        return self._last_locals, all_frame_locals

    @staticmethod
    def _child_statement_positions(node: ASTNode) -> Optional[list[tuple[Optional[str], int]]]:
        """(origin, line) for each top-level, non-declaration child of
        `node` (a ModularCall's `.children` — the `{ ... }` block passed to
        a module call), if any. Used by the debugger's "Step to Child"
        command to know which statements children()/children(N) might
        forward control to — stashed on self._last_children_positions
        rather than threaded through the debug_hook callback itself, so
        adding it doesn't change that protocol's signature (every
        hand-rolled test hook would otherwise need updating)."""
        node_children = getattr(node, 'children', None)
        if not node_children:
            return None
        positions = []
        for c in node_children:
            if isinstance(c, (Assignment, ModuleDeclaration, FunctionDeclaration)):
                continue
            cpos = getattr(c, 'position', None)
            cline = getattr(cpos, 'line', None) if cpos else None
            if cline is not None:
                positions.append((getattr(cpos, 'origin', None), int(cline)))
        return positions or None

    def _check_debug(self, node: ASTNode, ctx: EvalContext, forced: bool = False, expr_level: bool = False):
        if self._debug_hook is None:
            return
        pos = getattr(node, 'position', None)
        line = getattr(pos, 'line', None) if pos else None
        if line is None:
            return
        origin = getattr(pos, 'origin', None)
        self._last_children_positions = self._child_statement_positions(node)

        cmd, mods = self._debug_hook(
            int(line), len(self._call_stack),
            forced=forced, expr_level=expr_level,
            expr_depth=self._expr_depth, origin=origin,
            get_frames=lambda: (self._build_frame_locals(ctx), list(self._call_stack)),
        )
        for k, v in mods.items():
            ctx.let[k] = v
        if cmd == "stop":
            raise EvalError("Debugging stopped.")

    @staticmethod
    def _loc(pos) -> str:
        if pos is None:
            return ""
        return f" in file {pos.origin}, line {pos.line}"

    def _trace_lines(self, node=None, innermost_frame: str | None = None) -> list[str]:
        """Build TRACE lines matching OpenSCAD's error/warning format."""
        lines = []
        node_pos = getattr(node, 'position', None) if node is not None else None
        if innermost_frame:
            lines.append(f"TRACE: called by '{innermost_frame}'{self._loc(node_pos)}")
        for entry in reversed(self._call_stack):
            kind = entry[0]
            fname = entry[1]
            call_pos = entry[2]
            if kind == "module":
                decl_pos = entry[3] if len(entry) > 3 else None
                lines.append(f"TRACE: call of '{fname}()'{self._loc(decl_pos)}")
                lines.append(f"TRACE: called by '{fname}'{self._loc(call_pos)}")
            else:
                lines.append(f"TRACE: called by '{fname}'{self._loc(call_pos)}")
        return lines

    def error(self, msg: str, node=None, innermost_frame: str | None = None):
        pos = getattr(node, 'position', None) if node is not None else None
        header = f"ERROR: {msg}{self._loc(pos)}"
        lines = [header] + self._trace_lines(node, innermost_frame)
        full = "\n".join(lines)
        self._errors.append(full)
        if self._error_break_fn is not None:
            line = getattr(pos, 'line', 0) if pos else 0
            origin = getattr(pos, 'origin', None) if pos else None
            if self._last_ctx is not None:
                _, all_frame_locals = self._build_frame_locals(self._last_ctx)
            else:
                all_frame_locals = self._last_all_frame_locals
            self._error_break_fn(int(line), header, all_frame_locals, list(self._call_stack), origin=origin)
        raise EvalError(full)

    def _fmt_val(self, v) -> str:
        if v is None:
            return "undef"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, OscRange):
            return f"[{_format_number(v.start)} : {_format_number(v.step)} : {_format_number(v.end)}]"
        if isinstance(v, float):
            return _format_number(v)
        if isinstance(v, list):
            return "[" + ", ".join(self._fmt_val(x) for x in v) + "]"
        if isinstance(v, OscObject):
            if len(v) == 0:
                return "object()"
            inner = ", ".join(f"{k} = {self._fmt_val(val)}" for k, val in v.items())
            return f"object({inner})"
        if isinstance(v, str):
            return f'"{v}"'
        return str(v)

    def _do_echo(self, arguments, ctx: "EvalContext"):
        parts = []
        for arg in arguments:
            val = self._eval_expr(arg.expr, ctx)
            if isinstance(arg, NamedArgument):
                parts.append(f"{arg.name.name} = {self._fmt_val(val)}")
            else:
                parts.append(self._fmt_val(val))
        self._echo_fn("ECHO: " + ", ".join(parts))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_use_statements(nodes: list[ASTNode], root_scope) -> None:
        """Inject modules/functions from `use`d files into root_scope.

        OpenSCAD makes `use`d modules globally visible across the entire
        compilation unit.  The parser's build_scopes only hoists top-level
        declarations and skips UseStatement nodes, so we resolve them here.
        """
        seen: set[str] = set()
        for node in nodes:
            if type(node) is not UseStatement:
                continue
            filepath = node.filepath.val
            origin = getattr(node.position, 'origin', '') if node.position else ''
            lib_file = findLibraryFile(origin, filepath)
            if lib_file is None or lib_file in seen:
                continue
            seen.add(lib_file)
            used_ast = getASTfromFile(lib_file)
            if not used_ast:
                continue
            used_scope = build_scopes(used_ast)
            for name, decl in used_scope.modules.items():
                if name not in root_scope.modules:
                    root_scope.define_module(name, decl)
            for name, decl in used_scope.functions.items():
                if name not in root_scope.functions:
                    root_scope.define_function(name, decl)

    def evaluate(self, nodes: list[ASTNode], root_scope, viewport_params: dict | None = None) -> tuple[list[ColoredBody], dict[int, ASTNode]]:
        """Walk top-level AST nodes to build self.csg_tree (resolve pass,
        no Manifold calls), then generate_tree() it once (generate pass,
        the only place Manifold work happens) to produce the final
        geometry. Returns (geometry, id_to_node mapping)."""
        self._resolve_use_statements(nodes, root_scope)
        self._call_stack.clear()
        self._frame_ctxs.clear()
        self.csg_tree = []
        self._tree_stack = [self.csg_tree]
        ctx = EvalContext(scope=root_scope)
        if viewport_params:
            ctx.dyn.update(viewport_params)
        self._root_ctx = ctx
        # OpenSCAD executes all assignments before geometry in each scope.
        assignments = [n for n in nodes if isinstance(n, Assignment)]
        others = [n for n in nodes if not isinstance(n, Assignment)]
        for node in assignments + others:
            self._eval_statement(node, ctx)
        result = self.generate_tree(self.csg_tree)
        # ! (show_only) modifier: if any body is show_only, display only those + highlights
        show_only = [b for b in result if b.role == "show_only"]
        if show_only:
            result = [b for b in result if b.role in ("show_only", "highlight")]
        return result, self.id_to_node

    # ------------------------------------------------------------------
    # Statement dispatch
    # ------------------------------------------------------------------

    # AST node types that get their own CSGNode in self.csg_tree. ModularCall
    # covers every primitive/transform/boolean/hull/minkowski/children()/
    # user-module call. The three tagging modifiers (#/%/!) each wrap exactly
    # one child. ModularIntersectionFor is the one control-flow-shaped node
    # that is NOT transparent like for/if/let: it combines its per-iteration
    # children into a single intersected result (see _eval_intersection_for),
    # so its iterations must nest under one tree node just like union()'s
    # children nest under a union node — otherwise flatten_csg_tree() would
    # return the pre-intersection per-iteration bodies instead of the actual
    # combined result. ModularModifierDisable (*) is deliberately excluded:
    # it never evaluates its child at all, so there is nothing to record.
    _TREE_NODE_TYPES = (
        ModularCall,
        ModularModifierHighlight,
        ModularModifierBackground,
        ModularModifierShowOnly,
        ModularIntersectionFor,
    )

    def _tree_node_kind(self, node: ASTNode, ctx: EvalContext) -> tuple[str, bool]:
        """Return (kind, is_builtin) for a node in _TREE_NODE_TYPES. is_builtin
        is only meaningful for ModularCall (False when `name` resolves to a
        user module via ctx.scope.lookup_module) — a user module can shadow a
        builtin name, so `kind` alone is not a unique discriminator."""
        if isinstance(node, ModularCall):
            name = node.name.name
            return name, ctx.scope.lookup_module(name) is None
        if isinstance(node, ModularModifierHighlight):
            return "highlight", True
        if isinstance(node, ModularModifierBackground):
            return "background", True
        if isinstance(node, ModularModifierShowOnly):
            return "show_only", True
        return "intersection_for", True  # ModularIntersectionFor

    def _eval_statement(self, node: ASTNode, ctx: EvalContext) -> list[ColoredBody]:
        """Thin wrapper around _eval_statement_impl that additionally builds
        self.csg_tree as a side effect. For the five _TREE_NODE_TYPES,
        pushes a new children accumulator before resolving, pops it in a
        finally (so a raised EvalError cleanly discards the in-progress
        node rather than corrupting a parent's accumulator), then either
        appends the completed CSGNode to whichever accumulator is now on
        top of the stack, or -- for children() calls and user-module calls,
        neither of which is itself geometry -- splices the resolved
        children directly into that accumulator with no wrapping node (see
        the kind == "children"/is_builtin check below). Every recursive
        call site already calls self._eval_statement(...), so nesting
        composes automatically with no other call site changes.

        Generation is fully deferred (Phase 2 final cutover): this only
        ever calls a resolve_fn (plain data, no Manifold calls) and always
        returns []. Real bodies are populated later, in one bottom-up pass,
        by generate_tree(self.csg_tree) — see that method and evaluate().
        Every builtin kind has a resolve_fn (registered in
        _RESOLVE_DISPATCH); anything without one (user-module calls, and
        genuinely unknown module names) falls back to
        _resolve_fallback_call, which still builds the tree correctly via
        the pre-existing _eval_modular_call dispatch.

        The _check_debug(node, ctx) call below is this method's own
        responsibility for _TREE_NODE_TYPES — _eval_statement_impl (which
        calls it for every other statement type) is never reached for these
        nodes, since the isinstance check above always routes them here
        instead. Without this, no geometry-producing statement (any
        ModularCall, plus the #/%/! modifiers and intersection_for) would
        ever pause the debugger or advance step state.
        """
        if not isinstance(node, self._TREE_NODE_TYPES):
            return self._eval_statement_impl(node, ctx)
        self._last_ctx = ctx
        if self._debugging:
            self._check_debug(node, ctx)
        kind, is_builtin = self._tree_node_kind(node, ctx)
        resolve_fn = (self._RESOLVE_DISPATCH.get(kind) if is_builtin else None) or self._resolve_fallback_call
        self._tree_stack.append([])
        rands_before = self._rands_call_count
        try:
            params = resolve_fn(node, ctx)
        finally:
            children = self._tree_stack.pop()
        if (kind == "children" and is_builtin) or not is_builtin:
            # Neither children() nor a user-module call is itself geometry
            # -- children() is a call-site substitution, and a user module
            # is just a named wrapper around whatever geometry statements
            # its body runs. Splice the resolved subtree directly into the
            # enclosing node's children instead of wrapping it in its own
            # node, so the CSG tree represents the geometry being
            # combined, not the code structure that produced it. But if
            # that subtree is more than one sibling, group them under a
            # "union" label for display purposes (is_builtin=False so
            # generate_tree still takes the default-concatenation path,
            # not the real _generate_csg boolean merge -- juxtaposed
            # statements with no explicit combinator keep their bodies
            # separate, same as any other module body/top-level script,
            # preserving each body's own color/provenance rather than
            # collapsing them into one Manifold the way an *explicit*
            # union() call does) so the dump reads as one shape at this
            # call site instead of N unrelated-looking siblings.
            if len(children) > 1:
                union_node = CSGNode(
                    kind="union", node=node, bodies=[], is_builtin=False,
                    children=children, params={},
                    uncacheable=any(c.uncacheable for c in children),
                )
                self._tree_stack[-1].append(union_node)
            else:
                self._tree_stack[-1].extend(children)
            return []
        # Taint this node (and thus every ancestor, since uncacheable
        # propagates via the `any(...)` below at each enclosing level) if
        # rands() was called anywhere while resolving it -- see CSGNode's
        # uncacheable docstring.
        uncacheable = (self._rands_call_count != rands_before) or any(c.uncacheable for c in children)
        tree_node = CSGNode(kind=kind, node=node, bodies=[],
                             is_builtin=is_builtin, children=children, params=params,
                             uncacheable=uncacheable)
        self._tree_stack[-1].append(tree_node)
        return []

    def _resolve_fallback_call(self, node: ModularCall, ctx: EvalContext) -> dict:
        """Structural resolve for ModularCall kinds with no _RESOLVE_DISPATCH
        entry: user-module calls (is_builtin=False) and genuinely unknown
        module names (is_builtin=True, no matching builtin or user module).
        Reuses the existing _eval_modular_call dispatch purely for its
        tree-building side effect — its return value (real bodies) is
        unused now that generation is deferred to generate_tree(), and its
        default (no registered generate_fn) is to concatenate children's
        bodies, which matches a user module body's plain concatenation and
        an unknown module's empty children list alike."""
        self._eval_modular_call(node, ctx)
        return {}

    def _cache_key(self, node: CSGNode) -> tuple:
        """Structural content-hash key for `node`, used by generate_tree()'s
        ManifoldCache lookup: (kind, is_builtin, canonicalized params,
        recursively-hashed children). Deliberately excludes node.node (the
        AST object) — every render builds a brand-new AST via
        getASTfromFile, so keying on AST identity would defeat cross-render
        caching entirely. Pure function of already-resolved data, never
        touches .bodies, so it's always cheap/safe to compute speculatively
        even on a cache miss."""
        return (node.kind, node.is_builtin, _canon(node.params),
                tuple(self._cache_key(c) for c in node.children))

    def generate_tree(self, tree: list[CSGNode]) -> list[ColoredBody]:
        """Bottom-up second pass over an already-resolved CSG tree: for each
        node, first generates its children (so any generate_fn reading
        flatten_csg_tree(children) sees real, populated bodies), then calls
        the node's own generate_fn (or, for kinds with no registered
        generate_fn — user-module calls, render()/children()/unknown-module
        passthroughs — concatenates the children's bodies), storing the
        result on node.bodies. Can be called on any (possibly partial) tree
        at any point — e.g. the debugger calling it on self.csg_tree at a
        breakpoint to render a live partial result (Phase 3).

        If self._manifold_cache is set (opt-in — None by default, so
        existing bare Evaluator() construction/tests are unaffected), each
        node's content hash is checked before doing any Manifold work: a
        cache hit reuses the previous .bodies and skips recursing into
        node.children entirely (no wasted Manifold work re-deriving
        children that would just be discarded); a miss generates normally
        and stores the result. node.uncacheable (rands() taint) always
        forces a miss, never a hit, and never gets stored either."""
        result = []
        for node in tree:
            key = None if (self._manifold_cache is None or node.uncacheable) else self._cache_key(node)
            cached = self._manifold_cache.get(key) if key is not None else None
            if cached is not None:
                node.bodies = cached
            else:
                children_bodies = self.generate_tree(node.children)
                generate_fn = self._GENERATE_DISPATCH.get(node.kind) if node.is_builtin else None
                node.bodies = generate_fn(node.params, node.children, node.node) if generate_fn is not None else children_bodies
                if key is not None:
                    self._manifold_cache.put(key, node.bodies)
            result.extend(node.bodies)
        return result

    def _eval_statement_impl(self, node: ASTNode, ctx: EvalContext) -> list[ColoredBody]:
        self._last_ctx = ctx
        t = type(node)
        if t is not ModuleDeclaration and t is not FunctionDeclaration and t is not ModularLet:
            if self._debugging:
                self._check_debug(node, ctx)
        if t is Assignment:
            name = node.name.name
            if name[0] == '$':
                ctx.dyn[name] = self._eval_expr(node.expr, ctx)
                ctx.dyn_explicit.add(name)
            else:
                pos = getattr(node, 'position', None)
                if name in ctx.dyn_positions:
                    first_pos = ctx.dyn_positions[name]
                    first_line = getattr(first_pos, 'line', '?') if first_pos else '?'
                    self._echo_fn(
                        f"WARNING: {name} was assigned on line {first_line}"
                        f" but was overwritten{self._loc(pos)}"
                    )
                ctx.let[name] = self._eval_expr(node.expr, ctx)
                ctx.dyn_positions[name] = pos
            return []
        if t is ModularIf:
            cond = self._eval_expr(node.condition, ctx)
            if cond:
                branch = node.true_branch
                if self._debugging:
                    self._check_debug(branch[0] if branch else node, ctx, expr_level=True)
                return self._eval_children(branch, ctx)
            return []
        if t is ModularIfElse:
            cond = self._eval_expr(node.condition, ctx)
            branch = node.true_branch if cond else node.false_branch
            if self._debugging:
                self._check_debug(branch[0] if branch else node, ctx, expr_level=True)
            return self._eval_children(branch, ctx)
        if t is ModularFor:
            return self._eval_for(node, ctx)
        if t is ModularLet:
            return self._eval_let_block(node, ctx)
        if t is ModularEcho:
            self._do_echo(node.arguments, ctx)
            return []
        if t is ModularAssert:
            args = self._resolve_args(node.arguments, ctx)
            cond = self._get_arg(args, 0, "condition", True)
            if not cond:
                raw = node.arguments
                cond_text = to_openscad([raw[0].expr]).strip() if raw else "false"
                msg = self._get_arg(args, 1, "message", None)
                err = f"Assertion '{cond_text}' failed" + (f': "{msg}"' if msg is not None else "")
                self.error(err, node, innermost_frame="assert")
                return []
            # Assertion passed — propagate any chained child geometry (e.g. assert(...) translate(...) children())
            if node.children:
                return self._eval_children(node.children, ctx)
            return []
        if isinstance(node, ModularModifierDisable):  # * — fully excluded
            return []
        if isinstance(node, (ModuleDeclaration, FunctionDeclaration)):
            return []
        return []

    def _eval_children(self, children, ctx: EvalContext) -> list[ColoredBody]:
        result = []
        # OpenSCAD executes all assignments before geometry in each scope.
        assignments = [c for c in children if isinstance(c, Assignment)]
        others = [c for c in children if not isinstance(c, Assignment)]
        for child in assignments + others:
            # Use the node's own scope from build_scopes when available so that
            # each node evaluates in its correct lexical scope. Share ctx.dyn
            # (not a copy) so that eager assignments in one sibling are visible
            # to subsequent siblings in the same block.
            child_scope = getattr(child, 'scope', None)
            if child_scope is not None:
                child_ctx = EvalContext(
                    scope=child_scope,
                    dyn=ctx.dyn,
                    let=ctx.let,
                    dyn_positions=ctx.dyn_positions,
                    dyn_explicit=ctx.dyn_explicit,
                    color=ctx.color,
                    children_nodes=ctx.children_nodes,
                    children_caller_ctx=ctx.children_caller_ctx,
                )
            else:
                child_ctx = ctx
            result.extend(self._eval_statement(child, child_ctx))
        return result

    # ------------------------------------------------------------------
    # Module call dispatch
    # ------------------------------------------------------------------

    def _eval_modular_call(self, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        name = node.name.name
        user_mod = ctx.scope.lookup_module(name)
        if user_mod is not None:
            return self._eval_user_module(user_mod, node, ctx)
        return self._eval_builtin(name, node, ctx)

    @staticmethod
    def _body_list(body: Optional[ColoredBody]) -> list[ColoredBody]:
        return [body] if body is not None else []

    @staticmethod
    def _pos_contains(outer, inner) -> bool:
        """True if `inner`'s source span is strictly contained within `outer`'s.

        Used to detect "`inner` is declared lexically inside `outer`'s body"
        (e.g. a nested `module`/`function`). Identical spans (a declaration
        calling itself — direct recursion) are NOT considered contained.
        """
        if outer is None or inner is None:
            return False
        if outer.origin != inner.origin:
            return False
        if (outer.start_offset, outer.end_offset) == (inner.start_offset, inner.end_offset):
            return False
        return outer.start_offset <= inner.start_offset and inner.end_offset <= outer.end_offset

    def _call_ctx_for(self, decl, ctx: EvalContext, scope=None,
                      children_nodes=None, children_caller_ctx=None) -> EvalContext:
        call_stack = self._call_stack
        if call_stack:
            decl_pos = decl.position
            if decl_pos is not None:
                dp_origin = decl_pos.origin
                dp_start = decl_pos.start_offset
                dp_end = decl_pos.end_offset
                for frame in call_stack:
                    outer = frame[-1]
                    if outer is not None and outer.origin == dp_origin:
                        o_start, o_end = outer.start_offset, outer.end_offset
                        if (o_start, o_end) != (dp_start, dp_end) and o_start <= dp_start and dp_end <= o_end:
                            return ctx.child_ctx(scope=scope, children_nodes=children_nodes,
                                                 children_caller_ctx=children_caller_ctx)
        return ctx.call_ctx(scope=scope, children_nodes=children_nodes,
                            children_caller_ctx=children_caller_ctx)

    def _eval_user_module(self, decl: ModuleDeclaration, call: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        # Bind parameters
        child_scope = getattr(decl, 'scope', None) or ctx.scope
        params = getattr(decl, 'parameters', None) or []
        args = self._bind_args(params, call.arguments, ctx)

        child_ctx = self._call_ctx_for(
            decl, ctx,
            scope=child_scope,
            children_nodes=call.children,
            children_caller_ctx=ctx,
        )
        # $children is the number of module-instantiation children passed in
        # `{}`, not the number of geometries they produced — e.g. `children()`
        # counts as one child even if the caller passed it none to forward.
        child_ctx.dyn["$children"] = len([
            c for c in call.children
            if not isinstance(c, (Assignment, ModuleDeclaration, FunctionDeclaration))
        ])
        for k, v in args.items():
            if k[0] == '$':
                child_ctx.dyn[k] = v
            else:
                child_ctx.let[k] = v
        # Apply defaults for missing params
        self._apply_defaults(params, child_ctx, ctx)

        name = call.name.name
        call_pos = getattr(call, 'position', None)
        decl_pos = getattr(decl, 'position', None)
        child_ctx.dyn["$parent_modules"] = sum(1 for e in self._call_stack if e[0] == "module")
        self._call_stack.append(("module", name, call_pos, decl_pos))
        self._frame_ctxs.append(child_ctx)
        try:
            module_body = getattr(decl, 'children', None) or getattr(decl, 'body', None) or []
            return self._eval_children(module_body, child_ctx)
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()

    def _bind_args(self, params, arguments, ctx: EvalContext) -> dict[str, Any]:
        result = {}
        positional_idx = 0
        nparams = len(params)
        _eval = self._eval_expr
        for arg in arguments:
            if type(arg) is NamedArgument:
                result[arg.name.name] = _eval(arg.expr, ctx)
            else:
                if positional_idx < nparams:
                    result[params[positional_idx].name.name] = _eval(arg.expr, ctx)
                positional_idx += 1
        return result

    # ------------------------------------------------------------------
    # Built-in modules
    # ------------------------------------------------------------------

    def _resolve_call_args(self, node: ModularCall, ctx: EvalContext) -> tuple[dict, EvalContext]:
        """Resolve a ModularCall's arguments and apply any $-prefixed
        named-arg dynamic-context overrides (e.g. sphere(r=2, $fn=64)).
        Shared by _eval_builtin (for not-yet-migrated builtins) and every
        migrated _resolve_* method (Phase 2), which bypass _eval_builtin's
        dispatch entirely and so need this logic themselves."""
        args = self._resolve_args(node.arguments, ctx)
        dyn_overrides = {k: v for k, v in args.items() if isinstance(k, str) and k.startswith("$")}
        if dyn_overrides:
            ctx = ctx.child_ctx(dyn={**ctx.dyn, **dyn_overrides})
        return args, ctx

    def _eval_builtin(self, name: str, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        args, ctx = self._resolve_call_args(node, ctx)

        if name == "echo":
            self._do_echo(node.arguments, ctx)
            return []
        if name == "assert":
            return []
        # Unknown module — warn with call stack, matching OpenSCAD's WARNING format
        pos = getattr(node, 'position', None)
        warn = f"WARNING: Ignoring unknown module '{name}'{self._loc(pos)}"
        trace = self._trace_lines(node)
        self._echo_fn("\n".join([warn] + trace))
        return []

    def _resolve_render(self, node: ModularCall, ctx: EvalContext) -> dict:
        # render() is a display hint; just pass through children — no
        # generate_fn is registered, so generate_tree()'s default
        # (concatenate children's bodies) reproduces the passthrough.
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        return {}

    def _resolve_children_call(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._builtin_children(args, ctx)  # side effect only; return (real bodies) unused now
        return {}

    def _resolve_breakpoint(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._builtin_breakpoint(args, node, ctx)  # side effect only (debug hook); never had children
        return {}

    def _resolve_modifier_child(self, node, ctx: EvalContext) -> dict:
        """Shared resolve for the #/%/! modifiers (ModularModifierHighlight/
        Background/ShowOnly), which each wrap exactly one child (node.child,
        not a ModularCall's node.children list). Builds the child into the
        tree for its side effect only; the actual role tagging happens in
        the matching _generate_highlight/_generate_background/_generate_show_only."""
        self._eval_statement(node.child, ctx)
        return {}

    def _generate_highlight(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        return [replace(b, role="highlight") for b in flatten_csg_tree(children)]

    def _generate_background(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        return [replace(b, role="background") for b in flatten_csg_tree(children)]

    def _generate_show_only(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        return [replace(b, role="show_only") for b in flatten_csg_tree(children)]

    def _resolve_args(self, arguments, ctx: EvalContext) -> dict:
        result = {}
        pos = 0
        _eval = self._eval_expr
        for arg in arguments:
            if type(arg) is PositionalArgument:
                result[pos] = _eval(arg.expr, ctx)
                pos += 1
            else:
                result[arg.name.name] = _eval(arg.expr, ctx)
        return result

    def _get_arg(self, args: dict, pos: int, name: str, default=None):
        if name in args:
            return args[name]
        if pos in args:
            return args[pos]
        return default

    # --- primitives ---

    def _tag(self, body: m3d.Manifold, node: ASTNode, ctx: EvalContext) -> ColoredBody:
        for orig_id in body.to_mesh().run_original_id:
            self.id_to_node[int(orig_id)] = node
        return ColoredBody(body=body, color=ctx.color)

    def _tag_generated(self, body: m3d.Manifold, node: ASTNode, color) -> ColoredBody:
        """Generate-phase equivalent of _tag(): takes an already-resolved
        color instead of ctx, since ctx isn't available once a builtin has
        been migrated to the resolve/generate split (Phase 2)."""
        for orig_id in body.to_mesh().run_original_id:
            self.id_to_node[int(orig_id)] = node
        return ColoredBody(body=body, color=color)

    def _fn(self, ctx: EvalContext, r: float = 0.0) -> int:
        return self._fn_segments(ctx.dyn.get("$fn", 0), ctx.dyn.get("$fa", 12.0),
                                  ctx.dyn.get("$fs", 2.0), r)

    @staticmethod
    def _fn_segments(fn, fa, fs, r: float = 0.0) -> int:
        """Pure segment-count formula, split out of _fn() so generate-phase
        code (e.g. rotate_extrude, which needs the merged children's bounds
        — unavailable until generate — for its radius) can compute segments
        from cached $fn/$fa/$fs values without a live ctx."""
        if isinstance(fn, (int, float)) and fn > 0:
            return max(3, int(fn))
        if not isinstance(fa, (int, float)) or fa <= 0:
            fa = 12.0
        if not isinstance(fs, (int, float)) or fs <= 0:
            fs = 2.0
        r = abs(r) if isinstance(r, (int, float)) and math.isfinite(r) else 0.0
        return int(math.ceil(max(5, min(360.0 / fa, r * 2.0 * math.pi / fs))))

    # --- cube (resolve/generate — Phase 2) ---

    def _resolve_cube(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        size = self._get_arg(args, 0, "size", 1.0)
        center = bool(self._get_arg(args, 1, "center", False))
        if isinstance(size, (int, float)):
            size = [size, size, size]
        size = [float(s) for s in size]
        return {"size": size, "center": center, "color": ctx.color}

    def _generate_cube(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        body = m3d.Manifold.cube(params["size"], params["center"])
        return [self._tag_generated(body, node, params["color"])]

    # --- sphere (resolve/generate — Phase 2) ---

    def _resolve_sphere(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        r = self._get_arg(args, 0, "r", None)
        d = self._get_arg(args, None, "d", None)
        if d is not None:
            r = d / 2
        if r is None:
            r = 1.0
        r = float(r)
        n = self._fn(ctx, r)  # longitude segments
        stacks = max(2, int(math.ceil(n / 2)))  # number of latitude rings (no single-point poles)

        # OpenSCAD-compatible sphere: polygon caps at top/bottom (no triangulated poles),
        # quad belts between rings. Rings evenly spaced excluding the actual poles.
        step = math.pi / stacks  # latitude step in radians
        verts = []
        rings = []  # rings[i] = list of vertex indices

        for s in range(stacks):
            lat = -math.pi / 2 + (s + 0.5) * step
            ring_r = r * math.cos(lat)
            z = r * math.sin(lat)
            ring = []
            for seg in range(n):
                angle = 2 * math.pi * seg / n
                ring.append(len(verts))
                verts.append([ring_r * math.cos(angle), ring_r * math.sin(angle), z])
            rings.append(ring)

        tris = []

        # Bottom polygon cap: fan with reversed winding → outward normal points down
        bot = rings[0]
        for i in range(1, n - 1):
            tris.append([bot[0], bot[i + 1], bot[i]])

        # Quad belts between adjacent rings
        for s in range(stacks - 1):
            lo, hi = rings[s], rings[s + 1]
            for seg in range(n):
                a, b = lo[seg], lo[(seg + 1) % n]
                c, d_ = hi[seg], hi[(seg + 1) % n]
                tris.append([a, b, d_])
                tris.append([a, d_, c])

        # Top polygon cap: forward-winding fan → outward normal points up
        top = rings[-1]
        for i in range(1, n - 1):
            tris.append([top[0], top[i], top[i + 1]])

        verts_arr = np.array(verts, dtype=np.float32)
        tris_arr = np.array(tris, dtype=np.uint32)
        return {"r": r, "segs": n, "verts": verts_arr, "tris": tris_arr, "color": ctx.color}

    def _generate_sphere(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        mesh = m3d.Mesh(vert_properties=params["verts"], tri_verts=params["tris"])
        body = m3d.Manifold(mesh)
        return [self._tag_generated(body, node, params["color"])]

    # --- cylinder (resolve/generate — Phase 2) ---

    def _resolve_cylinder(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        h = float(self._get_arg(args, 0, "h", 1.0))
        r = self._get_arg(args, 1, "r", None)
        r1 = self._get_arg(args, None, "r1", None)
        r2 = self._get_arg(args, None, "r2", None)
        d = self._get_arg(args, None, "d", None)
        d1 = self._get_arg(args, None, "d1", None)
        d2 = self._get_arg(args, None, "d2", None)
        center = bool(self._get_arg(args, None, "center", False))

        if d is not None and r is None:
            r = d / 2
        if d1 is not None and r1 is None:
            r1 = d1 / 2
        if d2 is not None and r2 is None:
            r2 = d2 / 2
        if r is not None:
            r1 = r2 = float(r)
        if r1 is None:
            r1 = 1.0
        if r2 is None:
            r2 = r1
        segs = self._fn(ctx, max(float(r1), float(r2)))

        return {"h": h, "r1": float(r1), "r2": float(r2), "center": center,
                "segs": segs, "color": ctx.color}

    def _generate_cylinder(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        body = m3d.Manifold.cylinder(params["h"], params["r1"], params["r2"],
                                      circular_segments=params["segs"], center=params["center"])
        return [self._tag_generated(body, node, params["color"])]

    # --- transforms ---

    def _resolve_transform(self, node: ModularCall, ctx: EvalContext) -> dict:
        name = node.name.name
        args, ctx = self._resolve_call_args(node, ctx)
        # Evaluate children for the side effect of building their CSGNodes
        # (pushed onto self._tree_stack) — the returned bodies themselves
        # are discarded here; generate reads them back via each child
        # CSGNode's own .bodies, which is correct regardless of whether a
        # given child is itself migrated or still eager.
        self._eval_children(node.children, ctx)
        return {"name": name, "args": args}

    def _generate_transform(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        name, args = params["name"], params["args"]
        result = []
        for b in flatten_csg_tree(children):
            if b.section is not None:
                result.append(replace(b, section=self._apply_transform_2d(name, args, b.section)))
            elif b.body is not None:
                result.append(replace(b, body=self._apply_transform_3d(name, args, b.body)))
            else:
                result.append(b)
        return result

    def _apply_transform_2d(self, name: str, args: dict, cs: "m3d.CrossSection") -> "m3d.CrossSection":
        if name == "translate":
            v = self._get_arg(args, 0, "v", [0, 0])
            cs = cs.translate([float(v[0]), float(v[1])])
        elif name == "rotate":
            a = self._get_arg(args, 0, "a", 0)
            # 2D rotation: scalar angle (Z), or [x,y,z] list → use Z component
            if isinstance(a, list):
                angle = float(a[2]) if len(a) > 2 else 0.0
            else:
                angle = float(a)
            cs = cs.rotate(angle)
        elif name == "scale":
            v = self._get_arg(args, 0, "v", [1, 1])
            if isinstance(v, (int, float)):
                v = [float(v), float(v)]
            cs = cs.scale([float(v[0]), float(v[1])])
        elif name == "mirror":
            v = self._get_arg(args, 0, "v", [1, 0])
            cs = cs.mirror([float(v[0]), float(v[1])])
        elif name == "multmatrix":
            m = self._get_arg(args, 0, "m", None)
            if m is not None:
                # Extract 2×3 affine matrix from 4×4: rows 0,1, cols 0,1,3
                mat2x3 = [
                    [float(m[0][0]), float(m[0][1]), float(m[0][3])],
                    [float(m[1][0]), float(m[1][1]), float(m[1][3])],
                ]
                cs = cs.transform(mat2x3)
        return cs

    def _apply_transform_3d(self, name: str, args: dict, body: "m3d.Manifold") -> "m3d.Manifold":
        if name == "translate":
            v = self._get_arg(args, 0, "v", [0, 0, 0])
            v = self._to_vec3(v)
            body = body.translate(v)
        elif name == "rotate":
            a = self._get_arg(args, 0, "a", 0)
            v = self._get_arg(args, 1, "v", None)
            body = self._apply_rotate(body, a, v)
        elif name == "scale":
            v = self._get_arg(args, 0, "v", [1, 1, 1])
            if isinstance(v, (int, float)):
                v = [v, v, v]
            v = [float(x) for x in v]
            body = body.scale(v)
        elif name == "mirror":
            v = self._get_arg(args, 0, "v", [1, 0, 0])
            v = self._to_vec3(v)
            body = body.mirror(v)
        elif name == "resize":
            newsize = self._get_arg(args, 0, "newsize", [0, 0, 0])
            newsize = [float(x) for x in newsize]
            bb = body.bounding_box()  # (xmin,ymin,zmin,xmax,ymax,zmax)
            sx = newsize[0] / (bb[3] - bb[0]) if newsize[0] != 0 and (bb[3]-bb[0]) != 0 else 1
            sy = newsize[1] / (bb[4] - bb[1]) if newsize[1] != 0 and (bb[4]-bb[1]) != 0 else 1
            sz = newsize[2] / (bb[5] - bb[2]) if newsize[2] != 0 and (bb[5]-bb[2]) != 0 else 1
            body = body.scale([sx, sy, sz])
        elif name == "multmatrix":
            m = self._get_arg(args, 0, "m", None)
            if m is not None:
                mat = self._to_matrix4x3(m)
                body = body.transform(mat)
        return body

    def _apply_rotate(self, body: m3d.Manifold, a, v) -> m3d.Manifold:
        if isinstance(a, (list, tuple)):
            # rotate([x,y,z]) — Euler angles in degrees, applied Z then Y then X
            ax, ay, az = float(a[0]), float(a[1]), float(a[2]) if len(a) > 2 else 0.0
            body = body.rotate([ax, ay, az])
            return body
        else:
            # rotate(a, v) — angle around axis
            angle = float(a)
            if v is None:
                v = [0, 0, 1]
            v = self._to_vec3(v)
            # Rodrigues rotation via matrix
            mat = self._axis_angle_matrix(v, math.radians(angle))
            body = body.transform(mat)
            return body

    def _axis_angle_matrix(self, axis, angle_rad: float) -> list:
        ax, ay, az = axis
        length = math.sqrt(ax*ax + ay*ay + az*az)
        if length == 0:
            return [[1,0,0,0],[0,1,0,0],[0,0,1,0]]
        ax, ay, az = ax/length, ay/length, az/length
        c = math.cos(angle_rad)
        s = math.sin(angle_rad)
        t = 1 - c
        return [
            [t*ax*ax+c,    t*ax*ay-s*az, t*ax*az+s*ay, 0],
            [t*ax*ay+s*az, t*ay*ay+c,    t*ay*az-s*ax, 0],
            [t*ax*az-s*ay, t*ay*az+s*ax, t*az*az+c,    0],
        ]

    def _to_vec3(self, v) -> list[float]:
        if isinstance(v, (int, float)):
            return [float(v), 0.0, 0.0]
        result = [float(x) for x in v]
        while len(result) < 3:
            result.append(0.0)
        return result[:3]

    def _to_matrix4x3(self, m) -> list:
        """Convert 4x4 or 4x3 matrix to manifold's 4x3 row-major transform."""
        rows = []
        for row in m[:3]:
            r = [float(x) for x in row]
            while len(r) < 4:
                r.append(0.0)
            rows.append(r[:4])
        return rows

    # --- color ---

    def _resolve_color(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        c = self._get_arg(args, 0, "c", [1, 1, 1, 1])
        alpha = float(self._get_arg(args, 1, "alpha", 1.0))
        if isinstance(c, str):
            rgba = self._css_color(c, alpha)
        elif isinstance(c, (list, tuple)):
            rgba = tuple(float(x) for x in c) + (alpha,) if len(c) == 3 else tuple(float(x) for x in c[:4])
        else:
            rgba = (1.0, 1.0, 1.0, 1.0)

        child_ctx = ctx.child_ctx(color=rgba)
        self._eval_children(node.children, child_ctx)  # side effect only, see _resolve_transform
        return {"rgba": rgba}

    def _generate_color(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        rgba = params["rgba"]
        return [replace(b, color=rgba) for b in flatten_csg_tree(children)]

    def _css_color(self, name: str, alpha: float = 1.0) -> tuple:
        if name.startswith("#"):
            h = name.lstrip("#")
            if len(h) == 6:
                rgb = (int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255)
            elif len(h) == 3:
                rgb = (int(h[0],16)/15, int(h[1],16)/15, int(h[2],16)/15)
            else:
                rgb = (1, 1, 1)
            return rgb + (alpha,)

        color = QColor(name)
        rgb = color.getRgbF()[:3] if color.isValid() else (1, 1, 1)
        return rgb + (alpha,)

    # --- CSG ---

    def _resolve_csg(self, node: ModularCall, ctx: EvalContext) -> dict:
        # Evaluate each top-level geometry statement separately so their body groups are
        # preserved.  For difference(), all bodies from the FIRST statement form the
        # positive operand (unioned implicitly, as OpenSCAD does within a scope); bodies
        # from each subsequent statement are unioned and then subtracted.  A flat
        # evaluation loses this grouping and produces wrong results when BOSL2's
        # attachable() returns multiple bodies (parent + attached children) as the first
        # operand of difference().
        #
        # group_sizes records, per top-level statement, how many CSGNode
        # children it contributed to self._tree_stack[-1] — needed because
        # for/if/let are "transparent" in the tree (Phase 1), so one
        # statement can contribute a variable, unmarked number of tree
        # children (e.g. a for loop's iterations) with no boundary marker
        # otherwise. Measured as a stack-length delta: pure bookkeeping, no
        # Manifold calls, safe here.
        #
        # Every statement is always resolved (no short-circuiting): with
        # generation fully deferred (Phase 2 final cutover), resolve can no
        # longer tell whether a statement's geometry is empty — that's only
        # knowable once it's actually generated. _generate_csg re-derives
        # the discard-vs-skip short-circuit semantics itself, from real
        # generated bodies, using these same group_sizes to re-chunk children.
        op = node.name.name
        args, ctx = self._resolve_call_args(node, ctx)
        assign_nodes = [c for c in node.children if isinstance(c, Assignment)]
        geo_nodes = [c for c in node.children
                     if not isinstance(c, (Assignment, ModuleDeclaration, FunctionDeclaration))]

        # Process assignments first for side-effects (they update ctx.dyn in-place)
        if assign_nodes:
            self._eval_children(assign_nodes, ctx)

        group_sizes: list[int] = []
        for geo_node in geo_nodes:
            before = len(self._tree_stack[-1])
            self._eval_children([geo_node], ctx)
            after = len(self._tree_stack[-1])
            group_sizes.append(after - before)
        return {"op": op, "group_sizes": group_sizes}

    def _generate_csg(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        op = params["op"]
        all_bg: list[ColoredBody] = []
        all_hi: list[ColoredBody] = []
        all_so: list[ColoredBody] = []
        csg_result: Optional[ColoredBody] = None
        idx = 0

        for size in params["group_sizes"]:
            group_nodes = children[idx:idx + size]
            idx += size
            stmt_bodies = flatten_csg_tree(group_nodes)

            bg, fg, hi, so = self._split_by_role(stmt_bodies)
            all_bg.extend(bg)
            all_hi.extend(hi)
            all_so.extend(so)

            bodies_3d = [c for c in fg if c.body is not None]
            sections_2d = [c for c in fg if c.section is not None]

            if not bodies_3d and not sections_2d:
                # Empty statement: intersection(∅, B)=∅ discards any csg_result
                # already built from prior statements (matches resolve's own
                # short-circuit — group_sizes never has entries past this
                # point for intersection). difference(∅, B)=∅ only applies
                # while no positive operand has been established yet; union
                # just skips the empty contributor and keeps going.
                if op == "intersection":
                    csg_result = None
                    break
                if op == "difference" and csg_result is None:
                    break
                continue

            if bodies_3d:
                # Union all 3D bodies from this statement before applying the op
                grp = bodies_3d[0].body
                for c in bodies_3d[1:]:
                    grp = grp + c.body
                if csg_result is None:
                    csg_result = ColoredBody(body=grp, color=bodies_3d[0].color)
                elif op == "union":
                    csg_result = replace(csg_result, body=csg_result.body + grp)
                elif op == "difference":
                    csg_result = replace(csg_result, body=csg_result.body - grp)
                elif op == "intersection":
                    csg_result = replace(csg_result, body=csg_result.body ^ grp)
            elif sections_2d:
                # Union all 2D sections from this statement before applying the op
                grp = sections_2d[0].section
                for c in sections_2d[1:]:
                    grp = grp + c.section
                if csg_result is None:
                    csg_result = ColoredBody(section=grp, color=sections_2d[0].color)
                elif op == "union":
                    csg_result = replace(csg_result, section=csg_result.section + grp)
                elif op == "difference":
                    csg_result = replace(csg_result, section=csg_result.section - grp)
                elif op == "intersection":
                    csg_result = replace(csg_result, section=csg_result.section ^ grp)

        # Return: CSG result + background ghosts + highlight overlays + show_only bodies (all separate from CSG result)
        return ([csg_result] if csg_result is not None else []) + all_bg + all_hi + all_so

    @staticmethod
    def _split_by_role(bodies: list[ColoredBody]) -> tuple[list[ColoredBody], list[ColoredBody], list[ColoredBody], list[ColoredBody]]:
        """Split a flat body list into (background, foreground,
        highlight_ghost, show_only) per the %/#/! modifier convention —
        shared by hull/minkowski's generate steps (previously duplicated
        identically in both, plus a per-statement-group variant in
        _generate_csg). show_only (!) bodies must come out of `fg` just
        like background/highlight do: they were being unioned/hulled/
        minkowski-summed together with ordinary bodies, which silently
        strips their role (the operation's result has no role at all),
        so ! stopped isolating its subtree the moment it was nested inside
        any boolean op/hull/minkowski rather than sitting at the top
        level — evaluate()'s "any show_only body anywhere -> show only
        those + highlights" check at the very end never saw one."""
        bg = [c for c in bodies if c.role == "background"]
        fg = [c for c in bodies if c.role not in ("background", "show_only")]
        hi = [replace(c, role="highlight_ghost") for c in fg if c.role == "highlight"]
        so = [c for c in bodies if c.role == "show_only"]
        return bg, fg, hi, so

    def _resolve_hull(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        return {}

    def _generate_hull(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        bodies = flatten_csg_tree(children)
        if not bodies:
            return []
        bg, fg, hi, so = self._split_by_role(bodies)
        hull_result: Optional[ColoredBody] = None
        if fg:
            bodies_3d = [c.body for c in fg if c.body is not None]
            if bodies_3d:
                hull_result = ColoredBody(body=m3d.Manifold.batch_hull(bodies_3d), color=fg[0].color)
            else:
                sections = [c.section for c in fg if c.section is not None]
                if sections:
                    hull_result = ColoredBody(section=m3d.CrossSection.batch_hull(sections), color=fg[0].color)
        return ([hull_result] if hull_result is not None else []) + bg + hi + so

    def _resolve_polyhedron(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        points = self._get_arg(args, 0, "points", None)
        faces = self._get_arg(args, 1, "faces", None)
        if faces is None:
            faces = self._get_arg(args, 1, "triangles", None)  # legacy alias
        if points is None or faces is None:
            self.error("polyhedron: 'points' and 'faces' are required", node)
        if not isinstance(points, list) or not isinstance(faces, list):
            self.error("polyhedron: 'points' and 'faces' must be lists", node)
        for i, p in enumerate(points):
            if not isinstance(p, list) or len(p) != 3 or any(c is None for c in p):
                self.error(f"polyhedron: point[{i}] is not a valid [x,y,z] coordinate", node)
        try:
            verts = np.array([[float(c) for c in p] for p in points], dtype=np.float64)
            # Deduplicate vertices — VNF meshes (e.g. from BOSL2) often have
            # coincident vertices at seams/poles that must be merged for Manifold.
            rounded = np.round(verts, decimals=6)
            _, unique_idx, remap = np.unique(rounded, axis=0, return_index=True, return_inverse=True)
            verts = verts[unique_idx].astype(np.float32)
            # Fan-triangulate faces, reversing winding to convert OpenSCAD's
            # CW-from-outside convention to Manifold's CCW-from-outside convention.
            tris = []
            for face in faces:
                face = [int(x) for x in face]
                remapped = [int(remap[idx]) for idx in face]
                for i in range(1, len(remapped) - 1):
                    a, b, c = remapped[0], remapped[i + 1], remapped[i]
                    if a != b and b != c and a != c:
                        tris.append([a, b, c])
            tri_arr = np.array(tris, dtype=np.uint32) if tris else np.zeros((0, 3), dtype=np.uint32)
        except Exception as e:
            self.error(f"polyhedron: {e}", node)
        return {"verts": verts, "tri_arr": tri_arr, "color": ctx.color}

    def _generate_polyhedron(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        try:
            mesh = m3d.Mesh(vert_properties=params["verts"], tri_verts=params["tri_arr"])
            body = m3d.Manifold(mesh)
            return [self._tag_generated(body, node, params["color"])]
        except Exception as e:
            self.error(f"polyhedron: {e}", node)

    def _resolve_surface(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        file_arg = self._get_arg(args, 0, "file", None)
        center = bool(self._get_arg(args, None, "center", False))
        invert = bool(self._get_arg(args, None, "invert", False))
        color = ctx.color

        if file_arg is None:
            self.error("surface: 'file' parameter is required", node)
            return {"heights": None, "center": center, "color": color}

        # Resolve path relative to the source file
        base_dir = None
        pos = getattr(node, 'position', None)
        if pos and getattr(pos, 'origin', None):
            import os as _os
            base_dir = _os.path.dirname(pos.origin)
        if base_dir:
            import os as _os
            file_path = _os.path.join(base_dir, str(file_arg)) if not _os.path.isabs(str(file_arg)) else str(file_arg)
        else:
            file_path = str(file_arg)

        try:
            heights = self._surface_load(file_path, invert)
        except Exception as e:
            self.error(f"surface: {e}", node)
            return {"heights": None, "center": center, "color": color}

        if heights is None or len(heights) == 0 or len(heights[0]) == 0:
            self.error("surface: empty height data", node)
            return {"heights": None, "center": center, "color": color}

        return {"heights": heights, "center": center, "color": color}

    def _generate_surface(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        heights = params["heights"]
        if heights is None:
            return []
        center = params["center"]

        rows = len(heights)
        cols = len(heights[0])

        x_off = -(cols - 1) / 2.0 if center else 0.0
        y_off = -(rows - 1) / 2.0 if center else 0.0

        # Build vertex grid: (cols) * (rows) top vertices + same for bottom (z=0)
        # top verts: index = row * cols + col
        # bottom verts: index = rows*cols + row * cols + col
        n = rows * cols
        verts = []
        for r in range(rows):
            for c in range(cols):
                verts.append([c + x_off, r + y_off, float(heights[r][c])])
        for r in range(rows):
            for c in range(cols):
                verts.append([c + x_off, r + y_off, 0.0])

        tris = []

        def top(r, c):
            return r * cols + c

        def bot(r, c):
            return n + r * cols + c

        # Top surface (CCW from above = outward upward normal)
        for r in range(rows - 1):
            for c in range(cols - 1):
                tl, tr, bl, br = top(r+1, c), top(r+1, c+1), top(r, c), top(r, c+1)
                tris.append([tl, bl, br])
                tris.append([tl, br, tr])

        # Bottom face (CCW from below = outward downward normal)
        for r in range(rows - 1):
            for c in range(cols - 1):
                tl, tr, bl, br = bot(r+1, c), bot(r+1, c+1), bot(r, c), bot(r, c+1)
                tris.append([tl, tr, br])
                tris.append([tl, br, bl])

        # Side walls (outward normals: front=-Y, back=+Y, left=-X, right=+X)
        for c in range(cols - 1):  # front (r=0, outward=-Y)
            tris.append([top(0, c), bot(0, c), bot(0, c+1)])
            tris.append([top(0, c), bot(0, c+1), top(0, c+1)])
        for c in range(cols - 1):  # back (r=rows-1, outward=+Y)
            tris.append([top(rows-1, c), top(rows-1, c+1), bot(rows-1, c+1)])
            tris.append([top(rows-1, c), bot(rows-1, c+1), bot(rows-1, c)])
        for r in range(rows - 1):  # left (c=0, outward=-X)
            tris.append([top(r, 0), top(r+1, 0), bot(r+1, 0)])
            tris.append([top(r, 0), bot(r+1, 0), bot(r, 0)])
        for r in range(rows - 1):  # right (c=cols-1, outward=+X)
            tris.append([top(r, cols-1), bot(r+1, cols-1), top(r+1, cols-1)])
            tris.append([top(r, cols-1), bot(r, cols-1), bot(r+1, cols-1)])

        try:
            verts_arr = np.array(verts, dtype=np.float32)
            tris_arr = np.array(tris, dtype=np.uint32)
            mesh = m3d.Mesh(vert_properties=verts_arr, tri_verts=tris_arr)
            body = m3d.Manifold(mesh)
            return [self._tag_generated(body, node, params["color"])]
        except Exception as e:
            self.error(f"surface: mesh construction failed: {e}", node)
            return []

    def _surface_load(self, file_path: str, invert: bool):
        """Load height data from a .dat text file or a PNG image."""
        import os as _os
        ext = _os.path.splitext(file_path)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
            return self._surface_load_image(file_path, invert)
        return self._surface_load_dat(file_path)

    def _surface_load_dat(self, file_path: str):
        heights = []
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                heights.append([float(v) for v in line.split()])
        heights.reverse()  # first row in file = highest Y (OpenSCAD convention)
        return heights

    def _surface_load_image(self, file_path: str, invert: bool):
        try:
            from PIL import Image
        except ImportError:
            raise RuntimeError("Pillow is required for image-based surface() — install it with: uv add Pillow")
        img = Image.open(file_path).convert("RGB")
        w, h = img.size
        pixels = img.load()
        heights = []
        for row in range(h - 1, -1, -1):  # bottom row of image = Y=0
            r_vals = []
            for col in range(w):
                r, g, b = pixels[col, row]
                gray = 0.2126 * r + 0.7152 * g + 0.0722 * b  # linear luminance
                val = (255.0 - gray) / 255.0 * 100.0 if invert else gray / 255.0 * 100.0
                r_vals.append(val)
            heights.append(r_vals)
        return heights

    # ------------------------------------------------------------------
    # import() — 3D mesh, 2D geometry, JSON
    # ------------------------------------------------------------------

    def _resolve_import_path(self, file_arg: Any, node) -> str:
        import os as _os
        pos = getattr(node, "position", None)
        base_dir = _os.path.dirname(pos.origin) if pos and getattr(pos, "origin", None) else None
        path = str(file_arg) if file_arg is not None else ""
        if base_dir and not _os.path.isabs(path):
            path = _os.path.join(base_dir, path)
        return path

    def _resolve_import(self, node: ModularCall, ctx: EvalContext) -> dict:
        """Loads the file's raw data (verts/tris, or 2D contours) as plain
        data during resolve — all pure file I/O and numpy math, no Manifold
        calls — so generate only needs to build the Manifold/CrossSection
        from already-parsed data, matching the caching approach used for
        surface(). Mirrors _builtin_import's per-extension dispatch and
        each removed _import_X_geometry wrapper's own exception handling
        exactly, so observable errors/warnings are unchanged."""
        import os as _os
        args, ctx = self._resolve_call_args(node, ctx)
        file_arg = self._get_arg(args, 0, "file", None)
        layer    = self._get_arg(args, None, "layer", None)
        color = ctx.color
        if file_arg is None:
            self.error("import: 'file' parameter is required", node)
            return {"color": color}
        path = self._resolve_import_path(file_arg, node)
        ext  = _os.path.splitext(path)[1].lower()
        try:
            if ext in (".stl", ".obj", ".off", ".3mf"):
                loader = {".stl": self._load_stl, ".obj": self._load_obj,
                          ".off": self._load_off, ".3mf": self._load_3mf}[ext]
                try:
                    verts, tris = loader(path)
                except Exception as e:
                    self.error(f"import: {e}", node)
                    return {"color": color}
                return {"kind": "mesh", "verts": verts, "tris": tris, "color": color}
            elif ext == ".dxf":
                contours = self._load_dxf_contours(path, layer, node)
                return {"kind": "dxf", "contours": contours, "color": color}
            elif ext in (".svg", ".pdf"):
                try:
                    contours = self._load_svg_contours(path)
                except Exception as e:
                    self.error(f"import: {e}", node)
                    return {"color": color}
                return {"kind": "svg", "contours": contours, "color": color}
            elif ext == ".json":
                self.error("import: .json returns data, not geometry — use as an expression", node)
                return {"color": color}
            else:
                self.error(f"import: unsupported file type '{ext}'", node)
                return {"color": color}
        except OSError as e:
            self.error(f"import: {e}", node)
            return {"color": color}

    def _generate_import(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        kind = params.get("kind")
        color = params["color"]
        if kind == "mesh":
            body = self._mesh_to_colored_body_generate(params["verts"], params["tris"], node, color)
            return self._body_list(body)
        if kind == "dxf":
            contours = params["contours"]
            if contours is None:
                return []
            if not contours:
                self.error("import: no closed contours found in DXF file", node)
                return []
            polys = [np.array(c, dtype=np.float64) for c in contours]
            cs = m3d.CrossSection(polys, m3d.FillRule.EvenOdd)
            return [ColoredBody(section=cs, color=color)]
        if kind == "svg":
            contours = params["contours"]
            if not contours:
                self.error("import: no shapes found in SVG file", node)
                return []
            polys = [np.array(c, dtype=np.float64) for c in contours]
            cs = m3d.CrossSection(polys, m3d.FillRule.EvenOdd)
            return [ColoredBody(section=cs, color=color)]
        return []

    def _import_as_value(self, args: dict, node) -> Any:
        import os as _os
        file_arg = self._get_arg(args, 0, "file", None)
        layer    = self._get_arg(args, None, "layer", None)
        if file_arg is None:
            self.error("import: 'file' parameter is required", node)
            return None
        path = self._resolve_import_path(file_arg, node)
        ext  = _os.path.splitext(path)[1].lower()
        try:
            if ext == ".json":
                import json as _json
                with open(path, "r", encoding="utf-8") as f:
                    return self._json_to_osc(_json.load(f))
            elif ext in (".stl", ".obj", ".off", ".3mf"):
                return self._import_as_vnf(path, ext, node)
            elif ext in (".dxf", ".svg"):
                return self._import_as_region(path, ext, layer, node)
            else:
                self.error(f"import: unsupported file type '{ext}'", node)
                return None
        except OSError as e:
            self.error(f"import: {e}", node)
            return None

    def _import_as_vnf(self, path: str, ext: str, node) -> Any:
        """Load a mesh file and return a VNF: [[verts], [faces]]."""
        try:
            if ext == ".stl":
                raw_verts, raw_tris = self._load_stl(path)
            elif ext == ".obj":
                raw_verts, raw_tris = self._load_obj(path)
            elif ext == ".off":
                raw_verts, raw_tris = self._load_off(path)
            else:
                raw_verts, raw_tris = self._load_3mf(path)
        except Exception as e:
            self.error(f"import: {e}", node)
            return None
        vert_map: dict[tuple, int] = {}
        verts_out: list[list[float]] = []
        faces_out: list[list[int]] = []
        raw_verts_list = list(raw_verts)  # handle numpy arrays
        for face in raw_tris:
            fi = []
            for vi in face:
                v = raw_verts_list[int(vi)]
                key = (float(v[0]), float(v[1]), float(v[2]))
                if key not in vert_map:
                    vert_map[key] = len(verts_out)
                    verts_out.append(list(key))
                fi.append(vert_map[key])
            faces_out.append(fi)
        return [verts_out, faces_out]

    def _import_as_region(self, path: str, ext: str, layer: Any, node) -> Any:
        """Load a 2D file and return a Region: [[[x,y],...], ...]."""
        try:
            if ext == ".dxf":
                contours = self._load_dxf_contours(path, layer, node)
            else:
                contours = self._load_svg_contours(path)
        except Exception as e:
            self.error(f"import: {e}", node)
            return None
        if contours is None:
            return None
        return [[[pt[0], pt[1]] for pt in c] for c in contours]

    def _json_to_osc(self, v: Any) -> Any:
        """Recursively convert JSON-parsed Python value to evaluator-native types.
        JSON objects → OscObject; arrays/scalars pass through as-is."""
        if isinstance(v, dict):
            return OscObject({k: self._json_to_osc(val) for k, val in v.items()})
        if isinstance(v, list):
            return [self._json_to_osc(x) for x in v]
        return v  # str, int, float, bool, None — all native

    def _mesh_to_colored_body_generate(self, verts: Any, tris: Any, node, color) -> Optional[ColoredBody]:
        if len(tris) == 0:
            self.error("import: mesh has no triangles", node)
            return None
        try:
            verts_arr = np.asarray(verts, dtype=np.float64)
            tri_arr   = np.asarray(tris,  dtype=np.uint32)
            mesh = m3d.Mesh(vert_properties=verts_arr, tri_verts=tri_arr)
            body = m3d.Manifold(mesh)
        except Exception as e:
            self.error(f"import: mesh construction failed: {e}", node)
            return None
        if body.status() != m3d.Error.NoError:
            pos = getattr(node, "position", None)
            self._echo_fn(f"WARNING: import: mesh is not manifold ({body.status()}){self._loc(pos)}")
        return self._tag_generated(body, node, color)

    def _load_stl(self, path: str):
        """Return (verts, tris) from binary or ASCII STL."""
        import struct as _struct
        with open(path, "rb") as f:
            header = f.read(80)
            rest   = f.read()
        try:
            sample = (header + rest[:256]).decode("ascii", errors="ignore")
            is_ascii = "facet normal" in sample
        except Exception:
            is_ascii = False
        if is_ascii:
            text = (header + rest).decode("ascii", errors="replace")
            verts: list = []; tris: list = []; tri_verts: list = []
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("vertex "):
                    parts = line.split()
                    tri_verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
                    if len(tri_verts) == 3:
                        base = len(verts)
                        verts.extend(tri_verts)
                        tris.append([base, base + 1, base + 2])
                        tri_verts = []
        else:
            count = _struct.unpack_from("<I", rest, 0)[0]
            dtype = np.dtype([("normal", np.float32, (3,)),
                              ("v0", np.float32, (3,)), ("v1", np.float32, (3,)),
                              ("v2", np.float32, (3,)), ("attr", np.uint16)])
            data  = np.frombuffer(rest[4:4 + count * 50], dtype=dtype)
            verts = np.empty((count * 3, 3), dtype=np.float64)
            verts[0::3] = data["v0"]; verts[1::3] = data["v1"]; verts[2::3] = data["v2"]
            tris = np.arange(count * 3, dtype=np.uint32).reshape(-1, 3)
        return self._weld_stl_vertices(verts, tris)

    def _weld_stl_vertices(self, verts, tris):
        """STL has no vertex-index concept -- each triangle carries its own
        private copy of its 3 corner positions -- so a naive load produces a
        "vertex soup" with no shared indices at shared edges. manifold3d
        requires welded/shared indices to recognize a mesh as a closed
        manifold: confirmed empirically that the exact same cube topology
        already validated via _UNIT_CUBE_OBJ (volume 1, Error.NoError) comes
        back as Error.NotManifold / volume 0 once expanded into a vertex
        soup, even though it's a perfectly valid closed solid. Merging
        coincident vertices (exact-match, which is what matters for STL's
        own repeated-corner floats) and remapping triangle indices through
        the merge fixes this for every STL, not just malformed ones."""
        verts_arr = np.asarray(verts, dtype=np.float64)
        if len(verts_arr) == 0:
            return verts_arr, np.asarray(tris, dtype=np.uint32)
        unique_verts, inverse = np.unique(verts_arr, axis=0, return_inverse=True)
        welded_tris = np.asarray(inverse, dtype=np.uint32).reshape(-1)[np.asarray(tris, dtype=np.int64)]
        return unique_verts, welded_tris

    def _load_obj(self, path: str):
        verts: list[list[float]] = []; tris: list[list[int]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("v "):
                    p = line.split()
                    verts.append([float(p[1]), float(p[2]), float(p[3])])
                elif line.startswith("f "):
                    idx = [int(p.split("/")[0]) - 1 for p in line.split()[1:]]
                    for i in range(1, len(idx) - 1):
                        tris.append([idx[0], idx[i], idx[i + 1]])
        return verts, tris

    def _load_off(self, path: str):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        idx = 0
        if lines[idx].upper().startswith("OFF"):
            idx += 1
        n_v, n_f, _ = (int(x) for x in lines[idx].split()); idx += 1
        verts = []
        for _ in range(n_v):
            p = lines[idx].split(); verts.append([float(p[0]), float(p[1]), float(p[2])]); idx += 1
        tris: list[list[int]] = []
        for _ in range(n_f):
            p = [int(x) for x in lines[idx].split()]; idx += 1
            cnt, face_idx = p[0], p[1:p[0] + 1]
            for i in range(1, cnt - 1):
                tris.append([face_idx[0], face_idx[i], face_idx[i + 1]])
        return verts, tris

    def _load_3mf(self, path: str):
        import zipfile as _zf
        import xml.etree.ElementTree as _ET
        NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
        verts_all: list[list[float]] = []; tris_all: list[list[int]] = []
        with _zf.ZipFile(path) as z:
            model_name = next((n for n in z.namelist() if n.lower().endswith("3dmodel.model")), None)
            if model_name is None:
                raise ValueError("No 3dmodel.model found in 3MF archive")
            with z.open(model_name) as f:
                tree = _ET.parse(f)
        for mesh_el in tree.iter(f"{{{NS}}}mesh"):
            verts_el = mesh_el.find(f"{{{NS}}}vertices")
            tris_el  = mesh_el.find(f"{{{NS}}}triangles")
            if verts_el is None or tris_el is None:
                continue
            base = len(verts_all)
            for v in verts_el:
                verts_all.append([float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))])
            for t in tris_el:
                tris_all.append([base + int(t.get("v1")), base + int(t.get("v2")), base + int(t.get("v3"))])
        return verts_all, tris_all

    def _load_dxf_contours(self, path: str, layer: Any, node) -> Optional[list]:
        try:
            import ezdxf as _ezdxf
        except ImportError:
            self.error("import: DXF requires the 'ezdxf' library (pip install ezdxf)", node)
            return None
        doc = _ezdxf.readfile(path)
        msp = doc.modelspace()
        contours: list[list[tuple[float, float]]] = []
        for entity in msp:
            if layer is not None and entity.dxf.layer != str(layer):
                continue
            dtype = entity.dxftype()
            if dtype == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in entity.get_points()]
                if pts and entity.is_closed:
                    contours.append(pts)
            elif dtype == "POLYLINE" and entity.is_2d_polyline:
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                if pts and entity.is_closed:
                    contours.append(pts)
        return contours

    def _load_svg_contours(self, path: str) -> list[list[tuple[float, float]]]:
        import xml.etree.ElementTree as _ET
        import re as _re
        import math as _math

        SEGS = 32

        def _parse_transform(t_str: str) -> np.ndarray:
            m = np.eye(3, dtype=np.float64)
            if not t_str:
                return m
            for cmd, args_s in _re.findall(r'(\w+)\(([^)]*)\)', t_str):
                ns = [float(x) for x in _re.split(r'[,\s]+', args_s.strip()) if x]
                if cmd == "matrix" and len(ns) >= 6:
                    a, b, c, d, e, f = ns[:6]
                    m = np.array([[a, c, e], [b, d, f], [0, 0, 1]], dtype=np.float64) @ m
                elif cmd == "translate":
                    tx, ty = ns[0], ns[1] if len(ns) > 1 else 0.0
                    m = np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]], dtype=np.float64) @ m
                elif cmd == "scale":
                    sx, sy = ns[0], ns[1] if len(ns) > 1 else ns[0]
                    m = np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]], dtype=np.float64) @ m
                elif cmd == "rotate":
                    a  = _math.radians(ns[0])
                    cx = ns[1] if len(ns) > 1 else 0.0
                    cy = ns[2] if len(ns) > 2 else 0.0
                    ca, sa = _math.cos(a), _math.sin(a)
                    t1 = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]], dtype=np.float64)
                    r  = np.array([[ca, -sa, 0], [sa, ca, 0], [0, 0, 1]], dtype=np.float64)
                    t2 = np.array([[1, 0, cx], [0, 1, cy], [0, 0, 1]], dtype=np.float64)
                    m  = t2 @ r @ t1 @ m
            return m

        def _apply(pt: tuple, mat: np.ndarray) -> tuple:
            v = mat @ np.array([pt[0], pt[1], 1.0])
            return (float(v[0]), float(-v[1]))  # flip Y: SVG down→OpenSCAD up

        def _cubic(p0, p1, p2, p3):
            pts = []
            for i in range(1, SEGS + 1):
                t = i / SEGS; mt = 1 - t
                pts.append((mt**3*p0[0]+3*mt**2*t*p1[0]+3*mt*t**2*p2[0]+t**3*p3[0],
                             mt**3*p0[1]+3*mt**2*t*p1[1]+3*mt*t**2*p2[1]+t**3*p3[1]))
            return pts

        def _quad(p0, p1, p2):
            pts = []
            for i in range(1, SEGS + 1):
                t = i / SEGS; mt = 1 - t
                pts.append((mt**2*p0[0]+2*mt*t*p1[0]+t**2*p2[0],
                             mt**2*p0[1]+2*mt*t*p1[1]+t**2*p2[1]))
            return pts

        def _arc(x1, y1, rx, ry, x_rot, large, sweep, x2, y2):
            if rx == 0 or ry == 0:
                return [(x2, y2)]
            cos_r = _math.cos(_math.radians(x_rot)); sin_r = _math.sin(_math.radians(x_rot))
            dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
            x1p =  cos_r*dx + sin_r*dy; y1p = -sin_r*dx + cos_r*dy
            lam = (x1p/rx)**2 + (y1p/ry)**2
            if lam > 1:
                rx *= _math.sqrt(lam); ry *= _math.sqrt(lam)
            sq = max(0.0, (rx*ry)**2 - (rx*y1p)**2 - (ry*x1p)**2)
            sq = _math.sqrt(sq / max(1e-12, (rx*y1p)**2 + (ry*x1p)**2))
            if large == sweep:
                sq = -sq
            cxp = sq*rx*y1p/ry; cyp = -sq*ry*x1p/rx
            cx = cos_r*cxp - sin_r*cyp + (x1+x2)/2
            cy = sin_r*cxp + cos_r*cyp + (y1+y2)/2
            def _angle(ux, uy, vx, vy): return _math.atan2(ux*vy - uy*vx, ux*vx + uy*vy)
            th1 = _angle(1, 0, (x1p-cxp)/rx, (y1p-cyp)/ry)
            dth = _angle((x1p-cxp)/rx, (y1p-cyp)/ry, (-x1p-cxp)/rx, (-y1p-cyp)/ry)
            if sweep == 0 and dth > 0: dth -= 2*_math.pi
            if sweep == 1 and dth < 0: dth += 2*_math.pi
            n = max(4, int(abs(dth)/(2*_math.pi)*SEGS*4))
            pts = []
            for i in range(1, n + 1):
                th = th1 + dth*i/n
                pts.append((cos_r*rx*_math.cos(th) - sin_r*ry*_math.sin(th) + cx,
                             sin_r*rx*_math.cos(th) + cos_r*ry*_math.sin(th) + cy))
            return pts

        def _parse_d(d: str, mat: np.ndarray) -> list:
            toks = _re.findall(
                r'[MmZzLlHhVvCcSsQqTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d)
            contours: list = []; contour: list = []
            cur = (0.0, 0.0); start = (0.0, 0.0); last_ctrl = None; cmd = "M"; ti = 0

            def nx():
                nonlocal ti; v = float(toks[ti]); ti += 1; return v

            while ti < len(toks):
                t = toks[ti]
                if t in "MmZzLlHhVvCcSsQqTtAa":
                    cmd = t; ti += 1; last_ctrl = None; continue
                rel = cmd.islower(); ox, oy = cur if rel else (0.0, 0.0); lc = cmd.upper()
                if lc == "M":
                    if contour: contours.append(contour)
                    cur = (nx()+ox, nx()+oy); start = cur
                    contour = [_apply(cur, mat)]; cmd = "l" if rel else "L"
                elif lc == "Z":
                    if contour: contours.append(contour)
                    cur = start; contour = []
                elif lc == "L":
                    cur = (nx()+ox, nx()+oy); contour.append(_apply(cur, mat))
                elif lc == "H":
                    cur = (nx()+ox, cur[1]); contour.append(_apply(cur, mat))
                elif lc == "V":
                    cur = (cur[0], nx()+oy); contour.append(_apply(cur, mat))
                elif lc == "C":
                    p1 = (nx()+ox, nx()+oy); p2 = (nx()+ox, nx()+oy); p3 = (nx()+ox, nx()+oy)
                    last_ctrl = p2
                    for pt in _cubic(cur, p1, p2, p3): contour.append(_apply(pt, mat))
                    cur = p3
                elif lc == "S":
                    refl = (2*cur[0]-last_ctrl[0], 2*cur[1]-last_ctrl[1]) if last_ctrl else cur
                    p2 = (nx()+ox, nx()+oy); p3 = (nx()+ox, nx()+oy); last_ctrl = p2
                    for pt in _cubic(cur, refl, p2, p3): contour.append(_apply(pt, mat))
                    cur = p3
                elif lc == "Q":
                    p1 = (nx()+ox, nx()+oy); p2 = (nx()+ox, nx()+oy); last_ctrl = p1
                    for pt in _quad(cur, p1, p2): contour.append(_apply(pt, mat))
                    cur = p2
                elif lc == "T":
                    refl = (2*cur[0]-last_ctrl[0], 2*cur[1]-last_ctrl[1]) if last_ctrl else cur
                    p2 = (nx()+ox, nx()+oy); last_ctrl = refl
                    for pt in _quad(cur, refl, p2): contour.append(_apply(pt, mat))
                    cur = p2
                elif lc == "A":
                    rx2, ry2, xr, lg, sw = nx(), nx(), nx(), int(nx()), int(nx())
                    ex, ey = nx()+ox, nx()+oy
                    for pt in _arc(cur[0], cur[1], rx2, ry2, xr, lg, sw, ex, ey):
                        contour.append(_apply(pt, mat))
                    cur = (ex, ey)
            if contour:
                contours.append(contour)
            return contours

        def _shape_contours(el, mat: np.ndarray) -> list:
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag == "path":
                return _parse_d(el.get("d", ""), mat)
            if tag in ("polygon", "polyline"):
                # Both treated as closed fill contours here (this import
                # path only ever produces closed CrossSection polygons,
                # same as the DXF loader) -- <polyline> is nominally an
                # open SVG shape, but there's no "open path" concept
                # downstream to preserve, so closing it is the only useful
                # interpretation. Previously restricted to tag ==
                # "polygon", which made every <polyline> a silent no-op.
                nums = [float(x) for x in _re.split(r'[,\s]+', el.get("points", "").strip()) if x]
                pts = list(zip(nums[::2], nums[1::2]))
                return [[_apply(p, mat) for p in pts]] if pts else []
            if tag == "rect":
                x = float(el.get("x", 0)); y = float(el.get("y", 0))
                w = float(el.get("width", 0)); h = float(el.get("height", 0))
                pts = [(x, y), (x+w, y), (x+w, y+h), (x, y+h)]
                return [[_apply(p, mat) for p in pts]]
            if tag == "circle":
                cx = float(el.get("cx", 0)); cy = float(el.get("cy", 0)); r = float(el.get("r", 0))
                pts = [(cx+r*_math.cos(2*_math.pi*i/SEGS), cy+r*_math.sin(2*_math.pi*i/SEGS))
                       for i in range(SEGS)]
                return [[_apply(p, mat) for p in pts]]
            if tag == "ellipse":
                cx = float(el.get("cx", 0)); cy = float(el.get("cy", 0))
                rx = float(el.get("rx", 0)); ry = float(el.get("ry", 0))
                pts = [(cx+rx*_math.cos(2*_math.pi*i/SEGS), cy+ry*_math.sin(2*_math.pi*i/SEGS))
                       for i in range(SEGS)]
                return [[_apply(p, mat) for p in pts]]
            return []

        def _walk(el, mat: np.ndarray) -> list:
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag in ("defs", "symbol"):
                return []
            m = _parse_transform(el.get("transform", "")) @ mat
            out = _shape_contours(el, m)
            for child in el:
                out.extend(_walk(child, m))
            return out

        tree = _ET.parse(path)
        return _walk(tree.getroot(), np.eye(3, dtype=np.float64))

    def _resolve_offset(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        r = self._get_arg(args, None, "r", None)
        delta = self._get_arg(args, None, "delta", None)
        chamfer = bool(self._get_arg(args, None, "chamfer", False))
        segs = self._fn(ctx, abs(float(r))) if r is not None else None
        return {"r": r, "delta": delta, "chamfer": chamfer, "segs": segs, "color": ctx.color}

    def _generate_offset(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        bodies = flatten_csg_tree(children)
        cs = self._to_cross_section(bodies)
        if cs is None:
            return []
        r, delta, chamfer = params["r"], params["delta"], params["chamfer"]
        if r is not None:
            result = cs.offset(float(r), m3d.JoinType.Round, circular_segments=params["segs"])
        elif delta is not None:
            jt = m3d.JoinType.Miter if chamfer else m3d.JoinType.Square
            result = cs.offset(float(delta), jt)
        else:
            return [bodies[0]] if bodies else []
        return [ColoredBody(section=result, color=params["color"])]

    def _resolve_projection(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        return {"cut": bool(self._get_arg(args, None, "cut", False))}

    def _generate_projection(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        bodies = flatten_csg_tree(children)
        bodies_3d = [c for c in bodies if c.body is not None]
        if not bodies_3d:
            return []
        combined = self._combine(bodies_3d).body
        try:
            if params["cut"]:
                cs = combined.slice(0.0)
            else:
                raw = combined.project()
                # project() may produce self-intersecting polygons; re-fill to clean up
                polys = raw.to_polygons()
                cs = m3d.CrossSection(polys, m3d.FillRule.Positive) if polys else raw
            return [ColoredBody(section=cs, color=bodies_3d[0].color)]
        except Exception as e:
            self.error(f"projection: {e}", node)

    def _resolve_2d(self, node: ModularCall, ctx: EvalContext) -> dict:
        """circle/square/polygon share one dispatch entry, matching
        _builtin_2d's own name-based if/elif structure (kind == name for a
        ModularCall, so name is re-derived from node.name.name here)."""
        name = node.name.name
        args, ctx = self._resolve_call_args(node, ctx)
        try:
            if name == "circle":
                r = self._get_arg(args, 0, "r", None)
                d = self._get_arg(args, None, "d", None)
                if d is not None:
                    r = d / 2
                if r is None:
                    r = 1.0
                r = float(r)
                segs = self._fn(ctx, r)
                return {"name": name, "r": r, "segs": segs, "color": ctx.color}
            if name == "square":
                size = self._get_arg(args, 0, "size", 1.0)
                center = bool(self._get_arg(args, 1, "center", False))
                if isinstance(size, (int, float)):
                    size = [size, size]
                return {"name": name, "size": [float(size[0]), float(size[1])],
                        "center": center, "color": ctx.color}
            # polygon
            points = self._get_arg(args, 0, "points", None)
            paths = self._get_arg(args, 1, "paths", None)
            if points is None:
                self.error("polygon: 'points' is required", node)
            pts = [[float(p[0]), float(p[1])] for p in points]
            path_indices = None if paths is None else [[int(i) for i in path] for path in paths]
            return {"name": name, "pts": pts, "paths": path_indices, "color": ctx.color}
        except Exception as e:
            self.error(f"{name}: {e}", node)

    def _generate_2d(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        name = params["name"]
        try:
            if name == "circle":
                cs = m3d.CrossSection.circle(params["r"], params["segs"])
            elif name == "square":
                cs = m3d.CrossSection.square(params["size"], params["center"])
            else:  # polygon
                pts, paths = params["pts"], params["paths"]
                if paths is None:
                    contour = np.array(pts, dtype=np.float64)
                    cs = m3d.CrossSection([contour], m3d.FillRule.EvenOdd)
                else:
                    contours = [np.array([pts[i] for i in path], dtype=np.float64) for path in paths]
                    cs = m3d.CrossSection(contours, m3d.FillRule.EvenOdd)
            return [ColoredBody(section=cs, color=params["color"])]
        except Exception as e:
            self.error(f"{name}: {e}", node)

    def _resolve_text(self, node: ModularCall, ctx: EvalContext) -> dict:
        """`text(text=.., size=.., font=.., halign=.., valign=.., spacing=..)`.

        Renders `text` as 2D glyph outlines, using the font specified by `font=`
        (an OpenSCAD/fontconfig pattern such as `"Times New Roman:style=Bold"`).
        Resolved via `fc-match` when available; falls back to bundled Liberation
        Sans if the font cannot be found.  `direction`, `language`, `script` are
        accepted but unused.
        """
        args, ctx = self._resolve_call_args(node, ctx)
        text = self._get_arg(args, 0, "text", "")
        size = self._get_arg(args, 1, "size", 10)
        font_spec = self._get_arg(args, None, "font", "") or ""
        halign = self._get_arg(args, None, "halign", "left")
        valign = self._get_arg(args, None, "valign", "baseline")
        spacing = self._get_arg(args, None, "spacing", 1)

        try:
            font = _resolve_font(str(font_spec))
            scale = size * (100 / 72) / font["units_per_em"]
            segs = max(2, self._fn(ctx) // 2)
            m = _measure_text(text, size, spacing, font)
            offset_x, offset_y = _text_align_offset(halign, valign, m)
        except Exception as e:
            self.error(f"text: {e}", node)
        # font_spec (not the font dict itself) is cached: _resolve_font()
        # memoizes per spec string, so re-resolving it in generate is cheap
        # and doesn't need the (possibly large) font dict carried through.
        return {"font_spec": str(font_spec), "glyphs": m["glyphs"], "scale": scale,
                "segs": segs, "offset": (offset_x, offset_y), "color": ctx.color}

    def _generate_text(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        try:
            font = _resolve_font(params["font_spec"])
            scale = params["scale"]
            sections = []
            for gname, pen_x_scaled in params["glyphs"]:
                glyph_cs = _glyph_cross_section(gname, params["segs"], font)
                sections.append(glyph_cs.scale([scale, scale]).translate([pen_x_scaled, 0]))
            cs = m3d.CrossSection.batch_boolean(sections, m3d.OpType.Add) if sections else m3d.CrossSection()
            offset_x, offset_y = params["offset"]
            cs = cs.translate([offset_x, offset_y])
            return [ColoredBody(section=cs, color=params["color"])]
        except Exception as e:
            self.error(f"text: {e}", node)

    def _resolve_linear_extrude(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        height = float(self._get_arg(args, 0, "height", 1.0))
        center = bool(self._get_arg(args, None, "center", False))
        twist = float(self._get_arg(args, None, "twist", 0.0))
        slices = int(self._get_arg(args, None, "slices", 0))
        scale = self._get_arg(args, None, "scale", None)
        if scale is None:
            scale_top = (1.0, 1.0)
        elif isinstance(scale, (int, float)):
            scale_top = (float(scale), float(scale))
        else:
            scale_top = (float(scale[0]), float(scale[1]))
        return {"height": height, "center": center, "twist": twist, "slices": slices,
                "scale_top": scale_top, "color": ctx.color}

    def _generate_linear_extrude(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        cs = self._to_cross_section(flatten_csg_tree(children))
        if cs is None:
            return []
        try:
            body = m3d.Manifold.extrude(cs, params["height"], params["slices"],
                                         -params["twist"], params["scale_top"])
            if params["center"]:
                body = body.translate([0, 0, -params["height"] / 2])
            return [self._tag_generated(body, node, params["color"])]
        except Exception as e:
            self.error(f"linear_extrude: {e}", node)
            return []

    def _resolve_rotate_extrude(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        angle = float(self._get_arg(args, 0, "angle", 360.0))
        return {"angle": angle, "fn": ctx.dyn.get("$fn", 0), "fa": ctx.dyn.get("$fa", 12.0),
                "fs": ctx.dyn.get("$fs", 2.0), "color": ctx.color}

    def _generate_rotate_extrude(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        cs = self._to_cross_section(flatten_csg_tree(children))
        if cs is None:
            return []
        # max_x depends on the merged children's bounds, which doesn't exist
        # until generate — segment count can't be precomputed in resolve the
        # way e.g. offset's can, so it's derived here from cached $fn/$fa/$fs.
        bounds = cs.bounds()
        max_x = max(abs(bounds[0]), abs(bounds[2])) if bounds else 0.0
        segs = self._fn_segments(params["fn"], params["fa"], params["fs"], max_x)
        try:
            body = cs.revolve(segs, params["angle"])
            return [self._tag_generated(body, node, params["color"])]
        except Exception as e:
            self.error(f"rotate_extrude: {e}", node)
            return []

    def _resolve_roof(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        method = self._get_arg(args, None, "method", "voronoi")
        if method not in ("voronoi", "straight"):
            self._echo_fn(f"WARNING: Unknown roof method '{method}'. Using 'voronoi'.")
            method = "voronoi"
        return {"method": method, "color": ctx.color}

    def _generate_roof(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        cs = self._to_cross_section(flatten_csg_tree(children))
        if cs is None:
            return []
        try:
            if not cs.to_polygons():
                return []
            body = _skeleton_roof(cs)
            if body is None:
                body = _skeleton_roof_general(cs)
            if body is None:
                body = self._roof_sdf_fallback(cs)
            if body is None:
                return []
            return [self._tag_generated(body, node, params["color"])]
        except Exception as e:
            self.error(f"roof: {e}", node)
            return []

    def _roof_sdf_fallback(self, cs: m3d.CrossSection) -> Optional[m3d.Manifold]:
        """Signed-distance-field/`level_set` approximation of a roof, used
        when `_skeleton_roof` doesn't apply (holes, multi-contour, or a
        mitered-offset collapse with intermediate topology events)."""
        polys = cs.to_polygons()
        if not polys:
            return None
        edge_a_list, edge_b_list = [], []
        for poly in polys:
            n = len(poly)
            for i in range(n):
                edge_a_list.append(poly[i])
                edge_b_list.append(poly[(i + 1) % n])
        edge_a = np.array(edge_a_list, dtype=np.float64)  # (E, 2)
        edge_b = np.array(edge_b_list, dtype=np.float64)  # (E, 2)
        # Precompute per-edge AB and squared-length for fast per-voxel SDF.
        ab = edge_b - edge_a  # (E, 2)
        ab_sq = np.einsum('ij,ij->i', ab, ab)  # (E,)
        raw_edges = list(zip(edge_a, edge_b))  # for even-odd test

        minx, miny, maxx, maxy = cs.bounds()
        width, height = maxx - minx, maxy - miny

        # Scan a coarse grid to find the true maximum interior distance (= roof
        # height). Bounding-box heuristics badly overestimate for thin glyphs.
        _n = 40
        max_sdf = 0.0
        for xi in range(_n):
            for yi in range(_n):
                x = minx + width * xi / (_n - 1)
                y = miny + height * yi / (_n - 1)
                p = np.array([x, y])
                pa = p - edge_a
                t = np.einsum('ij,ij->i', pa, ab) / np.where(ab_sq > 0, ab_sq, 1.0)
                t = np.clip(t, 0.0, 1.0)
                d = float(np.min(np.linalg.norm(pa - t[:, None] * ab, axis=1)))
                if _point_in_poly_evenodd(p, raw_edges):
                    max_sdf = max(max_sdf, d)
        if max_sdf <= 0:
            return None
        z_max = max_sdf * 1.02
        edge_length = z_max / 5
        eps = edge_length / 2

        def sdf(x, y, z):
            p = np.array([x, y])
            pa = p - edge_a
            t = np.einsum('ij,ij->i', pa, ab) / np.where(ab_sq > 0, ab_sq, 1.0)
            t = np.clip(t, 0.0, 1.0)
            d = float(np.min(np.linalg.norm(pa - t[:, None] * ab, axis=1)))
            d2 = d if _point_in_poly_evenodd(p, raw_edges) else -d
            return d2 - z

        bounds = [minx - eps, miny - eps, 0.0, maxx + eps, maxy + eps, z_max + eps]
        body = m3d.Manifold.level_set(sdf, bounds, edge_length)
        if body.is_empty():
            return None
        return body.simplify(edge_length * 0.05)

    def _resolve_minkowski(self, node: ModularCall, ctx: EvalContext) -> dict:
        args, ctx = self._resolve_call_args(node, ctx)
        self._eval_children(node.children, ctx)  # side effect only, see _resolve_transform
        return {}

    def _generate_minkowski(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        bodies = flatten_csg_tree(children)
        bg, fg, hi, so = self._split_by_role(bodies)
        bodies_3d = [c for c in fg if c.body is not None]
        if not bodies_3d:
            return bg + hi + so
        if len(bodies_3d) == 1:
            return bodies_3d + bg + hi + so
        try:
            result = bodies_3d[0].body
            for c in bodies_3d[1:]:
                result = result.minkowski_sum(c.body)
            return [ColoredBody(body=result, color=bodies_3d[0].color)] + bg + hi + so
        except Exception as e:
            self.error(f"minkowski: {e}", node)
            return bg + hi + so

    @staticmethod
    def _copy_body(b: ColoredBody) -> ColoredBody:
        return ColoredBody(body=b.body, color=b.color, section=b.section,
                           flat_preview=b.flat_preview, role=b.role)

    def _eval_children_lazy(self, ctx: EvalContext) -> list[ColoredBody]:
        """Evaluate deferred children nodes with current $-variables injected."""
        if not ctx.children_nodes:
            return []
        caller_ctx = ctx.children_caller_ctx
        if caller_ctx is None:
            return []
        eval_ctx = caller_ctx.child_ctx(
            children_nodes=caller_ctx.children_nodes,
            children_caller_ctx=caller_ctx.children_caller_ctx,
        )
        for k, v in ctx.dyn.items():
            if k.startswith('$'):
                eval_ctx.dyn[k] = v
        for k, v in ctx.let.items():
            if k.startswith('$'):
                eval_ctx.let[k] = v
        return self._eval_children(ctx.children_nodes, eval_ctx)

    def _builtin_children(self, args: dict, ctx: EvalContext) -> list[ColoredBody]:
        idx = self._get_arg(args, 0, "index", None)
        if idx is None:
            return self._eval_children_lazy(ctx)
        # children(N) must index into child STATEMENTS, not output bodies.
        # A filtered statement may produce 0 bodies, shifting all subsequent
        # body-index lookups — so we evaluate only the Nth statement directly.
        idx = int(idx)
        if not ctx.children_nodes:
            return []
        caller_ctx = ctx.children_caller_ctx
        if caller_ctx is None:
            return []
        geo_nodes = [c for c in ctx.children_nodes
                     if not isinstance(c, (Assignment, ModuleDeclaration, FunctionDeclaration))]
        if idx < 0 or idx >= len(geo_nodes):
            return []
        eval_ctx = caller_ctx.child_ctx(
            children_nodes=caller_ctx.children_nodes,
            children_caller_ctx=caller_ctx.children_caller_ctx,
        )
        for k, v in ctx.dyn.items():
            if k.startswith('$'):
                eval_ctx.dyn[k] = v
        for k, v in ctx.let.items():
            if k.startswith('$'):
                eval_ctx.let[k] = v
        return self._eval_children([geo_nodes[idx]], eval_ctx)

    def _builtin_breakpoint(self, args: dict, node, ctx: EvalContext):
        cond = self._get_arg(args, 0, "condition", default=None)
        if cond is not None and not cond:
            return None
        if self._debugging:
            self._check_debug(node, ctx, forced=True)
        return None

    # --- for loops ---

    def _eval_for(self, node: ModularFor, ctx: EvalContext) -> list[ColoredBody]:
        # The parser puts body-level assignments into node.assignments alongside the actual
        # loop variables. Skip any assignment that also appears as a body node — those are
        # per-iteration let-like definitions, not loop variables.
        body_ids = {id(b) for b in node.body}
        _av_pairs: list[tuple] = []
        for assign in node.assignments:
            if id(assign) in body_ids:
                continue
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                values = []
            elif isinstance(values, OscRange):
                values = list(values)
            elif isinstance(values, OscObject):
                values = list(values)  # iterate over keys
            elif isinstance(values, str):
                values = list(values)  # iterate over characters
            elif not isinstance(values, list):
                values = [values]
            _av_pairs.append((assign, name, values))

        result = []
        _debugging = self._debugging

        def _nested(depth: int, parent_ctx: EvalContext) -> None:
            if depth == len(_av_pairs):
                if _debugging and node.body:
                    self._check_debug(node.body[0], parent_ctx, expr_level=True)
                result.extend(self._eval_children(node.body, parent_ctx))
                return
            assign_node, name, values = _av_pairs[depth]
            for val in values:
                child = parent_ctx.child_ctx(children_nodes=ctx.children_nodes,
                                             children_caller_ctx=ctx.children_caller_ctx)
                child.let[name] = val
                if _debugging:
                    self._check_debug(assign_node, child)
                _nested(depth + 1, child)

        _nested(0, ctx)
        return result

    @staticmethod
    def _cartesian(var_seqs: list[tuple[str, list]]):
        if not var_seqs:
            yield []
            return
        names, value_lists = zip(*var_seqs)
        for combo in _product(*value_lists):
            yield list(zip(names, combo))

    def _resolve_intersection_for(self, node: ModularIntersectionFor, ctx: EvalContext) -> dict:
        # group_sizes records, per loop iteration, how many CSGNode children
        # it contributed — same rationale as _resolve_csg's group_sizes
        # (the loop body can itself contain for/if/let, which are
        # transparent in the tree, so one iteration can contribute a
        # variable number of tree children). Combining each iteration's
        # children into one body (_combine, a real Manifold call) is
        # deferred to generate — only the plain-data grouping happens here.
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                return {"group_sizes": []}
            if isinstance(values, OscRange):
                values = list(values)
            elif isinstance(values, OscObject):
                values = list(values)  # iterate over keys
            elif isinstance(values, str):
                values = list(values)  # iterate over characters
            elif not isinstance(values, list):
                values = [values]
            var_seqs.append((name, values))

        body_node = node.body if isinstance(node.body, list) else [node.body]
        _debugging = self._debugging
        group_sizes: list[int] = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx(children_nodes=ctx.children_nodes,
                                     children_caller_ctx=ctx.children_caller_ctx)
            for vname, val in combo:
                loop_ctx.let[vname] = val
            if _debugging and body_node:
                self._check_debug(body_node[0], loop_ctx, expr_level=True)
            before = len(self._tree_stack[-1])
            self._eval_children(body_node, loop_ctx)
            after = len(self._tree_stack[-1])
            group_sizes.append(after - before)
        return {"group_sizes": group_sizes}

    def _generate_intersection_for(self, params: dict, children: list[CSGNode], node: ASTNode) -> list[ColoredBody]:
        idx = 0
        iterations = []
        for size in params["group_sizes"]:
            group_nodes = children[idx:idx + size]
            idx += size
            stmt_bodies = flatten_csg_tree(group_nodes)
            if stmt_bodies:
                iterations.append(self._combine(stmt_bodies))

        if not iterations:
            return []
        # Intersect all iteration results
        bodies_3d = [c for c in iterations if c.body is not None]
        if bodies_3d:
            result = bodies_3d[0].body
            for c in bodies_3d[1:]:
                result = result ^ c.body  # intersection
            return [ColoredBody(body=result, color=bodies_3d[0].color)]
        # 2D intersection
        sections = [c.section for c in iterations if c.section is not None]
        if sections:
            result = sections[0]
            for s in sections[1:]:
                result = result ^ s
            return [ColoredBody(section=result, color=iterations[0].color)]
        return []

    # --- let ---

    def _eval_let_block(self, node: ModularLet, ctx: EvalContext) -> list[ColoredBody]:
        child_ctx = ctx.child_ctx(children_nodes=ctx.children_nodes,
                                 children_caller_ctx=ctx.children_caller_ctx)
        for assign in node.assignments:
            if self._debugging:
                self._check_debug(assign, ctx)
            v = self._eval_expr(assign.expr, ctx)
            child_ctx.let[assign.name.name] = v
        body = getattr(node, 'children', None) or getattr(node, 'body', None) or []
        return self._eval_children(body, child_ctx)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _combine(self, bodies: list[ColoredBody]) -> ColoredBody:
        bodies_3d = [b for b in bodies if b.body is not None]
        if bodies_3d:
            if len(bodies_3d) == 1:
                return bodies_3d[0]
            composed = m3d.Manifold.compose([b.body for b in bodies_3d])
            return ColoredBody(body=composed, color=bodies_3d[0].color)
        # Pure 2D — union all cross sections
        sections = [b.section for b in bodies if b.section is not None]
        if not sections:
            return ColoredBody(body=m3d.Manifold())
        cs = sections[0]
        for s in sections[1:]:
            cs = cs + s
        return ColoredBody(section=cs, color=bodies[0].color)

    def _to_cross_section(self, children: list[ColoredBody]) -> Optional[m3d.CrossSection]:
        """Union all 2D children into a single CrossSection. Returns None if no 2D children."""
        sections = [c.section for c in children if c.section is not None]
        if not sections:
            return None
        cs = sections[0]
        for s in sections[1:]:
            cs = cs + s
        return cs

    # ------------------------------------------------------------------
    # Expression evaluator
    # ------------------------------------------------------------------

    def _eval_expr(self, node, ctx: EvalContext):
        t = type(node)
        if t is NumberLiteral or t is BooleanLiteral or t is StringLiteral:
            return node.val
        if t is Identifier:
            name = node.name
            let = ctx.let
            v = let.get(name)
            if v is not None:
                return v
            if name in let:
                return None
            if name[0] == '$':
                dyn = ctx.dyn
                v = dyn.get(name)
                if v is not None:
                    return v
                if name in dyn:
                    return v
            if name in self._CONSTANTS:
                return self._CONSTANTS[name]
            decl = ctx.scope.lookup_variable(name)
            if decl is None:
                pos = getattr(node, 'position', None)
                self._echo_fn(f"WARNING: Ignoring unknown variable '{name}'{self._loc(pos)}")
                return None
            if type(decl) is ParameterDeclaration:
                return None
            return self._eval_expr(decl.expr, ctx)
        if t is UndefinedLiteral:
            return None
        if t is CommentedExpr:
            return self._eval_expr(node.expr, ctx)
        handler = _EXPR_DISPATCH.get(t)
        if handler is not None:
            return handler(self, node, ctx)
        return None

    # _expr_listcomp and _expr_range removed — dispatch table points directly

    def _expr_add(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        ta, tb = type(a), type(b)
        if (ta is int or ta is float) and (tb is int or tb is float):
            return a + b
        return _vec_add(a, b)

    def _expr_sub(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        ta, tb = type(a), type(b)
        if (ta is int or ta is float) and (tb is int or tb is float):
            return a - b
        return _vec_sub(a, b)

    def _expr_mul(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        ta, tb = type(a), type(b)
        if (ta is int or ta is float) and (tb is int or tb is float):
            return a * b
        if ta is list and tb is list:
            return _matmul(a, b)
        if ta is list and tb in (int, float):
            return [_scale(b, x) for x in a]
        if tb is list and ta in (int, float):
            return [_scale(a, x) for x in b]
        if ta is bool or tb is bool:
            return None
        try:
            return a * b
        except TypeError:
            return None

    def _expr_div(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        ta, tb = type(a), type(b)
        if (ta is int or ta is float) and (tb is int or tb is float):
            if b == 0:
                return float('nan') if a == 0 else math.copysign(float('inf'), a)
            return a / b
        if ta is bool or tb is bool:
            return None
        if ta is list and tb in (int, float):
            return _div_scale(a, b)
        if ta not in (int, float) or tb not in (int, float):
            return None
        if b == 0:
            return float('nan') if a == 0 else math.copysign(float('inf'), a)
        return a / b

    def _expr_mod(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        if type(a) is bool or type(b) is bool:
            return None
        try:
            return a % b
        except (TypeError, ZeroDivisionError):
            return None

    def _expr_exp(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        if type(a) is bool or type(b) is bool:
            return None
        try:
            result = a ** b
            return float('nan') if type(result) is complex else result
        except (TypeError, ZeroDivisionError):
            return None

    def _expr_unary_minus(self, node, ctx):
        v = self._eval_expr(node.expr, ctx)
        if type(v) is list:
            return self._negate_list(v)
        if type(v) is bool:
            return None
        try:
            return -v
        except TypeError:
            return None

    def _expr_and(self, node, ctx):
        return bool(self._eval_expr(node.left, ctx)) and bool(self._eval_expr(node.right, ctx))

    def _expr_or(self, node, ctx):
        return bool(self._eval_expr(node.left, ctx)) or bool(self._eval_expr(node.right, ctx))

    def _expr_not(self, node, ctx):
        return not bool(self._eval_expr(node.expr, ctx))

    def _expr_eq(self, node, ctx):
        return _osc_equal(self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx))

    def _expr_neq(self, node, ctx):
        return not _osc_equal(self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx))

    def _expr_gt(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        if not _osc_comparable(a, b):
            self._echo_fn(f"WARNING: undefined operation ({_osc_type_name(a)} > {_osc_type_name(b)}){self._loc(getattr(node, 'position', None))}")
            return None
        try:
            return a > b
        except TypeError:
            return None

    def _expr_gte(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        if not _osc_comparable(a, b):
            self._echo_fn(f"WARNING: undefined operation ({_osc_type_name(a)} >= {_osc_type_name(b)}){self._loc(getattr(node, 'position', None))}")
            return None
        try:
            return a >= b
        except TypeError:
            return None

    def _expr_lt(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        if not _osc_comparable(a, b):
            self._echo_fn(f"WARNING: undefined operation ({_osc_type_name(a)} < {_osc_type_name(b)}){self._loc(getattr(node, 'position', None))}")
            return None
        try:
            return a < b
        except TypeError:
            return None

    def _expr_lte(self, node, ctx):
        a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
        if not _osc_comparable(a, b):
            self._echo_fn(f"WARNING: undefined operation ({_osc_type_name(a)} <= {_osc_type_name(b)}){self._loc(getattr(node, 'position', None))}")
            return None
        try:
            return a <= b
        except TypeError:
            return None

    def _expr_ternary(self, node, ctx):
        if self._debugging:
            self._check_debug(node, ctx)
        cond = self._eval_expr(node.condition, ctx)
        branch = node.true_expr if cond else node.false_expr
        if self._debugging:
            self._check_debug(branch, ctx, expr_level=True)
        return self._eval_expr(branch, ctx)

    # _expr_call removed — dispatch table points directly to _eval_function_call

    _SWIZZLE = {"x": 0, "y": 1, "z": 2, "w": 3}

    def _expr_index(self, node, ctx):
        obj = self._eval_expr(node.left, ctx)
        idx = self._eval_expr(node.index, ctx)
        tobj, tidx = type(obj), type(idx)
        if tobj is list or tobj is str:
            if tidx is int or tidx is float:
                i = int(idx)
                if i < 0:
                    return None
                try:
                    return obj[i]
                except IndexError:
                    return None
        tobj2 = type(obj)
        if tobj2 is OscRange and (tidx is int or tidx is float):
            return obj[int(idx)]
        if tobj2 is OscObject and tidx is str:
            return obj.get(idx)
        return None

    def _expr_member(self, node, ctx):
        obj = self._eval_expr(node.left, ctx)
        member = getattr(node.member, 'name', None) or str(node.member)
        tobj = type(obj)
        if tobj is list or tobj is tuple:
            idx = self._SWIZZLE.get(member)
            if idx is not None and idx < len(obj):
                return obj[idx]
        if tobj is OscObject:
            return obj.get(member)
        return None

    def _expr_let(self, node, ctx):
        child_ctx = ctx.let_child_ctx()
        for assign in node.assignments:
            if self._debugging:
                self._check_debug(assign, child_ctx)
            v = self._eval_expr(assign.expr, child_ctx)
            child_ctx.let[assign.name.name] = v
        return self._eval_expr(node.body, child_ctx)

    def _expr_echo(self, node, ctx):
        if self._debugging:
            self._check_debug(node, ctx)
        self._do_echo(node.arguments, ctx)
        return self._eval_expr(node.body, ctx)

    def _expr_assert(self, node, ctx):
        if self._debugging:
            self._check_debug(node, ctx)
        raw = node.arguments
        condition = self._eval_expr(raw[0].expr, ctx) if raw else True
        if not condition:
            cond_text = to_openscad([raw[0].expr]).strip() if raw else "false"
            msg = self._eval_expr(raw[1].expr, ctx) if len(raw) > 1 else None
            err = f"Assertion '{cond_text}' failed" + (f': "{msg}"' if msg is not None else "")
            self.error(err, node, innermost_frame="assert")
        return self._eval_expr(node.body, ctx)

    def _expr_function_literal(self, node, ctx):
        return node

    _CONSTANTS = {"PI": math.pi}

    def _eval_identifier(self, node: Identifier, ctx: EvalContext, warn_if_undef: bool = True) -> Any:
        name = node.name
        v = ctx.let.get(name)
        if v is not None:
            return v
        if name in ctx.let:
            return None
        if name[0] == '$':
            v = ctx.dyn.get(name)
            if v is not None:
                return v
            if name in ctx.dyn:
                return v
        if name in self._CONSTANTS:
            return self._CONSTANTS[name]
        decl = ctx.scope.lookup_variable(name)
        if decl is None:
            if warn_if_undef:
                pos = getattr(node, 'position', None)
                self._echo_fn(f"WARNING: Ignoring unknown variable '{name}'{self._loc(pos)}")
            return None
        if type(decl) is ParameterDeclaration:
            return None
        return self._eval_expr(decl.expr, ctx)

    def _eval_list_comp(self, node: ListComprehension, ctx: EvalContext) -> list:
        result = []
        for elem in node.elements:
            te = type(elem)
            if te is ListCompFor:
                result.extend(self._eval_listcomp_for(elem, ctx))
            elif te is ListCompCFor:
                result.extend(self._eval_listcomp_cfor(elem, ctx))
            elif te is ListCompIf:
                if self._debugging:
                    self._check_debug(elem, ctx)
                if self._eval_expr(elem.condition, ctx):
                    self._expr_depth += 1
                    if self._debugging:
                        self._check_debug(elem.true_expr, ctx, expr_level=True)
                    result.extend(self._eval_list_comp_body(elem.true_expr, ctx))
                    self._expr_depth -= 1
            elif te is ListCompIfElse:
                if self._debugging:
                    self._check_debug(elem, ctx)
                branch = elem.true_expr if self._eval_expr(elem.condition, ctx) else elem.false_expr
                self._expr_depth += 1
                if self._debugging:
                    self._check_debug(branch, ctx, expr_level=True)
                result.extend(self._eval_list_comp_body(branch, ctx))
                self._expr_depth -= 1
            elif te is ListCompLet:
                let_ctx = ctx.let_child_ctx()
                for assign in elem.assignments:
                    if self._debugging:
                        self._check_debug(assign, let_ctx)
                    let_ctx.let[assign.name.name] = self._eval_expr(assign.expr, let_ctx)
                result.extend(self._eval_list_comp_body(elem.body, let_ctx))
            elif te is ListCompEach:
                self._expr_depth += 1
                if self._debugging:
                    self._check_debug(elem, ctx, expr_level=True)
                inner = elem.body
                ti = type(inner)
                if ti is ListCompIf or ti is ListCompIfElse or ti is ListCompFor or ti is ListCompCFor or ti is ListCompLet or ti is ListCompEach:
                    for item in self._eval_list_comp_body(inner, ctx):
                        if type(item) is list:
                            result.extend(item)
                        elif item is not None:
                            result.append(item)
                else:
                    v = self._eval_expr(inner, ctx)
                    if type(v) is list:
                        result.extend(v)
                    elif v is not None:
                        result.append(v)
                self._expr_depth -= 1
            else:
                if self._debugging:
                    self._check_debug(elem, ctx, expr_level=True)
                result.append(self._eval_expr(elem, ctx))
        return result

    def _eval_list_comp_body(self, body, ctx: EvalContext) -> list:
        t = type(body)
        if t is ListComprehension:
            self._expr_depth += 1
            result = [self._eval_list_comp(body, ctx)]
            self._expr_depth -= 1
            return result
        if t is ListCompFor:
            return self._eval_listcomp_for(body, ctx)
        if t is ListCompCFor:
            return self._eval_listcomp_cfor(body, ctx)
        if t is ListCompLet:
            let_ctx = ctx.let_child_ctx()
            for assign in body.assignments:
                if self._debugging:
                    self._check_debug(assign, let_ctx)
                let_ctx.let[assign.name.name] = self._eval_expr(assign.expr, let_ctx)
            return self._eval_list_comp_body(body.body, let_ctx)
        if t is ListCompIf:
            if self._debugging:
                self._check_debug(body, ctx)
            if self._eval_expr(body.condition, ctx):
                self._expr_depth += 1
                if self._debugging:
                    self._check_debug(body.true_expr, ctx, expr_level=True)
                result = self._eval_list_comp_body(body.true_expr, ctx)
                self._expr_depth -= 1
                return result
            return []
        if t is ListCompIfElse:
            if self._debugging:
                self._check_debug(body, ctx)
            branch = body.true_expr if self._eval_expr(body.condition, ctx) else body.false_expr
            self._expr_depth += 1
            if self._debugging:
                self._check_debug(branch, ctx, expr_level=True)
            result = self._eval_list_comp_body(branch, ctx)
            self._expr_depth -= 1
            return result
        if t is ListCompEach:
            self._expr_depth += 1
            if self._debugging:
                self._check_debug(body, ctx, expr_level=True)
            inner = body.body
            ti = type(inner)
            if ti is ListCompIf or ti is ListCompIfElse or ti is ListCompFor or ti is ListCompCFor or ti is ListCompLet or ti is ListCompEach:
                result = []
                for item in self._eval_list_comp_body(inner, ctx):
                    if type(item) is list:
                        result.extend(item)
                    elif item is not None:
                        result.append(item)
                self._expr_depth -= 1
                return result
            v = self._eval_expr(inner, ctx)
            self._expr_depth -= 1
            if type(v) is list:
                return v
            return [v] if v is not None else []
        if self._debugging:
            self._check_debug(body, ctx, expr_level=True)
        v = self._eval_expr(body, ctx)
        return [v]

    def _eval_listcomp_for(self, node: ListCompFor, ctx: EvalContext) -> list:
        _av_pairs: list[tuple] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                values = []
            elif type(values) is list:
                pass
            elif type(values) is OscRange:
                values = list(values)
            elif type(values) is OscObject:
                values = list(values)
            elif type(values) is str:
                values = list(values)  # iterate over characters
            else:
                values = [values]
            _av_pairs.append((assign, name, values))

        result = []
        _debugging = self._debugging
        is_lc = type(node.body) is ListComprehension

        def _nested(depth: int, parent_ctx: EvalContext) -> None:
            if depth == len(_av_pairs):
                self._expr_depth += 1
                if is_lc:
                    result.append(self._eval_list_comp(node.body, parent_ctx))
                else:
                    result.extend(self._eval_list_comp_body(node.body, parent_ctx))
                self._expr_depth -= 1
                return
            assign_node, name, values = _av_pairs[depth]
            for val in values:
                child = parent_ctx.let_child_ctx()
                child.let[name] = val
                if _debugging:
                    self._check_debug(assign_node, child)
                _nested(depth + 1, child)

        _nested(0, ctx)
        return result

    _MAX_CFOR_ITERATIONS = 1_000_000

    def _eval_listcomp_cfor(self, node: ListCompCFor, ctx: EvalContext) -> list:
        loop_ctx = ctx.let_child_ctx()
        _debugging = self._debugging
        for assign in node.inits:
            if _debugging:
                self._check_debug(assign, loop_ctx)
            loop_ctx.let[assign.name.name] = self._eval_expr(assign.expr, loop_ctx)

        result = []
        iterations = 0
        is_lc = type(node.body) is ListComprehension
        while True:
            if _debugging:
                self._check_debug(node.condition, loop_ctx, expr_level=True)
            if not self._eval_expr(node.condition, loop_ctx):
                break
            iterations += 1
            if iterations > self._MAX_CFOR_ITERATIONS:
                self.error("C-style for loop exceeded maximum iteration count", node)
            self._expr_depth += 1
            if _debugging:
                self._check_debug(node, loop_ctx)
            if is_lc:
                result.append(self._eval_list_comp(node.body, loop_ctx))
            else:
                result.extend(self._eval_list_comp_body(node.body, loop_ctx))
            self._expr_depth -= 1
            for assign in node.incrs:
                if _debugging:
                    self._check_debug(assign, loop_ctx)
                loop_ctx.let[assign.name.name] = self._eval_expr(assign.expr, loop_ctx)
        return result

    def _eval_range(self, node: RangeLiteral, ctx: EvalContext) -> OscRange:
        start = self._eval_expr(node.start, ctx)

        stop = self._eval_expr(node.end, ctx)
        increment = self._eval_expr(node.step, ctx)

        start = float(start) if start is not None else 0.0
        stop = float(stop) if stop is not None else 0.0
        increment = float(increment) if increment is not None else 1.0
        return OscRange(start, increment, stop)

    def _eval_function_call(self, node: PrimaryCall, ctx: EvalContext) -> Any:
        left = node.left
        name = left.name if type(left) is Identifier else None

        if name:
            if name == "import":
                args = self._resolve_args(node.arguments, ctx)
                return self._import_as_value(args, node)
            if name not in self._BUILTIN_FN_NAMES:
                decl = ctx.scope.lookup_function(name)
                if decl is not None:
                    if self._debugging:
                        self._check_debug(node, ctx)
                    return self._eval_user_function(name, decl, node.arguments, ctx, node)
            else:
                args = self._resolve_args(node.arguments, ctx)
                if name == "object":
                    return self._builtin_object(args, node)
                if name == "textmetrics":
                    return self._builtin_textmetrics(args, node)
                if name == "fontmetrics":
                    return self._builtin_fontmetrics(args, node)
                fn = self._math_fns.get(name)
                if fn is not None:
                    positional = [args[i] for i in range(len(args)) if i in args]
                    if not positional:
                        positional = [args[k] for k in args if type(k) is str]
                    try:
                        return fn(*positional)
                    except Exception:
                        return None

        if type(left) is Identifier:
            func_node = self._eval_identifier(left, ctx, warn_if_undef=False)
        else:
            func_node = self._eval_expr(left, ctx)
        if type(func_node) is FunctionLiteral:
            if self._debugging:
                self._check_debug(node, ctx)
            return self._eval_function_literal(func_node, node.arguments, ctx, node, name=name)

        if name and func_node is None:
            pos = getattr(node, 'position', None)
            self._echo_fn(f"WARNING: Ignoring unknown function '{name}'{self._loc(pos)}")

        return None

    def _builtin_minmax(self, op, args):
        """Shared logic for OpenSCAD's `min`/`max`.

        A single vector argument returns `op` of its elements; multiple
        arguments must all be scalars (mixing in a vector is `undef`, like
        real OpenSCAD); a single scalar argument returns itself.
        """
        if len(args) == 1:
            v = args[0]
            return op(v) if isinstance(v, list) else v
        if any(isinstance(a, list) for a in args):
            return None
        return op(args)

    def _builtin_max(self, *args):
        return self._builtin_minmax(max, args)

    def _builtin_min(self, *args):
        return self._builtin_minmax(min, args)

    def _builtin_pow(self, a, b):
        if a < 0 and not float(b).is_integer():
            return float('nan')
        if a == 0 and b < 0:
            # 0 ** negative is +inf in OpenSCAD; Python's pow()/math.pow() raise.
            return float('inf')
        return pow(a, b)

    # At exact multiples of 90 degrees, sin/cos/tan use exact table values
    # instead of math.sin/cos/tan(radians(x)), which accumulate floating-point
    # noise (e.g. cos(90) -> 6.12e-17, tan(90) -> 1.63e+16) — matching real
    # OpenSCAD's degree-based trig, which special-cases these angles.
    _SIN_90 = (0.0, 1.0, 0.0, -1.0)
    _COS_90 = (1.0, 0.0, -1.0, 0.0)
    _TAN_90 = (0.0, math.inf, 0.0, -math.inf)

    def _deg_trig(self, x, table, fallback):
        if math.isnan(x) or math.isinf(x):
            return float('nan')
        n = x / 90.0
        rn = round(n)
        if rn == n:
            return table[int(rn) % 4]
        return fallback(math.radians(x))

    def _negate_list(self, v):
        if _is_flat_numeric(v):
            if len(v) >= _NP_VEC_THRESHOLD:
                return (-np.asarray(v)).tolist()
            return [-x for x in v]
        result = []
        for x in v:
            if isinstance(x, list):
                result.append(self._negate_list(x))
            elif isinstance(x, bool) or x is None:
                result.append(None)
            else:
                try:
                    result.append(-x)
                except TypeError:
                    result.append(None)
        return result

    def _builtin_sin(self, x):
        return self._deg_trig(x, self._SIN_90, math.sin)

    def _builtin_cos(self, x):
        return self._deg_trig(x, self._COS_90, math.cos)

    def _builtin_tan(self, x):
        return self._deg_trig(x, self._TAN_90, math.tan)

    def _builtin_cross(self, a, b):
        # Real OpenSCAD validates every component up front and returns
        # undef (with a WARNING naming the offending value) rather than
        # computing through: nan/inf components would otherwise propagate
        # unevenly (inf*0 is nan, not 0), producing a mixed
        # finite/nan/inf result instead of a clean undef -- confirmed
        # against real OpenSCAD 2022.08.22.
        for v in (a, b):
            for c in v:
                if isinstance(c, float) and not math.isfinite(c):
                    return None
        if len(a) == 2 and len(b) == 2:
            return a[0]*b[1] - a[1]*b[0]
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]

    def _builtin_chr(self, x):
        # Real OpenSCAD silently skips non-finite/non-numeric code points
        # (in a list) and returns "" (rather than undef) for a scalar
        # non-finite/undef argument -- confirmed against real OpenSCAD
        # 2022.08.22 (chr(1/0), chr(0/0), chr(undef) all -> "";
        # chr([65, 1/0, 66]) -> "AB", skipping the invalid element).
        def valid(c):
            return isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
        if isinstance(x, list):
            return "".join(chr(int(c)) for c in x if valid(c))
        return chr(int(x)) if valid(x) else ""

    def _builtin_rands(self, minval, maxval, n, seed=None):
        self._rands_call_count += 1
        if seed is not None:
            random.seed(int(seed))
        return [random.uniform(float(minval), float(maxval)) for _ in range(int(n))]

    def _builtin_search(self, match, vector, num_returns=1, index_col=0):
        """OpenSCAD search(): find positions of match value(s) in vector.

        Strings are treated as character arrays — each character is searched
        independently, mirroring OpenSCAD semantics.
        """
        num_returns = int(num_returns)
        col = int(index_col)

        def _find_all(val):
            results = []
            for i, item in enumerate(vector):
                # A vector match value (e.g. searching for a coordinate like
                # [0,0,1]) is compared directly against each whole element,
                # not column-indexed — index_col only applies to scalar matches.
                if isinstance(val, list):
                    target = item
                else:
                    target = item[col] if isinstance(item, list) else item
                if target == val:
                    results.append(i)
            return results

        def _result_for(val):
            """Result for one element in a list/string match context."""
            matches = _find_all(val)
            if num_returns == 1:
                return matches[0] if matches else []
            elif num_returns == 0:
                return matches
            else:
                return matches[:num_returns]

        if isinstance(match, str):
            # String → character array: search for each char independently.
            # With num_returns=1: not-found chars are dropped (not included as []).
            # With num_returns=0: all chars included, not-found → [].
            results = []
            for c in match:
                r = _result_for(c)
                if num_returns != 1 or r != []:
                    results.append(r)
            return results
        elif isinstance(match, list):
            return [_result_for(m) for m in match]
        else:
            # Scalar number: always return a list of matching indices
            matches = _find_all(match)
            if num_returns == 1:
                return matches[:1]      # [idx] or []
            elif num_returns == 0:
                return matches
            else:
                return matches[:num_returns]

    def _builtin_parent_module(self, idx=0):
        """Return the name of the module idx levels up from the current module."""
        modules = [e[1] for e in self._call_stack if e[0] == "module"]
        rev_idx = len(modules) - 1 - int(idx)
        return modules[rev_idx] if 0 <= rev_idx < len(modules) else None

    def _builtin_lookup(self, key, table):
        """Linear interpolation lookup in a [[key, value], ...] table."""
        if not table:
            return None
        pairs = sorted(table, key=lambda p: p[0])
        if key <= pairs[0][0]:
            return pairs[0][1]
        if key >= pairs[-1][0]:
            return pairs[-1][1]
        for i in range(len(pairs) - 1):
            k0, v0 = pairs[i]
            k1, v1 = pairs[i + 1]
            if k0 <= key <= k1:
                t = (key - k0) / (k1 - k0)
                return v0 + t * (v1 - v0)
        return 0

    def _builtin_object(self, args: dict, node) -> Optional[OscObject]:
        """`object(a=1, b=2, ...)` — an ordered string-keyed map.

        Positional arguments merge an existing `OscObject`'s entries, or a
        list of `[key, value]` pairs, into the result (in their own order);
        named arguments set/override entries in call order. Any other
        positional argument type is invalid and the whole call is `undef`.
        """
        result: dict = {}
        for key, val in args.items():
            if isinstance(key, str):
                result[key] = val
                continue
            if isinstance(val, OscObject):
                for k, v in val.items():
                    result[k] = v
            elif isinstance(val, list):
                for entry in val:
                    if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str):
                        result[entry[0]] = entry[1]
                    else:
                        self._echo_fn(
                            f"WARNING: object(Argument {key}) malformed [key,value] entry in "
                            f"unnamed list argument{self._loc(getattr(node, 'position', None))}"
                        )
                        return None
            else:
                tname = _object_arg_type_name(val)
                self._echo_fn(
                    f"WARNING: object(Argument {key} <{tname}>) An unnamed argument must be "
                    f"either <object> or <list>, it is <{tname}>. "
                    f"{self._loc(getattr(node, 'position', None))}"
                )
                return None
        return OscObject(result)

    def _builtin_textmetrics(self, args: dict, node) -> OscObject:
        """`textmetrics(text=.., size=.., halign=.., valign=.., spacing=.., font=..)`.

        Measures `text` against the font resolved by `font=` (an OpenSCAD/
        fontconfig pattern, e.g. `"Times New Roman:style=Bold"`, via
        `_resolve_font()` — same resolution `text()` uses) and returns an
        `OscObject` with `position`, `size`, `ascent`, `descent`, `offset`,
        `advance` — matching real OpenSCAD's key order. Falls back to the
        bundled Liberation Sans if `font=` is unset, `fc-match` is
        unavailable, or the font can't be found. `direction`/`language`/
        `script` are accepted but unused; see docs/evaluator.md for known gaps.
        """
        text = self._get_arg(args, 0, "text", "")
        size = self._get_arg(args, 1, "size", 10)
        halign = self._get_arg(args, None, "halign", "left")
        valign = self._get_arg(args, None, "valign", "baseline")
        spacing = self._get_arg(args, None, "spacing", 1)
        font_spec = self._get_arg(args, None, "font", "") or ""

        font = _resolve_font(str(font_spec))
        m = _measure_text(text, size, spacing, font)
        ascent, descent = m["ascent"], m["descent"]
        advance_x = m["advance_x"]

        offset_x, offset_y = _text_align_offset(halign, valign, m)

        position = [offset_x + m["ink_min_x"], offset_y + descent]
        size_vec = [m["ink_max_x"] - m["ink_min_x"], ascent - descent]

        return OscObject({
            "position": position,
            "size": size_vec,
            "ascent": ascent,
            "descent": descent,
            "offset": [offset_x, offset_y],
            "advance": [advance_x, 0.0],
        })

    def _builtin_fontmetrics(self, args: dict, node) -> OscObject:
        """`fontmetrics(size=.., font=..)` — global metrics of the font
        resolved by `font=` (via `_resolve_font()`, same resolution `text()`
        and `textmetrics()` use), scaled for `size`. Returns a nested
        `OscObject` with `nominal`/`max`/`interline`/`font`; `font.family`/
        `font.style` report the *actually resolved* font's real name (read
        from its `name` table via `getBestFamilyName()`/`getBestSubFamilyName()`),
        not just an echo of the request — e.g. `font="Times New Roman:style=Bold"`
        yields `family="Times New Roman"`, `style="Bold"`. Falls back to the
        bundled Liberation Sans if `font=` is unset, `fc-match` is
        unavailable, or the font can't be found."""
        size = self._get_arg(args, 0, "size", 10)
        font_spec = self._get_arg(args, None, "font", "") or ""

        font = _resolve_font(str(font_spec))
        head, hhea = font["head"], font["hhea"]
        scale = size * (100 / 72) / font["units_per_em"]

        return OscObject({
            "nominal": OscObject({
                "ascent": hhea.ascent * scale,
                "descent": hhea.descent * scale,
            }),
            "max": OscObject({
                "ascent": head.yMax * scale,
                "descent": head.yMin * scale,
            }),
            "interline": (hhea.ascent - hhea.descent + hhea.lineGap) * scale,
            "font": OscObject({
                "family": font["family_name"],
                "style": font["style_name"],
            }),
        })

    def _apply_defaults(self, params, child_ctx: EvalContext, caller_ctx: EvalContext):
        let_dict = child_ctx.let
        _eval = self._eval_expr
        for param in params:
            pname = param.name.name
            if pname not in let_dict:
                default = param.default
                let_dict[pname] = _eval(default, caller_ctx) if default is not None else None

    def _eval_user_function(self, name: str, decl: FunctionDeclaration, arguments, ctx: EvalContext, call_node=None) -> Any:
        params = decl.parameters or []
        bound = self._bind_args(params, arguments, ctx)
        fn_scope = decl.scope or ctx.scope
        child_ctx = self._call_ctx_for(decl, ctx, scope=fn_scope)
        for k, v in bound.items():
            if k[0] == '$':
                child_ctx.dyn[k] = v
            else:
                child_ctx.let[k] = v
        self._apply_defaults(params, child_ctx, ctx)
        pos = call_node.position if call_node is not None else None
        self._call_stack.append(("function", name, pos, decl.position))
        self._frame_ctxs.append(child_ctx)
        try:
            if self._debugging:
                self._check_debug(decl.expr, child_ctx)
            result = self._eval_expr(decl.expr, child_ctx)
            if self._return_hook is not None:
                self._return_hook(name, result, len(self._call_stack))
            return result
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()

    def _eval_function_literal(self, func_node: FunctionLiteral, arguments, ctx: EvalContext, call_node=None, name: str | None = None) -> Any:
        params = func_node.parameters
        bound = self._bind_args(params, arguments, ctx)
        fn_scope = func_node.scope or ctx.scope
        child_ctx = self._call_ctx_for(func_node, ctx, scope=fn_scope)
        for k, v in bound.items():
            if k[0] == '$':
                child_ctx.dyn[k] = v
            else:
                child_ctx.let[k] = v
        self._apply_defaults(params, child_ctx, ctx)
        pos = call_node.position if call_node is not None else None
        fn_name = name or "<function>"
        self._call_stack.append(("function", fn_name, pos, func_node.position))
        self._frame_ctxs.append(child_ctx)
        try:
            if self._debugging:
                self._check_debug(func_node.body, child_ctx)
            result = self._eval_expr(func_node.body, child_ctx)
            if self._return_hook is not None:
                self._return_hook(fn_name, result, len(self._call_stack))
            return result
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()


_EXPR_DISPATCH: dict[type, callable] = {
    ListComprehension: Evaluator._eval_list_comp,
    RangeLiteral: Evaluator._eval_range,
    AdditionOp: Evaluator._expr_add,
    SubtractionOp: Evaluator._expr_sub,
    MultiplicationOp: Evaluator._expr_mul,
    DivisionOp: Evaluator._expr_div,
    ModuloOp: Evaluator._expr_mod,
    ExponentOp: Evaluator._expr_exp,
    UnaryMinusOp: Evaluator._expr_unary_minus,
    LogicalAndOp: Evaluator._expr_and,
    LogicalOrOp: Evaluator._expr_or,
    LogicalNotOp: Evaluator._expr_not,
    EqualityOp: Evaluator._expr_eq,
    InequalityOp: Evaluator._expr_neq,
    GreaterThanOp: Evaluator._expr_gt,
    GreaterThanOrEqualOp: Evaluator._expr_gte,
    LessThanOp: Evaluator._expr_lt,
    LessThanOrEqualOp: Evaluator._expr_lte,
    TernaryOp: Evaluator._expr_ternary,
    PrimaryCall: Evaluator._eval_function_call,
    PrimaryIndex: Evaluator._expr_index,
    PrimaryMember: Evaluator._expr_member,
    LetOp: Evaluator._expr_let,
    EchoOp: Evaluator._expr_echo,
    AssertOp: Evaluator._expr_assert,
    FunctionLiteral: Evaluator._expr_function_literal,
}
