"""Fixed Length for the path editor's two NURBS modes (#947).

What is held: in NURBS Path, each knot span's arc length (one piece of
curve per control point past the degree); in NURBS Interpolated, the arc
length between each two neighbouring points, which lie on the curve.

Neither curve has pieces that can be moved on their own, as a Bezier
segment can (a control point shapes degree+1 spans; interpolation solves
for every point at once), so a drag is one constrained solve: get the
dragged point to the cursor, or as near as the lengths allow, moving the
rest as little as possible, with every length as it was: Gauss-Newton,
each step a small equality-constrained least-squares solve. In NURBS Path
the curve is linear in its control points, so the length model is built
once; in NURBS Interpolated it is rebuilt every step, since the
interpolation's parameters move with the points.

2D and open curves only, as Fixed Length is elsewhere. Qt-free.
"""

import numpy as np

from belfryscad import nurbs

_GL_X, _GL_W = np.polynomial.legendre.leggauss(16)


def _deriv_basis(s, t, p, U):
    """The p+1 non-zero degree-p basis derivatives at t in knot span s."""
    lower = nurbs._basis(s, t, p - 1, U)        # N[s-p+1 .. s] of degree p-1
    out = np.zeros(p + 1)
    for r in range(p + 1):
        i = s - p + r
        if r >= 1 and U[i + p] - U[i] > 0:
            out[r] += p * lower[r - 1] / (U[i + p] - U[i])
        if r < p and U[i + p + 1] - U[i + 1] > 0:
            out[r] -= p * lower[r] / (U[i + p + 1] - U[i + 1])
    return out


def _eval(ctrl, p, U, ts):
    n = len(ctrl)
    out = np.empty((len(ts), ctrl.shape[1]))
    for k, t in enumerate(ts):
        s = nurbs._span(t, U, p, n - 1)
        out[k] = np.dot(nurbs._basis(s, t, p, U), ctrl[s - p:s + 1])
    return out


def _deriv_matrix(U, p, n, ts):
    """(len(ts), n): every basis function's derivative at each t, by
    Cox-de Boor on all of them at once. No t may sit on a knot."""
    U = np.asarray(U, dtype=float)
    ts = np.asarray(ts, dtype=float)[:, None]

    def ratio(num, den):
        return np.divide(num, den, out=np.zeros(np.broadcast_shapes(num.shape, den.shape)),
                         where=den > 0)
    N = ((U[:-1] <= ts) & (ts < U[1:])).astype(float)
    for q in range(1, p):
        k = len(U) - q - 1
        N = (ratio(ts - U[:k], U[q:q + k] - U[:k]) * N[:, :k]
             + ratio(U[q + 1:q + 1 + k] - ts, U[q + 1:q + 1 + k] - U[1:k + 1]) * N[:, 1:k + 1])
    # N is now degree p-1, one column per basis function, n + 1 of them.
    left = ratio(np.float64(p), U[p:p + n] - U[:n]) * N[:, :n]
    right = ratio(np.float64(p), U[p + 1:p + 1 + n] - U[1:n + 1]) * N[:, 1:n + 1]
    return left - right


def _rows(U, p, n, bounds):
    """(Q, w, which) with the arc length of the B-spline with control
    points C from bounds[j] to bounds[j+1] being the sum of w @ |Q @ C|
    over the rows with which == j: 16-point Gauss-Legendre on each knot
    span an interval crosses."""
    lo, hi, which = [], [], []
    for j, (a, b) in enumerate(zip(bounds, bounds[1:])):
        cuts = sorted({a, b} | {u for u in U if a < u < b})
        for c0, c1 in zip(cuts, cuts[1:]):
            if c1 - c0 > 1e-15:
                lo.append(c0)
                hi.append(c1)
                which.append(j)
    lo, hi = np.array(lo), np.array(hi)
    h = (hi - lo)[:, None] / 2
    ts = (lo[:, None] + (_GL_X + 1) * h).ravel()
    return (_deriv_matrix(U, p, n, ts), (_GL_W * h).ravel(),
            np.repeat(np.array(which), len(_GL_X)))


