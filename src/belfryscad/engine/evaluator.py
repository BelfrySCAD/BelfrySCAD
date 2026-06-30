"""
AST evaluator: walks the openscad_lalr_parser AST and produces Manifold geometry.
Returns (manifold_body, id_to_node, colored_meshes) or raises EvalError.
"""
from __future__ import annotations
import math
import random
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

    Returns `(heights, adjacency, p0_keys, hole_keys_list, key_fn)` or None.
      heights         : position-key → offset-distance (0 on boundary)
      adjacency       : position-key → [neighbour keys]  (undirected)
      p0_keys         : keys for p0 vertices in traversal order
      hole_keys_list  : list of key lists, one list per hole in order
      key_fn          : snap function `(x, y) → position-key`
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
        subtrees = skeletonize(outer_pts, holes_pts if holes_pts else None)
        if not subtrees:
            return None

        for st in subtrees:
            s = key(st.source.x, st.source.y)
            heights[s] = st.height
            adjacency.setdefault(s, [])
            for sink in st.sinks:
                t = key(sink.x, sink.y)
                heights.setdefault(t, 0.0)
                adjacency.setdefault(t, [])
                add_edge(s, t)

        return heights, adjacency, p0_keys, hole_keys_list, key
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
    coplanar). The projection basis derived from the first 3 points (`u`
    along the first edge, `v = normal x u`) makes `_ear_clip`'s output map
    directly to outward-facing 3D triangles, with no winding reversal.

    Returns `None` if the face is degenerate (fewer than 3 points, near-zero
    normal), not planar within tolerance, or ear-clipping fails.
    """
    n = len(face_pts3d)
    if n < 3:
        return None
    p0, p1, p2 = face_pts3d[0], face_pts3d[1], face_pts3d[2]
    normal = np.cross(p1 - p0, p2 - p0)
    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return None
    normal = normal / norm_len
    u_axis = p1 - p0
    u_axis = u_axis / np.linalg.norm(u_axis)
    v_axis = np.cross(normal, u_axis)

    span = max(float(np.linalg.norm(face_pts3d.max(axis=0) - face_pts3d.min(axis=0))), 1e-9)
    tol = span * 1e-4
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


def _skeleton_roof_component(
    outer_arr: np.ndarray,
    hole_arrs: list[np.ndarray],
) -> Optional[m3d.Manifold]:
    """Build a straight-skeleton roof for one connected component: a CCW outer
    polygon and zero or more CW hole polygons. Returns a closed Manifold or None.

    Floor faces: tessellated via shapely Delaunay (centroid-filtered for holes).
    Roof faces: traced from each boundary edge in its natural direction using
    `_trace_face`. For CCW outer edges the left side is the interior (roofable).
    For CW hole edges the left side is also the exterior of the hole (roofable),
    so natural direction works for both — no reversal needed.
    """
    try:
        from shapely.geometry import Polygon as _SPoly
        from shapely.ops import triangulate as _stri

        p0 = _ccw_polygon(outer_arr)
        n0 = len(p0)
        if n0 < 3:
            return None

        graph = _build_skeleton_graph_with_holes(p0, hole_arrs)
        if graph is None:
            return None
        heights, adjacency, p0_keys, hole_keys_list, key = graph
        if len(set(p0_keys)) != n0:
            return None

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
            # Holes: shapely Delaunay + centroid filter to exclude hole regions.
            # Shapely's triangle winding is inconsistent, so we check each triangle's
            # signed area and reverse if necessary to ensure a downward (-z) normal.
            outer_2d = [(float(p[0]), float(p[1])) for p in p0]
            holes_2d = [[(float(p[0]), float(p[1])) for p in h] for h in hole_arrs]
            shape = _SPoly(outer_2d, holes_2d)
            for tri in _stri(shape):
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
        for i in range(n0):
            face = _trace_face(adjacency, p0_keys[i], p0_keys[(i + 1) % n0])
            if face is None or len(face) < 3:
                return None
            face_pts3d = np.array([(p[0], p[1], heights[p]) for p in face])
            face_tris = _triangulate_planar_face(face_pts3d)
            if face_tris is None:
                return None
            face_idx = [vert_index(p) for p in face]
            for (a, b, c) in face_tris:
                tris.append((face_idx[a], face_idx[b], face_idx[c]))

        # --- Roof faces for hole boundary edges ---
        # Holes are CW-in-math; the LEFT of each natural CW directed edge is
        # the exterior (roofable) region, so trace in the natural direction.
        for hkeys in hole_keys_list:
            nh = len(hkeys)
            if len(set(hkeys)) != nh:
                return None
            for i in range(nh):
                face = _trace_face(adjacency, hkeys[i], hkeys[(i + 1) % nh])
                if face is None or len(face) < 3:
                    return None
                face_pts3d = np.array([(p[0], p[1], heights[p]) for p in face])
                face_tris = _triangulate_planar_face(face_pts3d)
                if face_tris is None:
                    return None
                face_idx = [vert_index(p) for p in face]
                for (a, b, c) in face_tris:
                    tris.append((face_idx[a], face_idx[b], face_idx[c]))

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
            if b is not None:
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
_default_font_cache: Optional[dict] = None


def _load_default_font() -> dict:
    """Lazily load the bundled Liberation Sans font and cache its tables.

    `textmetrics()`/`fontmetrics()` always measure against this single
    bundled font regardless of the requested `font=` — see docs/evaluator.md
    for the rationale (real OpenSCAD resolves `font=` via fontconfig/CoreText,
    which is out of scope here).
    """
    global _default_font_cache
    if _default_font_cache is None:
        font = TTFont(str(_FONT_PATH))
        _default_font_cache = {
            "cmap": font.getBestCmap(),
            "hmtx": font["hmtx"],
            "glyf": font["glyf"],
            "units_per_em": font["head"].unitsPerEm,
            "head": font["head"],
            "hhea": font["hhea"],
            "glyph_set": font.getGlyphSet(),
        }
    return _default_font_cache


def _measure_text(text: str, size: float, spacing: float) -> dict:
    """Lay out `text` left-to-right and return its ink-bbox/advance metrics
    in OpenSCAD units, scaled for `size` (see docs/evaluator.md for the
    scale-factor and per-glyph layout derivation).

    Returns a dict with `ascent`, `descent`, `ink_min_x`, `ink_max_x`,
    `advance_x`, and `glyphs` (a list of `(glyph_name, pen_x_scaled)` for
    each renderable glyph, used by `text()`) — aggregates are all `0` and
    `glyphs` is empty if `text` contains no measurable glyphs.
    """
    font = _load_default_font()
    cmap, hmtx, glyf = font["cmap"], font["hmtx"], font["glyf"]
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
        glyph = glyf[gname]
        if glyph.numberOfContours != 0:
            left = pen_x * scale + glyph.xMin * scale
            right = pen_x * scale + glyph.xMax * scale
            top = glyph.yMax * scale
            bottom = glyph.yMin * scale
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
    """A `BasePen` that flattens glyph outlines (including quadratic Bezier
    curves) into polygon contours, for building a `m3d.CrossSection`."""

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
_glyph_contour_cache: dict[tuple[str, int], list[np.ndarray]] = {}


def _glyph_cross_section(gname: str, segs: int) -> m3d.CrossSection:
    """Return the (unscaled, font-units) `m3d.CrossSection` for glyph `gname`,
    flattening curves into `segs` segments. Contours cached per `(gname, segs)`."""
    key = (gname, segs)
    contours = _glyph_contour_cache.get(key)
    if contours is None:
        glyph_set = _load_default_font()["glyph_set"]
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


_DEFAULT_DOLLAR = {"$fn": 0, "$fa": 12.0, "$fs": 2.0, "$t": 0.0, "$parent_modules": 0}


class EvalContext:
    """Mutable evaluation state threaded through recursive calls."""
    __slots__ = ('scope', 'dyn', 'let', 'dyn_positions', 'color',
                 'children_nodes', 'children_caller_ctx')

    def __init__(self, scope, dyn=None, let=None, dyn_positions=None, color=None,
                 children_nodes=None, children_caller_ctx=None):
        self.scope = scope
        self.dyn = dyn if dyn is not None else dict(_DEFAULT_DOLLAR)
        self.let = let if let is not None else {}
        self.dyn_positions = dyn_positions if dyn_positions is not None else {}
        self.color = color
        self.children_nodes = children_nodes if children_nodes is not None else []
        self.children_caller_ctx = children_caller_ctx

    def child_ctx(self, scope=None, dyn=None, let=None, color=None,
                  children_nodes=None, children_caller_ctx=None):
        return EvalContext(
            scope=scope if scope is not None else self.scope,
            dyn=dyn if dyn is not None else dict(self.dyn),
            let=let if let is not None else dict(self.let),
            dyn_positions={} if dyn is None else self.dyn_positions,
            color=color if color is not None else self.color,
            children_nodes=children_nodes if children_nodes is not None else [],
            children_caller_ctx=children_caller_ctx,
        )

    def let_child_ctx(self):
        ctx = EvalContext.__new__(EvalContext)
        ctx.scope = self.scope
        ctx.dyn = self.dyn
        ctx.let = dict(self.let)
        ctx.dyn_positions = self.dyn_positions
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
            color=color if color is not None else self.color,
            children_nodes=children_nodes if children_nodes is not None else [],
            children_caller_ctx=children_caller_ctx,
        )


class Evaluator:
    def __init__(self, echo_fn=None, debug_hook=None, error_break_fn=None, return_hook=None):
        self.id_to_node: dict[int, ASTNode] = {}
        self._errors: list[str] = []
        self._echo_fn = echo_fn or (lambda msg: print(msg))
        self._call_stack: list = []
        self._frame_ctxs: list = []
        self._debug_hook = debug_hook
        self._debugging = debug_hook is not None
        self._error_break_fn = error_break_fn
        self._return_hook = return_hook
        self._last_locals: dict = {}
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
            "chr": lambda x: "".join(chr(int(c)) for c in x) if isinstance(x, list) else chr(int(x)),
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

        current_frame = {"local_scope": local_scope, "outer_scope": outer_scope, "dyn_names": dyn_names}
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
            all_frame_locals.append({"local_scope": p_local, "outer_scope": {}, "dyn_names": p_dyn})

        if self._call_stack:
            toplevel_frame = {
                "local_scope": dict(outer_scope),
                "outer_scope": {},
                "dyn_names": set(),
            }
            all_frame_locals.append(toplevel_frame)

        self._last_locals = {n: v for n, v in local_scope.items() if n in dyn_names}
        self._last_all_frame_locals = all_frame_locals
        return self._last_locals, all_frame_locals

    def _check_debug(self, node: ASTNode, ctx: EvalContext, forced: bool = False, expr_level: bool = False):
        if self._debug_hook is None:
            return
        pos = getattr(node, 'position', None)
        line = getattr(pos, 'line', None) if pos else None
        if line is None:
            return
        origin = getattr(pos, 'origin', None)

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
        """Walk top-level AST nodes and return (geometry, id_to_node mapping)."""
        self._resolve_use_statements(nodes, root_scope)
        self._call_stack.clear()
        self._frame_ctxs.clear()
        ctx = EvalContext(scope=root_scope)
        if viewport_params:
            ctx.dyn.update(viewport_params)
        self._root_ctx = ctx
        result = []
        # OpenSCAD executes all assignments before geometry in each scope.
        assignments = [n for n in nodes if isinstance(n, Assignment)]
        others = [n for n in nodes if not isinstance(n, Assignment)]
        for node in assignments + others:
            bodies = self._eval_statement(node, ctx)
            result.extend(bodies)
        # ! (show_only) modifier: if any body is show_only, display only those + highlights
        show_only = [b for b in result if b.role == "show_only"]
        if show_only:
            result = [b for b in result if b.role in ("show_only", "highlight")]
        return result, self.id_to_node

    # ------------------------------------------------------------------
    # Statement dispatch
    # ------------------------------------------------------------------

    def _eval_statement(self, node: ASTNode, ctx: EvalContext) -> list[ColoredBody]:
        self._last_ctx = ctx
        t = type(node)
        if t is not ModuleDeclaration and t is not FunctionDeclaration and t is not ModularLet:
            if self._debugging:
                self._check_debug(node, ctx)
        if t is Assignment:
            name = node.name.name
            if name[0] == '$':
                ctx.dyn[name] = self._eval_expr(node.expr, ctx)
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
        if t is ModularCall:
            return self._eval_modular_call(node, ctx)
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
        if t is ModularIntersectionFor:
            return self._eval_intersection_for(node, ctx)
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
        if isinstance(node, ModularModifierHighlight):  # # — real geometry + highlight overlay
            return [replace(b, role="highlight") for b in self._eval_statement(node.child, ctx)]
        if isinstance(node, ModularModifierBackground):  # % — ghost display only, excluded from CSG
            return [replace(b, role="background") for b in self._eval_statement(node.child, ctx)]
        if isinstance(node, ModularModifierShowOnly):  # ! — show only this subtree
            return [replace(b, role="show_only") for b in self._eval_statement(node.child, ctx)]
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

    def _eval_builtin(self, name: str, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        args = self._resolve_args(node.arguments, ctx)
        # $-prefixed named args (e.g. $fn=32) override the dynamic context for this call
        dyn_overrides = {k: v for k, v in args.items() if isinstance(k, str) and k.startswith("$")}
        if dyn_overrides:
            ctx = ctx.child_ctx(dyn={**ctx.dyn, **dyn_overrides})

        if name == "cube":
            return self._body_list(self._builtin_cube(args, node, ctx))
        if name == "sphere":
            return self._body_list(self._builtin_sphere(args, node, ctx))
        if name == "cylinder":
            return self._body_list(self._builtin_cylinder(args, node, ctx))
        if name in ("translate", "rotate", "scale", "mirror", "resize", "multmatrix"):
            return self._builtin_transform(name, args, node, ctx)
        if name == "color":
            return self._builtin_color(args, node, ctx)
        if name == "union":
            return self._builtin_csg("union", node, ctx)
        if name == "difference":
            return self._builtin_csg("difference", node, ctx)
        if name == "intersection":
            return self._builtin_csg("intersection", node, ctx)
        if name == "hull":
            return self._builtin_hull(node, ctx)
        if name == "minkowski":
            return self._builtin_minkowski(node, ctx)
        if name == "polyhedron":
            return self._body_list(self._builtin_polyhedron(args, node, ctx))
        if name in ("circle", "square", "polygon"):
            return self._body_list(self._builtin_2d(name, args, node, ctx))
        if name == "text":
            return self._body_list(self._builtin_text(args, node, ctx))
        if name == "offset":
            return self._body_list(self._builtin_offset(args, node, ctx))
        if name == "projection":
            return self._body_list(self._builtin_projection(args, node, ctx))
        if name == "linear_extrude":
            return self._body_list(self._builtin_linear_extrude(args, node, ctx))
        if name == "rotate_extrude":
            return self._body_list(self._builtin_rotate_extrude(args, node, ctx))
        if name == "roof":
            return self._body_list(self._builtin_roof(args, node, ctx))
        if name == "render":
            # render() is a display hint; just pass through children
            return self._eval_children(node.children, ctx)
        if name == "surface":
            return self._body_list(self._builtin_surface(args, node, ctx))
        if name == "import":
            return self._body_list(self._builtin_import(args, node, ctx))
        if name == "echo":
            self._do_echo(node.arguments, ctx)
            return []
        if name == "assert":
            return []
        if name == "children":
            return self._builtin_children(args, ctx)
        if name == "breakpoint":
            return self._body_list(self._builtin_breakpoint(args, node, ctx))
        # Unknown module — warn with call stack, matching OpenSCAD's WARNING format
        pos = getattr(node, 'position', None)
        warn = f"WARNING: Ignoring unknown module '{name}'{self._loc(pos)}"
        trace = self._trace_lines(node)
        self._echo_fn("\n".join([warn] + trace))
        return []

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

    def _fn(self, ctx: EvalContext, r: float = 0.0) -> int:
        fn = ctx.dyn.get("$fn", 0)
        if isinstance(fn, (int, float)) and fn > 0:
            return max(3, int(fn))
        fa = ctx.dyn.get("$fa", 12.0)
        fs = ctx.dyn.get("$fs", 2.0)
        if not isinstance(fa, (int, float)) or fa <= 0:
            fa = 12.0
        if not isinstance(fs, (int, float)) or fs <= 0:
            fs = 2.0
        r = abs(r) if isinstance(r, (int, float)) and math.isfinite(r) else 0.0
        return int(math.ceil(max(5, min(360.0 / fa, r * 2.0 * math.pi / fs))))

    def _builtin_cube(self, args: dict, node: ModularCall, ctx: EvalContext) -> ColoredBody:
        size = self._get_arg(args, 0, "size", 1.0)
        center = bool(self._get_arg(args, 1, "center", False))
        if isinstance(size, (int, float)):
            size = [size, size, size]
        size = [float(s) for s in size]
        body = m3d.Manifold.cube(size, center)
        return self._tag(body, node, ctx)

    def _builtin_sphere(self, args: dict, node: ModularCall, ctx: EvalContext) -> ColoredBody:
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
        mesh = m3d.Mesh(vert_properties=verts_arr, tri_verts=tris_arr)
        body = m3d.Manifold(mesh)
        return self._tag(body, node, ctx)

    def _builtin_cylinder(self, args: dict, node: ModularCall, ctx: EvalContext) -> ColoredBody:
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

        body = m3d.Manifold.cylinder(h, float(r1), float(r2), circular_segments=segs, center=center)
        return self._tag(body, node, ctx)

    # --- transforms ---

    def _builtin_transform(self, name: str, args: dict, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        result = []
        for b in children:
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

    def _builtin_color(self, args: dict, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        c = self._get_arg(args, 0, "c", [1, 1, 1, 1])
        alpha = float(self._get_arg(args, 1, "alpha", 1.0))
        if isinstance(c, str):
            rgba = self._css_color(c, alpha)
        elif isinstance(c, (list, tuple)):
            rgba = tuple(float(x) for x in c) + (alpha,) if len(c) == 3 else tuple(float(x) for x in c[:4])
        else:
            rgba = (1.0, 1.0, 1.0, 1.0)

        child_ctx = ctx.child_ctx(color=rgba)
        children = self._eval_children(node.children, child_ctx)
        return [replace(b, color=rgba) for b in children]

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

    def _builtin_csg(self, op: str, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        # Evaluate each top-level geometry statement separately so their body groups are
        # preserved.  For difference(), all bodies from the FIRST statement form the
        # positive operand (unioned implicitly, as OpenSCAD does within a scope); bodies
        # from each subsequent statement are unioned and then subtracted.  A flat
        # evaluation loses this grouping and produces wrong results when BOSL2's
        # attachable() returns multiple bodies (parent + attached children) as the first
        # operand of difference().
        assign_nodes = [c for c in node.children if isinstance(c, Assignment)]
        geo_nodes = [c for c in node.children
                     if not isinstance(c, (Assignment, ModuleDeclaration, FunctionDeclaration))]

        # Process assignments first for side-effects (they update ctx.dyn in-place)
        if assign_nodes:
            self._eval_children(assign_nodes, ctx)

        if not geo_nodes:
            return []

        all_bg: list[ColoredBody] = []
        all_hi: list[ColoredBody] = []
        csg_result: Optional[ColoredBody] = None

        for geo_node in geo_nodes:
            stmt_bodies = self._eval_children([geo_node], ctx)

            bg = [c for c in stmt_bodies if c.role == "background"]
            fg = [c for c in stmt_bodies if c.role != "background"]
            hi = [replace(c, role="highlight_ghost") for c in fg if c.role == "highlight"]
            all_bg.extend(bg)
            all_hi.extend(hi)

            bodies_3d = [c for c in fg if c.body is not None]
            sections_2d = [c for c in fg if c.section is not None]

            if bodies_3d:
                # Union all 3D bodies from this statement before applying the op
                grp = bodies_3d[0].body
                for c in bodies_3d[1:]:
                    grp = grp + c.body
                if csg_result is None:
                    csg_result = ColoredBody(body=grp, color=bodies_3d[0].color)
                else:
                    if op == "union":
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
                else:
                    if op == "union":
                        csg_result = replace(csg_result, section=csg_result.section + grp)
                    elif op == "difference":
                        csg_result = replace(csg_result, section=csg_result.section - grp)
                    elif op == "intersection":
                        csg_result = replace(csg_result, section=csg_result.section ^ grp)

        # Return: CSG result + background ghosts + highlight overlays (separate from CSG result)
        return ([csg_result] if csg_result is not None else []) + all_bg + all_hi

    def _builtin_hull(self, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        if not children:
            return []
        bg = [c for c in children if c.role == "background"]
        fg = [c for c in children if c.role != "background"]
        hi = [replace(c, role="highlight_ghost") for c in fg if c.role == "highlight"]
        hull_result: Optional[ColoredBody] = None
        if fg:
            bodies_3d = [c.body for c in fg if c.body is not None]
            if bodies_3d:
                hull_result = ColoredBody(body=m3d.Manifold.batch_hull(bodies_3d), color=fg[0].color)
            else:
                sections = [c.section for c in fg if c.section is not None]
                if sections:
                    hull_result = ColoredBody(section=m3d.CrossSection.batch_hull(sections), color=fg[0].color)
        return ([hull_result] if hull_result is not None else []) + bg + hi

    def _builtin_polyhedron(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        points = self._get_arg(args, 0, "points", None)
        faces = self._get_arg(args, 1, "faces", None)
        if faces is None:
            faces = self._get_arg(args, 1, "triangles", None)  # legacy alias
        if points is None or faces is None:
            self.error("polyhedron: 'points' and 'faces' are required", node)
            return None
        if not isinstance(points, list) or not isinstance(faces, list):
            self.error("polyhedron: 'points' and 'faces' must be lists", node)
            return None
        for i, p in enumerate(points):
            if not isinstance(p, list) or len(p) != 3 or any(c is None for c in p):
                self.error(f"polyhedron: point[{i}] is not a valid [x,y,z] coordinate", node)
                return None
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
            mesh = m3d.Mesh(vert_properties=verts, tri_verts=tri_arr)
            body = m3d.Manifold(mesh)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"polyhedron: {e}", node)
            return None

    def _builtin_surface(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        file_arg = self._get_arg(args, 0, "file", None)
        center = bool(self._get_arg(args, None, "center", False))
        invert = bool(self._get_arg(args, None, "invert", False))

        if file_arg is None:
            self.error("surface: 'file' parameter is required", node)
            return None

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
            return None

        if heights is None or len(heights) == 0 or len(heights[0]) == 0:
            self.error("surface: empty height data", node)
            return None

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
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"surface: mesh construction failed: {e}", node)
            return None

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

    def _builtin_import(self, args: dict, node, ctx: EvalContext) -> Optional[ColoredBody]:
        import os as _os
        file_arg = self._get_arg(args, 0, "file", None)
        layer    = self._get_arg(args, None, "layer", None)
        if file_arg is None:
            self.error("import: 'file' parameter is required", node)
            return None
        path = self._resolve_import_path(file_arg, node)
        ext  = _os.path.splitext(path)[1].lower()
        try:
            if ext == ".stl":
                return self._import_stl_geometry(path, node, ctx)
            elif ext == ".obj":
                return self._import_obj_geometry(path, node, ctx)
            elif ext == ".off":
                return self._import_off_geometry(path, node, ctx)
            elif ext == ".3mf":
                return self._import_3mf_geometry(path, node, ctx)
            elif ext == ".dxf":
                return self._import_dxf_geometry(path, layer, node, ctx)
            elif ext in (".svg", ".pdf"):
                return self._import_svg_geometry(path, node, ctx)
            elif ext == ".json":
                self.error("import: .json returns data, not geometry — use as an expression", node)
                return None
            else:
                self.error(f"import: unsupported file type '{ext}'", node)
                return None
        except OSError as e:
            self.error(f"import: {e}", node)
            return None

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

    def _mesh_to_colored_body(self, verts: Any, tris: Any, node, ctx: EvalContext) -> Optional[ColoredBody]:
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
        return self._tag(body, node, ctx)

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
            return verts, tris
        else:
            count = _struct.unpack_from("<I", rest, 0)[0]
            dtype = np.dtype([("normal", np.float32, (3,)),
                              ("v0", np.float32, (3,)), ("v1", np.float32, (3,)),
                              ("v2", np.float32, (3,)), ("attr", np.uint16)])
            data  = np.frombuffer(rest[4:4 + count * 50], dtype=dtype)
            verts = np.empty((count * 3, 3), dtype=np.float64)
            verts[0::3] = data["v0"]; verts[1::3] = data["v1"]; verts[2::3] = data["v2"]
            tris = np.arange(count * 3, dtype=np.uint32).reshape(-1, 3)
            return verts, tris

    def _import_stl_geometry(self, path: str, node, ctx: EvalContext) -> Optional[ColoredBody]:
        try:
            verts, tris = self._load_stl(path)
        except Exception as e:
            self.error(f"import: {e}", node); return None
        return self._mesh_to_colored_body(verts, tris, node, ctx)

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

    def _import_obj_geometry(self, path: str, node, ctx: EvalContext) -> Optional[ColoredBody]:
        try:
            verts, tris = self._load_obj(path)
        except Exception as e:
            self.error(f"import: {e}", node); return None
        return self._mesh_to_colored_body(verts, tris, node, ctx)

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

    def _import_off_geometry(self, path: str, node, ctx: EvalContext) -> Optional[ColoredBody]:
        try:
            verts, tris = self._load_off(path)
        except Exception as e:
            self.error(f"import: {e}", node); return None
        return self._mesh_to_colored_body(verts, tris, node, ctx)

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

    def _import_3mf_geometry(self, path: str, node, ctx: EvalContext) -> Optional[ColoredBody]:
        try:
            verts, tris = self._load_3mf(path)
        except Exception as e:
            self.error(f"import: {e}", node); return None
        return self._mesh_to_colored_body(verts, tris, node, ctx)

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

    def _import_dxf_geometry(self, path: str, layer: Any, node, ctx: EvalContext) -> Optional[ColoredBody]:
        contours = self._load_dxf_contours(path, layer, node)
        if contours is None:
            return None
        if not contours:
            self.error("import: no closed contours found in DXF file", node)
            return None
        polys = [np.array(c, dtype=np.float64) for c in contours]
        cs = m3d.CrossSection(polys, m3d.FillRule.EvenOdd)
        return ColoredBody(section=cs, color=ctx.color)

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
                nums = [float(x) for x in _re.split(r'[,\s]+', el.get("points", "").strip()) if x]
                pts = list(zip(nums[::2], nums[1::2]))
                return [[_apply(p, mat) for p in pts]] if tag == "polygon" and pts else []
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

    def _import_svg_geometry(self, path: str, node, ctx: EvalContext) -> Optional[ColoredBody]:
        try:
            contours = self._load_svg_contours(path)
        except Exception as e:
            self.error(f"import: {e}", node); return None
        if not contours:
            self.error("import: no shapes found in SVG file", node); return None
        polys = [np.array(c, dtype=np.float64) for c in contours]
        cs = m3d.CrossSection(polys, m3d.FillRule.EvenOdd)
        return ColoredBody(section=cs, color=ctx.color)

    def _builtin_offset(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        r = self._get_arg(args, None, "r", None)
        delta = self._get_arg(args, None, "delta", None)
        chamfer = bool(self._get_arg(args, None, "chamfer", False))
        if r is not None:
            segs = self._fn(ctx, abs(float(r)))
            result = cs.offset(float(r), m3d.JoinType.Round, circular_segments=segs)
        elif delta is not None:
            jt = m3d.JoinType.Miter if chamfer else m3d.JoinType.Square
            result = cs.offset(float(delta), jt)
        else:
            return children[0] if children else None
        return ColoredBody(section=result, color=ctx.color)

    def _builtin_projection(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        bodies_3d = [c for c in children if c.body is not None]
        if not bodies_3d:
            return None
        combined = self._combine(bodies_3d).body
        cut = bool(self._get_arg(args, None, "cut", False))
        try:
            if cut:
                cs = combined.slice(0.0)
            else:
                raw = combined.project()
                # project() may produce self-intersecting polygons; re-fill to clean up
                polys = raw.to_polygons()
                cs = m3d.CrossSection(polys, m3d.FillRule.Positive) if polys else raw
            return ColoredBody(section=cs, color=bodies_3d[0].color)
        except Exception as e:
            self.error(f"projection: {e}", node)
            return None

    def _builtin_2d(self, name: str, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        try:
            if name == "circle":
                r = self._get_arg(args, 0, "r", None)
                d = self._get_arg(args, None, "d", None)
                if d is not None:
                    r = d / 2
                if r is None:
                    r = 1.0
                segs = self._fn(ctx, float(r))
                cs = m3d.CrossSection.circle(float(r), segs)
            elif name == "square":
                size = self._get_arg(args, 0, "size", 1.0)
                center = bool(self._get_arg(args, 1, "center", False))
                if isinstance(size, (int, float)):
                    size = [size, size]
                cs = m3d.CrossSection.square([float(size[0]), float(size[1])], center)
            elif name == "polygon":
                points = self._get_arg(args, 0, "points", None)
                paths = self._get_arg(args, 1, "paths", None)
                if points is None:
                    self.error("polygon: 'points' is required", node)
                    return None
                pts = [[float(p[0]), float(p[1])] for p in points]
                if paths is None:
                    contour = np.array(pts, dtype=np.float64)
                    cs = m3d.CrossSection([contour], m3d.FillRule.EvenOdd)
                else:
                    contours = [np.array([pts[int(i)] for i in path], dtype=np.float64) for path in paths]
                    cs = m3d.CrossSection(contours, m3d.FillRule.EvenOdd)
            else:
                return None
            return ColoredBody(section=cs, color=ctx.color)
        except Exception as e:
            self.error(f"{name}: {e}", node)
            return None

    def _builtin_text(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        """`text(text=.., size=.., halign=.., valign=.., spacing=..)`.

        Renders `text` as 2D glyph outlines from the bundled Liberation Sans
        font, laid out and aligned using the same `_measure_text`/
        `_text_align_offset` infrastructure as `textmetrics()`. `font`,
        `direction`, `language`, `script` are accepted but unused; see
        docs/evaluator.md for known gaps.
        """
        text = self._get_arg(args, 0, "text", "")
        size = self._get_arg(args, 1, "size", 10)
        halign = self._get_arg(args, None, "halign", "left")
        valign = self._get_arg(args, None, "valign", "baseline")
        spacing = self._get_arg(args, None, "spacing", 1)

        try:
            m = _measure_text(text, size, spacing)
            font = _load_default_font()
            scale = size * (100 / 72) / font["units_per_em"]
            segs = max(2, self._fn(ctx) // 2)

            sections = []
            for gname, pen_x_scaled in m["glyphs"]:
                glyph_cs = _glyph_cross_section(gname, segs)
                sections.append(glyph_cs.scale([scale, scale]).translate([pen_x_scaled, 0]))

            cs = m3d.CrossSection.batch_boolean(sections, m3d.OpType.Add) if sections else m3d.CrossSection()
            offset_x, offset_y = _text_align_offset(halign, valign, m)
            cs = cs.translate([offset_x, offset_y])
            return ColoredBody(section=cs, color=ctx.color)
        except Exception as e:
            self.error(f"text: {e}", node)
            return None

    def _builtin_linear_extrude(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
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
        try:
            body = m3d.Manifold.extrude(cs, height, slices, -twist, scale_top)
            if center:
                body = body.translate([0, 0, -height / 2])
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"linear_extrude: {e}", node)
            return None

    def _builtin_rotate_extrude(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        angle = float(self._get_arg(args, 0, "angle", 360.0))
        bounds = cs.bounds()
        max_x = max(abs(bounds[0]), abs(bounds[2])) if bounds else 0.0
        segs = self._fn(ctx, max_x)
        try:
            body = cs.revolve(segs, angle)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"rotate_extrude: {e}", node)
            return None

    def _builtin_roof(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        method = self._get_arg(args, None, "method", "voronoi")
        if method not in ("voronoi", "straight"):
            self._echo_fn(f"WARNING: Unknown roof method '{method}'. Using 'voronoi'.")
            method = "voronoi"
        try:
            if not cs.to_polygons():
                return None
            body = _skeleton_roof(cs)
            if body is None:
                body = _skeleton_roof_general(cs)
            if body is None:
                body = self._roof_sdf_fallback(cs)
            if body is None:
                return None
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"roof: {e}", node)
            return None

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

    def _builtin_minkowski(self, node: ModularCall, ctx: EvalContext) -> list[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        bg = [c for c in children if c.role == "background"]
        fg = [c for c in children if c.role != "background"]
        hi = [replace(c, role="highlight_ghost") for c in fg if c.role == "highlight"]
        bodies_3d = [c for c in fg if c.body is not None]
        if not bodies_3d:
            return bg + hi
        if len(bodies_3d) == 1:
            return bodies_3d + bg + hi
        try:
            result = bodies_3d[0].body
            for c in bodies_3d[1:]:
                result = result.minkowski_sum(c.body)
            return [ColoredBody(body=result, color=bodies_3d[0].color)] + bg + hi
        except Exception as e:
            self.error(f"minkowski: {e}", node)
            return bg + hi

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
        var_seqs: list[tuple[str, list]] = []
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
            var_seqs.append((name, values))

        result = []
        _debugging = self._debugging
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx(children_nodes=ctx.children_nodes,
                                     children_caller_ctx=ctx.children_caller_ctx)
            for vname, val in combo:
                loop_ctx.let[vname] = val
            if _debugging and node.body:
                self._check_debug(node.body[0], loop_ctx, expr_level=True)
            result.extend(self._eval_children(node.body, loop_ctx))
        return result

    @staticmethod
    def _cartesian(var_seqs: list[tuple[str, list]]):
        if not var_seqs:
            yield []
            return
        names, value_lists = zip(*var_seqs)
        for combo in _product(*value_lists):
            yield list(zip(names, combo))

    def _eval_intersection_for(self, node: ModularIntersectionFor, ctx: EvalContext) -> list[ColoredBody]:
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                return []
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
        iterations = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx(children_nodes=ctx.children_nodes,
                                     children_caller_ctx=ctx.children_caller_ctx)
            for vname, val in combo:
                loop_ctx.let[vname] = val
            if _debugging and body_node:
                self._check_debug(body_node[0], loop_ctx, expr_level=True)
            children = self._eval_children(body_node, loop_ctx)
            if children:
                iterations.append(self._combine(children))

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
        var_seqs: list[tuple[str, list]] = []
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
            var_seqs.append((name, values))

        result = []
        _debugging = self._debugging
        is_lc = type(node.body) is ListComprehension

        if len(var_seqs) == 1:
            name, values = var_seqs[0]
            if not values:
                return result
            loop_ctx = ctx.let_child_ctx()
            let_dict = loop_ctx.let
            for val in values:
                let_dict[name] = val
                self._expr_depth += 1
                if _debugging:
                    self._check_debug(node, loop_ctx)
                if is_lc:
                    result.append(self._eval_list_comp(node.body, loop_ctx))
                else:
                    result.extend(self._eval_list_comp_body(node.body, loop_ctx))
                self._expr_depth -= 1
            return result

        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.let_child_ctx()
            for vname, val in combo:
                loop_ctx.let[vname] = val
            self._expr_depth += 1
            if _debugging:
                self._check_debug(node, loop_ctx)
            if is_lc:
                result.append(self._eval_list_comp(node.body, loop_ctx))
            else:
                result.extend(self._eval_list_comp_body(node.body, loop_ctx))
            self._expr_depth -= 1
        return result

    _MAX_CFOR_ITERATIONS = 1_000_000

    def _eval_listcomp_cfor(self, node: ListCompCFor, ctx: EvalContext) -> list:
        loop_ctx = ctx.let_child_ctx()
        for assign in node.inits:
            loop_ctx.let[assign.name.name] = self._eval_expr(assign.expr, loop_ctx)

        result = []
        iterations = 0
        _debugging = self._debugging
        is_lc = type(node.body) is ListComprehension
        while self._eval_expr(node.condition, loop_ctx):
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
        if len(a) == 2 and len(b) == 2:
            return a[0]*b[1] - a[1]*b[0]
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]

    def _builtin_rands(self, minval, maxval, n, seed=None):
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
        """`textmetrics(text=.., size=.., halign=.., valign=.., spacing=..)`.

        Measures `text` against the bundled Liberation Sans font (see
        `_measure_text`) and returns an `OscObject` with `position`, `size`,
        `ascent`, `descent`, `offset`, `advance` — matching real OpenSCAD's
        key order and (for Liberation Sans) numeric values. `font`,
        `direction`, `language`, `script` are accepted but unused; see
        docs/evaluator.md for known gaps.
        """
        text = self._get_arg(args, 0, "text", "")
        size = self._get_arg(args, 1, "size", 10)
        halign = self._get_arg(args, None, "halign", "left")
        valign = self._get_arg(args, None, "valign", "baseline")
        spacing = self._get_arg(args, None, "spacing", 1)

        m = _measure_text(text, size, spacing)
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
        """`fontmetrics(size=.., font=..)` — global metrics of the bundled
        Liberation Sans font, scaled for `size`. Returns a nested `OscObject`
        with `nominal`/`max`/`interline`/`font`. `font=` is echoed back into
        `font.family` for round-tripping but doesn't change the measurements
        (see docs/evaluator.md for known gaps)."""
        size = self._get_arg(args, 0, "size", 10)
        font_name = self._get_arg(args, None, "font", "Liberation Sans")

        font = _load_default_font()
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
                "family": font_name,
                "style": "Regular",
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
