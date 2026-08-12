"""Manifold validation for a VNF, reporting *where* each problem is.

The evaluator's own `check_mesh` counts problems, which is what an export
warning needs. Highlighting them in the viewer needs the elements
themselves, so this reports edges and face pairs rather than totals.

Pure numpy, no Qt: the viewer calls it, and so can a test.

Five conditions, in the order a mesh usually fails them:

  holes            an edge with only one face on it -- the mesh is open
  flipped          two faces sharing an edge that traverse it the same
                   way, so one of them is wound backwards
  t_joints         a vertex lying in the middle of another face's edge;
                   the surface looks closed but the two faces do not
                   share an edge, so it is not welded
  intersections    two faces that pass through each other
  overlaps         two coplanar faces covering the same area

Vertices are welded by rounded position before anything else, because
that is what every consumer of an exported file does. Two coincident but
separately indexed vertices form a hole no index-based check can see.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Pairwise tests are quadratic in the worst case. A spatial grid keeps
# the usual case near-linear, but a pathological mesh (everything in one
# cell) can still blow up, so the pair budget is capped and the result
# says so rather than hanging the UI.
MAX_PAIRS = 4_000_000


@dataclass
class VNFReport:
    """Where a mesh fails to be a closed, sound solid.

    Every vertex index here indexes `welded_points`, not the VNF's own
    point list -- welding merges coincident vertices and therefore
    renumbers them. `remap` translates: `remap[original] -> welded`. The
    viewer draws from `welded_points`, so this is the form it wants;
    anything reporting back to the user in terms of the original mesh
    needs to invert `remap`.
    """
    hole_edges: list = field(default_factory=list)        # [(vi, vj), ...]
    flipped_edges: list = field(default_factory=list)     # [(vi, vj), ...]
    t_joints: list = field(default_factory=list)          # [(vert, (vi, vj)), ...]
    intersecting: list = field(default_factory=list)      # [(face_i, face_j), ...]
    overlapping: list = field(default_factory=list)       # [(face_i, face_j), ...]
    nonmanifold_edges: list = field(default_factory=list)  # [(vi, vj), ...] 3+ faces
    welded_points: np.ndarray | None = None               # positions, post-weld
    remap: np.ndarray | None = None                       # original index -> welded
    truncated: bool = False                               # a budget was hit
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.hole_edges or self.flipped_edges or self.t_joints
                    or self.intersecting or self.overlapping
                    or self.nonmanifold_edges)

    def summary(self) -> str:
        if self.ok:
            return "No problems found: closed, consistently wound, no self-intersections."
        parts = []
        for n, label in ((len(self.hole_edges), "hole edge"),
                         (len(self.flipped_edges), "flipped-normal edge"),
                         (len(self.nonmanifold_edges), "non-manifold edge"),
                         (len(self.t_joints), "T-joint"),
                         (len(self.intersecting), "intersecting face pair"),
                         (len(self.overlapping), "overlapping coplanar pair")):
            if n:
                parts.append(f"{n} {label}{'' if n == 1 else 's'}")
        text = ", ".join(parts)
        if self.truncated:
            text += " (search truncated -- mesh too large for an exhaustive pass)"
        return text


def _weld(points, tol=1e-6):
    """Merge vertices that share a position. Returns (positions, remap)."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.size == 0:
        return pts.reshape(0, 3), np.zeros(0, dtype=np.int64)
    decimals = max(0, int(round(-np.log10(tol))))
    keys, inverse = np.unique(np.round(pts, decimals), axis=0, return_inverse=True)
    return keys, inverse.ravel()


def _int_faces(faces):
    """Coerce faces to lists of `int`, or None if they are not indices.

    Rejects a non-integral value (2.5 is not a vertex) rather than
    truncating it into a valid-looking index.
    """
    out = []
    try:
        for f in faces:
            row = []
            for i in f:
                n = int(i)
                if n != i:
                    return None
                row.append(n)
            out.append(row)
    except (TypeError, ValueError):
        return None
    return out


def _triangles(faces, remap):
    """Fan-triangulate every face, keeping which face each came from.

    A VNF face may be a polygon. The geometric tests need triangles; the
    report needs the original face, so both are carried.
    """
    tris, owner = [], []
    for fi, f in enumerate(faces):
        idx = [int(remap[i]) for i in f]
        for k in range(1, len(idx) - 1):
            tris.append((idx[0], idx[k], idx[k + 1]))
            owner.append(fi)
    return np.array(tris, dtype=np.int64).reshape(-1, 3), np.array(owner, dtype=np.int64)


def _edge_use(faces, remap):
    """How many times each undirected edge is used, and in which direction.

    Directed edges come from walking each face's own winding. A sound
    closed surface uses every edge exactly twice, once in each direction.
    """
    directed = {}
    for fi, f in enumerate(faces):
        idx = [int(remap[i]) for i in f]
        n = len(idx)
        for k in range(n):
            a, b = idx[k], idx[(k + 1) % n]
            if a == b:
                continue          # degenerate, not an edge
            directed.setdefault((a, b), []).append(fi)
    return directed