def _runs(pts, degree, corners):
    """NURBS Interpolated's pieces, as nurbs.interp_curve splits it at the
    corners: (first index, P, p, U, params) with the piece's control
    points P @ its points, and each point at its parameter."""
    n = len(pts)
    cuts = [0] + sorted(c for c in set(corners) if 0 < c < n - 1) + [n - 1]
    for a, b in zip(cuts, cuts[1:]):
        run = pts[a:b + 1]
        k = len(run)
        p = min(degree, k - 1)
        if p < 2:       # drawn as a polyline: a degree-1 spline through the points
            params = np.linspace(0.0, 1.0, k)
            yield a, np.eye(k), 1, [0.0] + list(params) + [1.0], params
        else:
            params = nurbs._interp_params(run, closed=False)
            N, knots = nurbs._clamped_system(params, p)
            yield a, np.linalg.inv(N), p, [0.0] * p + list(knots) + [1.0] * p, params


def _model(pts, mode, degree, corners):
    """(Q, w, seg, m): length j is the sum of w_r |Q_r @ points| over the
    rows r with seg[r] == j. Exact at `pts`. Raises ValueError where the
    curve can't be drawn."""
    n = len(pts)
    if mode == "nurbs":
        p = degree
        if n < p + 1:
            raise ValueError("too few points for the degree")
        mult = [p + 1] + [1] * (n - p - 1) + [p + 1]
        U = [i / (len(mult) - 1) for i, k in enumerate(mult) for _ in range(k)]
        Q, w, seg = _rows(U, p, n, U[p:n + 1])
        return Q, w, seg, n - p
    if n < 2:
        raise ValueError("too few points")
    blocks = []
    for a, P, p, U, params in _runs(pts, degree, corners):
        k = len(params)
        Q, w, which = _rows(U, p, k, list(params))
        full = np.zeros((len(Q), n))
        full[:, a:a + k] = Q @ P
        blocks.append((full, w, a + which))
    return (np.vstack([b[0] for b in blocks]), np.concatenate([b[1] for b in blocks]),
            np.concatenate([b[2] for b in blocks]), n - 1)


def _lengths(Q, w, seg, m, x):
    """The lengths, and each one's gradient on the points (m, n, 2)."""
    d = Q @ x
    sp = np.linalg.norm(d, axis=1)
    L = np.bincount(seg, w * sp, minlength=m)
    E = (w / np.maximum(sp, 1e-15))[:, None] * d
    J = np.zeros((m, x.shape[0], x.shape[1]))
    np.add.at(J, seg, Q[:, :, None] * E[:, None, :])
    return L, J


def arc_lengths(points, mode, degree, corners=()) -> np.ndarray:
    """The lengths Fixed Length holds (see the module docstring)."""
    x = np.asarray(points, dtype=float)[:, :2]
    return _lengths(*_model(x, mode, degree, corners), x)[0]


