"""VNF tile textures: BOSL2's seam check, and moves that keep seams intact.

A VNF tile is a VNF whose X and Y lie in the unit square, repeated edge to
edge to texture a surface (BOSL2's `texture=` on `linear_sweep()`, `cyl()`
and friends). A tile only works if each vertex on a boundary edge has a
twin on the opposite edge at the same height, so that neighbouring copies
meet without a crack.

Pure Python, no Qt: the viewer calls it, and so can a test.
"""
from __future__ import annotations

from collections import Counter

_EPSILON = 1e-9     # BOSL2's approx() tolerance


def _approx(a, b) -> bool:
    return all(abs(p - q) <= _EPSILON for p, q in zip(a, b))


def _on_edge(c) -> bool:
    # Exact, as BOSL2 tests it (`v.x==0 || v.x==1`).
    return c == 0 or c == 1


def is_tile(verts) -> bool:
    """Every vertex's X and Y lie in the unit square."""
    return all(0 <= v[0] <= 1 and 0 <= v[1] <= 1 for v in verts)


def tile_problems(vnf) -> tuple[list[int], list[int]]:
    """`(out_of_range, unmatched)` vertex indices, the two things BOSL2's
    `_validate_texture` asserts on (skin.scad).

    `out_of_range`: X or Y outside [0, 1]. `unmatched`: a vertex on an
    open boundary edge lying on X=0/1 (or Y=0/1) with no vertex at
    `[1-x, y, z]` (or `[x, 1-y, z]`) among the other boundary vertices.
    Only open edges count, as BOSL2 has it: a closed-off side face touching
    the tile's edge is not a seam.
    """
    verts, faces = vnf
    out_of_range = [i for i, v in enumerate(verts) if not is_tile([v])]
    on_edge = [_on_edge(v[0]) or _on_edge(v[1]) for v in verts]
    uses = Counter()
    for face in faces:
        idx = [int(i) for i in face]
        for a, b in zip(idx, idx[1:] + idx[:1]):
            if on_edge[a] and on_edge[b]:
                uses[(min(a, b), max(a, b))] += 1
    boundary = sorted({i for e, n in uses.items() if n == 1 for i in e})
    h = [i for i in boundary if _on_edge(verts[i][0])]
    v = [i for i in boundary if _on_edge(verts[i][1])]
    unmatched = set()
    for i in h:
        x, y, z = verts[i]
        if not any(_approx(verts[j], (1 - x, y, z)) for j in h):
            unmatched.add(i)
    for i in v:
        x, y, z = verts[i]
        if not any(_approx(verts[j], (x, 1 - y, z)) for j in v):
            unmatched.add(i)
    return out_of_range, sorted(unmatched)


def set_aside_rim_holes(report):
    """Drop the open edges that run along one side of the unit square from
    a vnf_validate report, counting them in `expected_open` instead.

    A tile is an open surface: its rim along X=0/1 and Y=0/1 is where the
    next copy joins on, so manifold validation reports it as holes on every
    valid tile. An open edge anywhere else -- a crack inside the tile, or
    one running diagonally across a corner -- is still reported.
    """
    pts = report.welded_points

    def along_a_side(a, b):
        return any(abs(pts[a][k] - e) <= _EPSILON and abs(pts[b][k] - e) <= _EPSILON
                   for k in (0, 1) for e in (0, 1))

    rim = [e for e in report.hole_edges if along_a_side(*e)]
    report.hole_edges = [e for e in report.hole_edges if not along_a_side(*e)]
    report.expected_open += len(rim)
    return report


def linked_moves(verts, i, new_pos, lock=True) -> list[tuple[int, list]]:
    """Every `(index, position)` that moving vertex `i` to `new_pos` should
    set, so that a seam which matched before still matches after.

    A vertex on X=0/1 carries its twin on the opposite X edge along in Y
    and Z; likewise for Y; a corner carries all three others in Z. With
    `lock` (a drag or nudge) the vertex also stays on its edge and inside
    the unit square -- dragging freely would break the seam it is there to
    keep. Without it (a value typed into the table) the typed value is
    taken as given.
    """
    v = verts[i]
    ex, ey = _on_edge(v[0]), _on_edge(v[1])
    x, y, z = new_pos
    if lock:
        x = v[0] if ex else min(max(x, 0), 1)
        y = v[1] if ey else min(max(y, 0), 1)
    moves = [(i, [x, y, z])]
    if not (ex or ey):
        return moves
    twins = []
    if ex:
        twins.append((1 - v[0], v[1], v[2]))
    if ey:
        twins.append((v[0], 1 - v[1], v[2]))
    if ex and ey:
        twins.append((1 - v[0], 1 - v[1], v[2]))
    for j, w in enumerate(verts):
        if j != i and any(_approx(w, t) for t in twins):
            moves.append((j, [w[0] if ex else x, w[1] if ey else y, z]))
    return moves