def _edges_report(faces, remap):
    directed = _edge_use(faces, remap)
    holes, flipped, nonmanifold = [], [], []
    seen = set()
    for (a, b), fwd in directed.items():
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        back = directed.get((b, a), [])
        total = len(fwd) + len(back)
        if total == 1:
            holes.append(key)
        elif total > 2:
            nonmanifold.append(key)
        elif len(fwd) == 2 or len(back) == 2:
            # Both faces walk the edge the same way: one is wound backwards
            # relative to the other.
            flipped.append(key)
    return holes, flipped, nonmanifold


def _t_joints(points, faces, remap, tol=1e-7):
    """Vertices sitting in the interior of an edge they are not part of.

    Only edges are searched, not face interiors: a T-joint is specifically
    the case where two surfaces meet along a line but do not share
    endpoints, which is what leaves a crack a slicer will find.
    """
    edges = set()
    for f in faces:
        idx = [int(remap[i]) for i in f]
        n = len(idx)
        for k in range(n):
            a, b = idx[k], idx[(k + 1) % n]
            if a != b:
                edges.add((a, b) if a < b else (b, a))
    if not edges:
        return []

    edge_arr = np.array(sorted(edges), dtype=np.int64)
    p0 = points[edge_arr[:, 0]]
    p1 = points[edge_arr[:, 1]]
    d = p1 - p0
    length2 = np.einsum("ij,ij->i", d, d)
    keep = length2 > tol * tol
    edge_arr, p0, d, length2 = edge_arr[keep], p0[keep], d[keep], length2[keep]

    hits = []
    # Bucket edges by their bounding box on a grid, so each vertex is only
    # tested against edges that could plausibly contain it.
    for vi, p in enumerate(points):
        w = p - p0
        t = np.einsum("ij,ij->i", w, d) / length2
        inside = (t > tol) & (t < 1.0 - tol)
        if not inside.any():
            continue
        cand = np.nonzero(inside)[0]
        proj = p0[cand] + d[cand] * t[cand, None]
        dist2 = np.einsum("ij,ij->i", p - proj, p - proj)
        on = cand[dist2 <= tol * tol]
        for e in on:
            a, b = int(edge_arr[e, 0]), int(edge_arr[e, 1])
            if vi != a and vi != b:
                hits.append((int(vi), (a, b)))
    return hits


def _tri_aabbs(points, tris):
    p = points[tris]
    return p.min(axis=1), p.max(axis=1)


def _candidate_pairs(lo, hi, budget=MAX_PAIRS):
    """Index pairs whose bounding boxes overlap.

    A uniform grid over the average box size: the usual mesh spreads
    across many cells and this stays near-linear, while a degenerate one
    stops at the budget instead of running forever.
    """
    n = len(lo)
    if n < 2:
        return [], False
    size = np.maximum((hi - lo).mean(axis=0), 1e-9)
    cell = float(max(size.max(), 1e-9))
    origin = lo.min(axis=0)
    buckets = {}
    for i in range(n):
        c0 = np.floor((lo[i] - origin) / cell).astype(np.int64)
        c1 = np.floor((hi[i] - origin) / cell).astype(np.int64)
        for x in range(c0[0], c1[0] + 1):
            for y in range(c0[1], c1[1] + 1):
                for z in range(c0[2], c1[2] + 1):
                    buckets.setdefault((x, y, z), []).append(i)

    pairs, truncated = set(), False
    for members in buckets.values():
        m = len(members)
        if m < 2:
            continue
        if len(pairs) + m * (m - 1) // 2 > budget:
            truncated = True
            break
        for a in range(m):
            for b in range(a + 1, m):
                i, j = members[a], members[b]
                if (lo[i] <= hi[j]).all() and (lo[j] <= hi[i]).all():
                    pairs.add((i, j) if i < j else (j, i))
    return sorted(pairs), truncated


