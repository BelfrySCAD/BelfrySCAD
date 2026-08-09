#!/usr/bin/env python3
"""Snapping for the measurement tools picks real geometry, not artifacts.

The case worth writing first: the viewport shows a triangulated mesh, so a
flat square face is two triangles with a diagonal across it. That diagonal
is not a feature of the model -- it moves when $fn changes -- and snapping
to it would give a measurement that looks precise and means nothing.

Pure math only; no GL, no window.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def cube_tris(s=10.0):
    """A unit-ish cube as (v0, v1, v2) arrays -- 12 triangles, two per face."""
    c = np.array([[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
                  [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]], dtype=np.float64)
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    v0 = np.array([c[a] for a, _, _ in faces])
    v1 = np.array([c[b] for _, b, _ in faces])
    v2 = np.array([c[d] for _, _, d in faces])
    return v0, v1, v2


def main():
    from belfryscad.engine.renderer import (
        SNAP_EDGE, SNAP_FACE, SNAP_VERTEX, choose_snap,
        feature_edges_of_triangle, triangle_normal,
    )

    n = triangle_normal(np.array([0., 0, 0]), np.array([1., 0, 0]), np.array([0., 1, 0]))
    check("a triangle's normal is a unit vector", abs(np.linalg.norm(n) - 1) < 1e-9)
    check("and points the right way", np.allclose(n, [0, 0, 1]), str(n))
    degenerate = triangle_normal(np.array([0., 0, 0]), np.array([1., 0, 0]), np.array([2., 0, 0]))
    check("a degenerate triangle yields no normal, not a crash",
          np.allclose(degenerate, 0), str(degenerate))

    v0, v1, v2 = cube_tris()

    # Triangle 0 is half the bottom face: corners (0,0,0) (10,10,0) (10,0,0).
    # Its edges are two real cube edges and one diagonal across the face.
    edges = feature_edges_of_triangle(v0, v1, v2, 0)
    check("a face's triangulation diagonal is not a feature edge",
          len(edges) == 2, f"{len(edges)} feature edges, expected 2")

    def has_edge(edges, a, b):
        a, b = np.array(a, float), np.array(b, float)
        return any((np.allclose(p, a) and np.allclose(q, b))
                   or (np.allclose(p, b) and np.allclose(q, a)) for p, q in edges)

    check("the two real cube edges are features",
          has_edge(edges, [0, 0, 0], [10, 0, 0])
          and has_edge(edges, [10, 10, 0], [10, 0, 0]), str(edges))
    check("and the diagonal specifically is not",
          not has_edge(edges, [0, 0, 0], [10, 10, 0]), str(edges))

    # Every triangle of a closed cube should report exactly its two real
    # edges -- if the rule only worked for one orientation it would show here.
    counts = [len(feature_edges_of_triangle(v0, v1, v2, i)) for i in range(12)]
    check("every cube triangle reports exactly two feature edges",
          counts == [2] * 12, str(counts))

    # An open edge -- nothing shares it -- is a feature by definition.
    lone0 = np.array([[0., 0, 0]])
    lone1 = np.array([[1., 0, 0]])
    lone2 = np.array([[0., 1, 0]])
    check("a boundary edge counts as a feature",
          len(feature_edges_of_triangle(lone0, lone1, lone2, 0)) == 3)

    # A shallow crease should not register: this is what stops a cylinder's
    # facets each becoming a snappable edge.
    a = np.array([[0., 0, 0]]), np.array([[10., 0, 0]]), np.array([[0., 10, 0]])
    tilt = np.radians(5.0)
    b0 = np.array([[0., 0, 0]])
    b1 = np.array([[10., 0, 0]])
    b2 = np.array([[0., -10 * np.cos(tilt), 10 * np.sin(tilt)]])
    v0c = np.concatenate([a[0], b0]); v1c = np.concatenate([a[1], b1]); v2c = np.concatenate([a[2], b2])
    shallow = feature_edges_of_triangle(v0c, v1c, v2c, 0)
    check("a 5-degree crease is not a feature edge",
          not has_edge(shallow, [0, 0, 0], [10, 0, 0]), str(shallow))

    # ...but a sharp one is.
    b2 = np.array([[0., 0, 10.]])
    v2c = np.concatenate([a[2], b2])
    sharp = feature_edges_of_triangle(v0c, v1c, v2c, 0)
    check("a 90-degree crease is", has_edge(sharp, [0, 0, 0], [10, 0, 0]), str(sharp))

    # --- choosing a snap -------------------------------------------------
    # Orthographic-ish MVP looking down -Z, so screen x/y track world x/y.
    W = H = 400
    mvp = np.array([[2 / 20, 0, 0, 0], [0, 2 / 20, 0, 0], [0, 0, -0.01, 0], [0, 0, 0, 1.0]])

    def screen_of(p):
        clip = mvp @ np.array([p[0], p[1], p[2], 1.0])
        ndc = clip[:2] / clip[3]
        return ((ndc[0] * .5 + .5) * W, (1 - (ndc[1] * .5 + .5)) * H)

    tri = (np.array([0., 0, 0]), np.array([10., 10, 0]), np.array([10., 0, 0]))
    real_edges = [(np.array([0., 0, 0]), np.array([10., 0, 0]))]

    def ray_through(p):
        """The pick ray for a click over world point p. Must follow the
        click: edge snapping finds the point on the edge closest to the
        ray, so a ray fixed at the origin would test nothing."""
        return np.array([p[0], p[1], 50.]), np.array([0., 0, -1.])

    ray_o, ray_d = ray_through([0, 0, 0])

    px, py = screen_of([10, 0, 0])
    pt, kind = choose_snap(np.array([9.8, 0.2, 0]), tri, real_edges, ray_o, ray_d,
                           mvp, px, py, W, H)
    check("a click on a corner snaps to the vertex", kind == SNAP_VERTEX, kind)
    check("and lands exactly on it", np.allclose(pt, [10, 0, 0]), str(pt))

    px, py = screen_of([5, 0, 0])
    pt, kind = choose_snap(np.array([5., 0.3, 0]), tri, real_edges,
                           *ray_through([5, 0.3, 0]), mvp, px, py, W, H)
    check("a click along an edge snaps to the edge", kind == SNAP_EDGE, kind)
    check("and lands on it", abs(pt[1]) < 1e-6, str(pt))

    px, py = screen_of([7, 4, 0])
    raw = np.array([7., 4, 0])
    pt, kind = choose_snap(raw, tri, real_edges, *ray_through(raw), mvp, px, py, W, H)
    check("a click in open face falls back to the surface point",
          kind == SNAP_FACE and np.allclose(pt, raw), f"{kind} {pt}")

    # Vertex must win near a corner even though the edge is nearer the
    # cursor there -- that is the whole reason for the priority.
    px, py = screen_of([9.6, 0, 0])
    pt, kind = choose_snap(np.array([9.6, 0.05, 0]), tri, real_edges,
                           *ray_through([9.6, 0.05, 0]), mvp, px, py, W, H)
    check("near a corner the vertex wins over the edge through it",
          kind == SNAP_VERTEX, kind)

    # With no feature edges, a click along the diagonal must NOT snap to it.
    px, py = screen_of([5, 5, 0])
    pt, kind = choose_snap(np.array([5., 5, 0]), tri, [],
                           *ray_through([5, 5, 0]), mvp, px, py, W, H)
    check("with no feature edges a mid-face click stays on the face",
          kind == SNAP_FACE, kind)

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