def rope_moves(points, mode, degree, corners, lengths, pins, dragged, target,
               together=None) -> list:
    """A Fixed Length drag of point `dragged` toward `target`: every
    length in `lengths` kept, point 0 and the pins never moving. With
    `together` = j, lengths j and j+1 need only keep their sum (a slide).
    Returns (index, point) moves, or [] for a move the solve can't make
    without stretching anything."""
    pts = np.asarray(points, dtype=float)
    n = len(pts)
    fixed = set(pins) | {0}
    if dragged in fixed or not 0 <= dragged < n:
        return []
    free = [i for i in range(n) if i not in fixed]
    x = pts[:, :2].copy()
    t = np.asarray(target, dtype=float)[:2]
    L0 = np.asarray(lengths, dtype=float)
    m = len(L0)
    S = np.eye(m)
    if together is not None:
        S[together, together + 1] = 1.0
        S = np.delete(S, together + 1, axis=0)
    c = len(S)
    others = [i for i in free if i != dragged]
    scale = float(np.mean(L0))
    model = None

    def measure(x, idx):
        nonlocal model
        if model is None or mode != "nurbs":    # interpolation's parameters move
            model = _model(x, mode, degree, corners)
        L, J = _lengths(*model, x)
        Jc = (S @ J.reshape(m, -1)).reshape(c, n, 2)[:, idx, :].reshape(c, -1)
        return S @ (L - L0), Jc

    def restore(x, idx):
        """Back onto exact lengths by the smallest change to points `idx`
        (Newton), or None if that doesn't converge."""
        # In NURBS Interpolated the Jacobian holds the parameters, so this
        # converges linearly there rather than quadratically: more passes,
        # but give up as soon as one fails to halve the error.
        last = np.inf
        for _ in range(40):
            C, Jc = measure(x, idx)
            err = float(np.abs(C).max())
            if err < 1e-11:
                return x
            if err > last / 2 or not np.isfinite(err):
                return None
            last = err
            x = x.copy()
            x[idx] -= np.linalg.lstsq(Jc, C, rcond=None)[0].reshape(-1, 2)
        return None

    try:
        # Walk the dragged point straight toward the cursor, the others
        # following by the least change; a step too far is halved.
        h = float(np.linalg.norm(t - x[dragged]))
        while np.linalg.norm(t - x[dragged]) > 1e-12 and h > 1e-3 * scale:
            gap = t - x[dragged]
            d = float(np.linalg.norm(gap))
            nxt = x.copy()
            nxt[dragged] = t if h >= d else x[dragged] + gap / d * h
            nxt = restore(nxt, others) if others else None
            if nxt is None:
                h /= 2
            else:
                x, h = nxt, h * 2
        # Blocked short of it (the span pulled taut): slide along what the
        # lengths allow, toward the cursor, in short steps.
        # ponytail: ~165 ms a move at 30 interpolated points, each step
        # re-interpolating; an exact parameter Jacobian would cut the
        # projection's passes if taut drags of long curves need it.
        if np.linalg.norm(t - x[dragged]) > 1e-9:
            di, k = free.index(dragged), 2 * len(free)
            wts = np.ones(k)
            cap = 0.2 * scale
            for _ in range(12):
                C, Jc = measure(x, free)
                b = np.zeros(k)
                b[2 * di:2 * di + 2] = t - x[dragged]
                K = np.block([[np.diag(wts), Jc.T], [Jc, np.zeros((c, c))]])
                step = np.linalg.lstsq(K, np.concatenate([wts * b, -C]), rcond=None)[0][:k].reshape(-1, 2)
                big = float(np.linalg.norm(step, axis=1).max())
                if big < 1e-9 * scale:
                    break
                if big > cap:
                    step *= cap / big
                nxt = x.copy()
                nxt[free] += step
                nxt = restore(nxt, free)
                if nxt is None or np.linalg.norm(t - nxt[dragged]) >= np.linalg.norm(t - x[dragged]):
                    cap /= 2
                    if cap < 1e-5 * scale:
                        break
                    continue
                gain = np.linalg.norm(t - x[dragged]) - np.linalg.norm(t - nxt[dragged])
                x = nxt
                if gain < 1e-6 * scale:
                    break
        L = _lengths(*_model(x, mode, degree, corners), x)[0]
    except (ValueError, np.linalg.LinAlgError):
        return []
    if not np.all(np.isfinite(x)) or np.abs(S @ (L - L0)).max() > 1e-7:
        return []
    out = []
    for i in free:
        if np.linalg.norm(x[i] - pts[i, :2]) > 1e-12:
            q = pts[i].copy()
            q[:2] = x[i]
            out.append((i, q))
    return out


def slide_target(points, degree, corners, v, cursor):
    """NURBS Interpolated's Option-drag: the point of the curve through
    `points`, between point v's neighbours, nearest `cursor` -- or None
    for a point that can't slide (an end or a corner)."""
    pts = np.asarray(points, dtype=float)[:, :2]
    cur = np.asarray(cursor, dtype=float)[:2]
    if v <= 0 or v >= len(pts) - 1 or v in set(corners):
        return None
    for a, P, p, U, params in _runs(pts, degree, corners):
        if a < v < a + len(params) - 1:
            i = v - a
            ts = np.linspace(params[i - 1], params[i + 1], 801)[1:-1]
            curve = _eval(P @ pts[a:a + len(params)], p, U, ts)
            return curve[int(np.argmin(np.linalg.norm(curve - cur, axis=1)))]
    return None