def _tri_plane(points, tri):
    a, b, c = points[tri[0]], points[tri[1]], points[tri[2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n)
    if ln < 1e-12:
        return None, None
    n = n / ln
    return n, float(np.dot(n, a))


def _segments_cross(p, q, tol):
    """2D segment intersection, used for coplanar overlap."""
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    d1 = cross(q[0], q[1], p[0])
    d2 = cross(q[0], q[1], p[1])
    d3 = cross(p[0], p[1], q[0])
    d4 = cross(p[0], p[1], q[1])
    return ((d1 > tol and d2 < -tol) or (d1 < -tol and d2 > tol)) and \
           ((d3 > tol and d4 < -tol) or (d3 < -tol and d4 > tol))


def _point_in_tri_2d(pt, tri2, tol):
    (ax, ay), (bx, by), (cx, cy) = tri2
    px, py = pt
    d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(d) < tol:
        return False
    u = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
    v = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
    w = 1.0 - u - v
    return u > tol and v > tol and w > tol


def _coplanar_overlap(points, t1, t2, normal, tol=1e-9):
    """Do two coplanar triangles cover any of the same area?"""
    axis = int(np.argmax(np.abs(normal)))
    keep = [i for i in range(3) if i != axis]
    a = points[t1][:, keep]
    b = points[t2][:, keep]
    if any(_point_in_tri_2d(p, b, tol) for p in a):
        return True
    if any(_point_in_tri_2d(p, a, tol) for p in b):
        return True
    for i in range(3):
        for j in range(3):
            if _segments_cross((a[i], a[(i + 1) % 3]),
                               (b[j], b[(j + 1) % 3]), tol):
                return True
    return False


def _tri_intersects(points, t1, t2, tol=1e-9):
    """Do two triangles pass through each other?

    Signed distances of each triangle's corners to the other's plane; if
    either triangle sits wholly on one side there is no intersection.
    Shared vertices are ignored -- neighbours touching along an edge are
    not an intersection.
    """
    if set(t1.tolist()) & set(t2.tolist()):
        return False
    n1, d1 = _tri_plane(points, t1)
    n2, d2 = _tri_plane(points, t2)
    if n1 is None or n2 is None:
        return False
    s2 = points[t2] @ n1 - d1
    if (s2 > tol).all() or (s2 < -tol).all():
        return False
    s1 = points[t1] @ n2 - d2
    if (s1 > tol).all() or (s1 < -tol).all():
        return False
    if abs(abs(float(np.dot(n1, n2))) - 1.0) < 1e-9 and abs(d1 - abs(d2)) < tol:
        return False        # coplanar: reported as an overlap instead
    # Both straddle: intersect each edge with the other's plane and see
    # whether the crossing point lands inside that triangle.
    for tri, other, n, d in ((t1, t2, n2, d2), (t2, t1, n1, d1)):
        pts = points[tri]
        s = pts @ n - d
        for i in range(3):
            j = (i + 1) % 3
            if (s[i] > tol and s[j] < -tol) or (s[i] < -tol and s[j] > tol):
                t = s[i] / (s[i] - s[j])
                hit = pts[i] + (pts[j] - pts[i]) * t
                axis = int(np.argmax(np.abs(n)))
                keep = [k for k in range(3) if k != axis]
                if _point_in_tri_2d(hit[keep], points[other][:, keep], tol):
                    return True
    return False


def validate_vnf(vnf, weld_tol=1e-6, max_pairs=MAX_PAIRS) -> VNFReport:
    """Check a `[points, faces]` VNF and report where it is unsound."""
    rep = VNFReport()
    try:
        points_in, faces = vnf[0], vnf[1]
    except Exception:                                        # noqa: BLE001
        rep.notes.append("not a VNF: expected [points, faces]")
        return rep
    if len(points_in) == 0 or len(faces) == 0:
        rep.notes.append("empty VNF")
        rep.welded_points = np.zeros((0, 3))
        return rep

    points, remap = _weld(points_in, weld_tol)
    rep.welded_points = points
    rep.remap = remap
    if len(points) < len(points_in):
        rep.notes.append(f"{len(points_in) - len(points)} coincident vertices welded "
                         f"before checking")

    # Every number in an evaluated VNF is a double -- OpenSCAD has no
    # integer type -- so real face indices arrive as 0.0, 1.0, 2.0 and
    # cannot index anything until they are coerced. Done once here rather
    # than at each `remap[i]` below.
    faces = _int_faces(faces)
    if faces is None:
        rep.notes.append("faces are not lists of vertex indices")
        return rep
    bad = [i for f in faces for i in f if not 0 <= i < len(points_in)]
    if bad:
        rep.notes.append(f"{len(bad)} face indices are outside the vertex list "
                         f"(e.g. {bad[0]}); nothing else could be checked")
        return rep

    rep.hole_edges, rep.flipped_edges, rep.nonmanifold_edges = _edges_report(faces, remap)
    rep.t_joints = _t_joints(points, faces, remap)

    tris, owner = _triangles(faces, remap)
    if len(tris):
        lo, hi = _tri_aabbs(points, tris)
        pairs, truncated = _candidate_pairs(lo, hi, max_pairs)
        rep.truncated = truncated
        inter, over = set(), set()
        for i, j in pairs:
            if owner[i] == owner[j]:
                continue                    # same original face, fanned
            t1, t2 = tris[i], tris[j]
            n1, d1 = _tri_plane(points, t1)
            n2, d2 = _tri_plane(points, t2)
            coplanar = (n1 is not None and n2 is not None
                        and abs(abs(float(np.dot(n1, n2))) - 1.0) < 1e-9
                        and abs(abs(d1) - abs(d2)) < 1e-7)
            fi, fj = int(owner[i]), int(owner[j])
            key = (fi, fj) if fi < fj else (fj, fi)
            if coplanar:
                if _coplanar_overlap(points, t1, t2, n1):
                    over.add(key)
            elif _tri_intersects(points, t1, t2):
                inter.add(key)
        rep.intersecting = sorted(inter)
        rep.overlapping = sorted(over)
    return rep
