"""Edges and faces of a triangle mesh, and the measurements between them (#1131).

The viewport holds triangle soup: each triangle its own three positions,
with no shared indices. Measuring an edge or a face needs more than that --
which triangles meet along an edge, which of them lie in one plane -- so
`MeshTopology` recovers it, matching corners by position.

A **face** is a connected set of triangles in one plane, so a cube side is
one face and each facet of a `$fn` cylinder is its own: true to the mesh,
which is the only geometry there is. An **edge** is a run of collinear
feature edges (a boundary, or where the surface turns by more than
`FEATURE_ANGLE`), so a cube edge split by a vertex on it still measures
its whole length.

Pure numpy, no GL or Qt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

FEATURE_ANGLE = 20.0        # degrees; as renderer.feature_edges_of_triangle
_COPLANAR_COS = np.cos(np.radians(0.05))


@dataclass
class Face:
    tris: np.ndarray                 # triangle indices
    normal: np.ndarray
    centroid: np.ndarray             # area-weighted
    area: float
    outline: list = field(default_factory=list)   # [(a, b)] boundary segments


@dataclass
class Feature:
    """One selection: a point, an edge or a face."""
    kind: str                        # "point" | "edge" | "face"
    point: np.ndarray                # the point; an edge's midpoint; a face's centroid
    tag: str = ""                    # a point's origin: vertex / edge cp / face cp / face
    a: np.ndarray | None = None      # edge endpoints
    b: np.ndarray | None = None
    face: Face | None = None

    def segments(self) -> list:
        """What to draw to show this selection."""
        if self.kind == "edge":
            return [(self.a, self.b)]
        if self.kind == "face":
            return list(self.face.outline)
        return []


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class MeshTopology:
    """Adjacency of triangle soup `v0, v1, v2` (each (N, 3)), by position."""

    def __init__(self, v0, v1, v2):
        v0, v1, v2 = (np.asarray(v, dtype=np.float64) for v in (v0, v1, v2))
        self.v = np.stack([v0, v1, v2], axis=1)              # (N, 3, 3)
        n = len(v0)
        corners = self.v.reshape(-1, 3)
        span = float(np.ptp(corners, axis=0).max()) if n else 1.0
        self.tol = 1e-6 * max(span, 1.0)
        # Packed into one int64 per corner: a 1-D unique is ~10x a row-wise
        # one. span/tol is 1e6, so each axis needs 20 bits.
        q = np.round((corners - corners.min(axis=0)) / self.tol).astype(np.int64) if n else \
            np.zeros((0, 3), dtype=np.int64)
        r = int(q.max()) + 1 if n else 1
        uniq_keys, vid = np.unique((q[:, 0] * r + q[:, 1]) * r + q[:, 2], return_inverse=True)
        self.vid = vid.reshape(n, 3)
        cross = np.cross(v1 - v0, v2 - v0)
        dbl = np.linalg.norm(cross, axis=1)
        self.area = dbl / 2
        self.normal = cross / np.where(dbl > 0, dbl, 1.0)[:, None]
        self.offset = np.einsum("ij,ij->i", self.normal, v0)

        # Edge k of triangle t runs corner k -> corner (k+1)%3.
        ea = self.vid
        eb = np.roll(self.vid, -1, axis=1)
        nv = len(uniq_keys)
        packed = (np.minimum(ea, eb) * nv + np.maximum(ea, eb)).reshape(-1)
        uniq, eid = np.unique(packed, return_inverse=True)
        self.edge_verts = np.stack([uniq // nv, uniq % nv], axis=1)   # (E, 2) vertex ids
        self.eid = eid.reshape(n, 3)
        order = np.argsort(eid, kind="stable")
        self._edge_tris = order // 3
        self._edge_start = np.searchsorted(eid[order], np.arange(len(uniq) + 1))

        # Feature edges: a boundary, more than two triangles, or a sharp turn.
        count = np.diff(self._edge_start)
        feature = count != 2
        two = np.nonzero(count == 2)[0]
        t1 = self._edge_tris[self._edge_start[two]]
        t2 = self._edge_tris[self._edge_start[two] + 1]
        dots = np.einsum("ij,ij->i", self.normal[t1], self.normal[t2])
        feature[two] = dots < np.cos(np.radians(FEATURE_ANGLE))
        self.feature = feature

        # One position per vertex id, and the feature edges at each vertex.
        self.pos = np.zeros((len(uniq_keys), 3))
        self.pos[self.vid.reshape(-1)] = corners
        self._vert_edges: dict[int, list[int]] = {}
        for e in np.nonzero(feature)[0]:
            for vtx in self.edge_verts[e]:
                self._vert_edges.setdefault(int(vtx), []).append(int(e))
        self._face_of = np.full(n, -1, dtype=np.int64)
        self._faces: list[Face] = []

    # -- queries ---------------------------------------------------------

    def edge_tris(self, e: int) -> np.ndarray:
        return self._edge_tris[self._edge_start[e]:self._edge_start[e + 1]]

    def corner_vertex(self, tri: int, k: int) -> int:
        return int(self.vid[tri, k])

    def feature_edges_of(self, tri: int) -> list[int]:
        return [int(e) for e in self.eid[tri] if self.feature[e]]

    def edges_at_vertex(self, vtx: int) -> list[tuple]:
        """The whole edges (collinear runs) meeting at vertex `vtx`."""
        return [self.edge_chain(e) for e in self._vert_edges.get(vtx, [])]

    def edge_chain(self, e: int) -> tuple:
        """Endpoints of the straight run of feature edges through edge `e`:
        followed past a vertex only where exactly one feature edge carries
        on in the same direction."""
        a, b = (int(x) for x in self.edge_verts[e])
        d = _unit(self.pos[b] - self.pos[a])
        ends = []
        for start, sign in ((b, 1.0), (a, -1.0)):
            cur, prev_e = start, e
            for _ in range(len(self.edge_verts)):
                nxt = []
                for f in self._vert_edges.get(cur, []):
                    if f == prev_e:
                        continue
                    p, q = (int(x) for x in self.edge_verts[f])
                    other = q if p == cur else p
                    step = _unit(self.pos[other] - self.pos[cur])
                    if float(np.dot(step, d)) * sign > 1 - 1e-9:
                        nxt.append((f, other))
                if len(nxt) != 1:
                    break
                prev_e, cur = nxt[0]
            ends.append(cur)
        return self.pos[ends[1]].copy(), self.pos[ends[0]].copy()

    def face_of(self, tri: int) -> Face:
        """The planar face triangle `tri` belongs to (cached)."""
        if self._face_of[tri] >= 0:
            return self._faces[self._face_of[tri]]
        n0, d0 = self.normal[tri], self.offset[tri]
        members, stack = [], [tri]
        self._face_of[tri] = len(self._faces)
        while stack:
            t = stack.pop()
            members.append(t)
            for e in self.eid[t]:
                for u in self.edge_tris(e):
                    if (self._face_of[u] < 0 and float(np.dot(self.normal[u], n0)) > _COPLANAR_COS
                            and abs(self.offset[u] - d0) < 10 * self.tol):
                        self._face_of[u] = len(self._faces)
                        stack.append(int(u))
        tris = np.array(members)
        w = self.area[tris]
        centres = self.v[tris].mean(axis=1)
        total = float(w.sum())
        centroid = (centres * w[:, None]).sum(axis=0) / total if total > 0 else centres.mean(axis=0)
        inside = set(tris.tolist())
        outline = []
        for t in tris:
            for k, e in enumerate(self.eid[t]):
                if not any(int(u) in inside for u in self.edge_tris(e) if u != t):
                    outline.append((self.v[t, k].copy(), self.v[t, (k + 1) % 3].copy()))
        face = Face(tris, n0.copy(), centroid, total, outline)
        self._faces.append(face)
        return face


# -- measurements ---------------------------------------------------------

def _fmt(x: float) -> str:
    t = f"{float(x):.4f}".rstrip("0").rstrip(".")
    return "0" if t in ("", "-0") else t


def _lit(p) -> str:
    return "[" + ", ".join(_fmt(float(c)) for c in p) + "]"


def _closest_on_segment(p, a, b):
    ab = b - a
    denom = float(np.dot(ab, ab))
    t = 0.0 if denom == 0 else min(1.0, max(0.0, float(np.dot(p - a, ab)) / denom))
    return a + t * ab


def _segment_segment(a0, a1, b0, b1):
    """Closest points between segments a0-a1 and b0-b1."""
    d1, d2, r = a1 - a0, b1 - b0, a0 - b0
    aa, ee, f = float(np.dot(d1, d1)), float(np.dot(d2, d2)), float(np.dot(d2, r))
    if aa == 0 and ee == 0:
        return a0, b0
    if aa == 0:
        return a0, _closest_on_segment(a0, b0, b1)
    c = float(np.dot(d1, r))
    if ee == 0:
        return _closest_on_segment(b0, a0, a1), b0
    bb = float(np.dot(d1, d2))
    denom = aa * ee - bb * bb
    s = min(1.0, max(0.0, (bb * f - c * ee) / denom)) if denom > 1e-18 * aa * ee else 0.0
    t = (bb * s + f) / ee
    if t < 0:
        t, s = 0.0, min(1.0, max(0.0, -c / aa))
    elif t > 1:
        t, s = 1.0, min(1.0, max(0.0, (bb - c) / aa))
    return a0 + s * d1, b0 + t * d2


def _angle(u, v) -> float:
    c = float(np.dot(_unit(u), _unit(v)))
    return float(np.degrees(np.arccos(max(-1.0, min(1.0, c)))))


def describe(f: Feature, n: int) -> str:
    """One selection, as the console reports it."""
    if f.kind == "point":
        return f"selection {n} point {_lit(f.point)} [{f.tag}]"
    if f.kind == "edge":
        return (f"selection {n} edge length {_fmt(float(np.linalg.norm(f.b - f.a)))} "
                f"from {_lit(f.a)} to {_lit(f.b)}")
    return (f"selection {n} face normal {_lit(f.face.normal)} area {_fmt(f.face.area)} "
            f"centroid {_lit(f.face.centroid)}")


def measure(f1: Feature, f2: Feature):
    """What two selections measure. Returns (lines, headline, p, q): the
    console lines, the label's text, and the two points its dimension line
    joins (p == q when there is no distance to draw)."""
    rank = {"point": 0, "edge": 1, "face": 2}
    x, y = (f1, f2) if rank[f1.kind] <= rank[f2.kind] else (f2, f1)
    nx, ny = (1, 2) if x is f1 else (2, 1)
    lines, head, p, q = [], "", x.point, y.point

    def dist_line(p, q, what):
        d = q - p
        return (f"{what}: {_fmt(float(np.linalg.norm(d)))}  "
                f"(dx {_fmt(d[0])}, dy {_fmt(d[1])}, dz {_fmt(d[2])}) from {_lit(p)} to {_lit(q)}")

    kinds = (x.kind, y.kind)
    if kinds == ("point", "point"):
        lines.append(dist_line(x.point, y.point, "distance"))
        head = _fmt(float(np.linalg.norm(y.point - x.point)))
    elif kinds == ("point", "edge"):
        q = _closest_on_segment(x.point, y.a, y.b)
        dseg = float(np.linalg.norm(q - p))
        u = _unit(y.b - y.a)
        r = x.point - y.a
        dline = float(np.linalg.norm(r - np.dot(r, u) * u))
        lines.append(f"minimum distance from point{nx} to edge{ny}: {_fmt(dseg)}")
        lines.append(f"distance from point{nx} to the line of edge{ny}: {_fmt(dline)}")
        head = _fmt(dseg)
    elif kinds == ("point", "face"):
        nrm = y.face.normal
        sd = float(np.dot(x.point - y.face.centroid, nrm))
        q = x.point - sd * nrm
        lines.append(f"distance from point{nx} to the plane of face{ny}: {_fmt(sd)}")
        head = _fmt(sd)
    elif kinds == ("edge", "edge"):
        p, q = _segment_segment(x.a, x.b, y.a, y.b)
        dmin = float(np.linalg.norm(q - p))
        ang = _angle(x.b - x.a, y.b - y.a)
        ang = min(ang, 180.0 - ang)
        lines.append(f"minimum distance from edge{nx} to edge{ny}: {_fmt(dmin)}")
        lines.append(f"angle from edge{nx} to edge{ny}: {_fmt(ang)}")
        head = f"{_fmt(dmin)}  {_fmt(ang)}°"
    elif kinds == ("edge", "face"):
        nrm = y.face.normal
        ang = 90.0 - _angle(x.b - x.a, nrm)
        ang = abs(ang)
        lines.append(f"angle from edge{nx} to the plane of face{ny}: {_fmt(ang)}")
        head = f"{_fmt(ang)}°"
        if ang < 1e-3:
            sd = float(np.dot(x.a - y.face.centroid, nrm))
            p, q = x.a, x.a - sd * nrm
            lines.append(f"distance from edge{nx} to the plane of face{ny}: {_fmt(sd)}")
            head = f"{_fmt(sd)}  {head}"
    else:
        n1, n2 = x.face.normal, y.face.normal
        ang = _angle(n1, n2)
        lines.append(f"angle between the normals of face{nx} and face{ny}: {_fmt(ang)}")
        head = f"{_fmt(ang)}°"
        if min(ang, 180.0 - ang) < 1e-3:
            sd = float(np.dot(x.face.centroid - y.face.centroid, n2))
            p, q = x.face.centroid, x.face.centroid - sd * n2
            lines.append(f"distance between the planes of face{nx} and face{ny}: {_fmt(abs(sd))}")
            head = f"{_fmt(abs(sd))}  {head}"
    return lines, head, np.asarray(p, dtype=np.float64), np.asarray(q, dtype=np.float64)
