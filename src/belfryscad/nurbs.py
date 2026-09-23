"""NURBS curve evaluation and interpolation for the Path viewer.

Ported from BOSL2's `nurbs.scad` so the viewer draws exactly the curve a
script would get from `nurbs_curve()` / `nurbs_interp()` for the same
points, degree and closed flag. Only the subset the viewer can express is
ported: uniform weights, default knots, and unconstrained centripetal
interpolation. BOSL2 function names are given on each helper so a later
change upstream can be traced back here.

Qt-free, so it is testable without a widget.
"""

import numpy as np

_EPSILON = 1e-9     # BOSL2's approx() tolerance


def _extend_knot_vector(knots, length):
    """BOSL2 `_extend_knot_vector`: repeat the knot spacing periodically."""
    knots = list(knots)
    nxt = 0
    while len(knots) < length:
        knots.append(knots[-1] + knots[nxt + 1] - knots[nxt])
        nxt += 1
    return knots


def _basis(s, t, p, U):
    """The p+1 non-zero degree-p basis values at t in knot span s
    (Piegl & Tiller A2.2; BOSL2 `_deboor_to_degree`)."""
    N = [1.0] + [0.0] * p
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    for j in range(1, p + 1):
        left[j] = t - U[s + 1 - j]
        right[j] = U[s + j] - t
        saved = 0.0
        for r in range(j):
            tmp = N[r] / (right[r + 1] + left[j - r])
            N[r] = saved + right[r + 1] * tmp
            saved = left[j - r] * tmp
        N[j] = saved
    return N


def _span(t, U, lo, hi):
    """Knot span in [lo, hi] holding t, skipping zero-length spans so the
    domain's end value lands in the last real span."""
    s = int(np.searchsorted(U, t, side="right")) - 1
    s = min(max(s, lo), hi)
    while s > lo and U[s + 1] - U[s] <= _EPSILON:
        s -= 1
    return s


def curve(control, degree, closed=False, splinesteps=16, knots=None):
    """Sample a uniform-weight B-spline, as BOSL2's
    `nurbs_curve(control, degree, splinesteps, type=, knots=)` with
    type "closed" or "clamped". `knots` is BOSL2's short form (the
    `nurbs_interp` result's knot list); omitted means uniform.

    Returns an (m, dim) array. A closed curve does not repeat its start.
    """
    ctrl = np.asarray(control, dtype=float)
    p = degree
    n = len(ctrl)
    if not closed and n < p + 1:
        raise ValueError(f"clamped NURBS of degree {p} needs at least {p + 1} points")
    ctrl2 = np.concatenate([ctrl, ctrl[np.arange(p) % n]]) if closed else ctrl
    n2 = len(ctrl2)
    if knots is None:
        if closed:
            U = list(np.linspace(0.0, 1.0, n2 + p + 1))
        else:
            mult = [p + 1] + [1] * (n2 - p - 1) + [p + 1]
            U = [i / (len(mult) - 1) for i, m in enumerate(mult) for _ in range(m)]
    elif closed:
        U = _extend_knot_vector(knots, n2 + p + 1)
    else:
        U = [knots[0]] * p + list(knots) + [knots[-1]] * p
    ts = [t for i in range(p, n2) if U[i + 1] - U[i] > _EPSILON
          for t in np.linspace(U[i], U[i + 1], splinesteps, endpoint=False)]
    if not closed:
        ts.append(U[n2])
    out = np.empty((len(ts), ctrl.shape[1]))
    for k, t in enumerate(ts):
        s = _span(t, U, p, n2 - 1)
        out[k] = np.dot(_basis(s, t, p, U), ctrl2[s - p:s + 1])
    return out


def _interp_params(points, closed):
    """BOSL2 `_interp_params`, centripetal method only."""
    seg = np.diff(np.vstack([points, points[:1]]) if closed else points, axis=0)
    raw = np.linalg.norm(seg, axis=1)
    n = len(raw)
    if raw.sum() < 1e-10:
        return np.arange(n if closed else n + 1) / n
    if raw.min() <= 1e-10:
        raise ValueError("consecutive duplicate points")
    cs = np.cumsum(np.sqrt(raw))
    params = np.concatenate([[0.0], cs[:-1] / cs[-1]])
    return params if closed else np.append(params, 1.0)


def _fix_tiny_spans(bar, n, eps=1e-6):
    """BOSL2 `_fix_tiny_spans`: merge a near-empty span into a neighbour
    and bisect the merged span, keeping the knot count."""
    bar = list(bar)
    while True:
        spans = [bar[k + 1] - bar[k] for k in range(n)]
        if min(spans) >= eps * bar[n]:
            return bar
        k = int(np.argmin(spans))
        remove = 1 if k == 0 else n - 1 if k == n - 1 else k + 1
        merged = [b for i, b in enumerate(bar) if i != remove]
        absorb = 0 if k == 0 else k - 1
        mid = (merged[absorb] + merged[absorb + 1]) / 2
        bar = [x for i in range(n) for x in ([merged[i], mid] if i == absorb else [merged[i]])]


def _closed_system(points, p, rot):
    """Knots and collocation matrix for one seam rotation of a closed
    interpolation (BOSL2 `_closed_basic_solve`'s setup)."""
    n = len(points)
    pts = np.roll(points, -rot, axis=0)
    raw = _interp_params(pts, closed=True)
    avg = [sum(raw[(j + k) % n] + (j + k) // n for k in range(p)) / p for j in range(n + 1)]
    bar = _fix_tiny_spans([a - avg[0] for a in avg], n)
    U = _extend_knot_vector(bar, n + 2 * p + 1)
    params = raw + bar[p]
    return pts, bar, U, params


def _collisions(points, p, rot):
    """BOSL2 `_closed_rotation_collision_count`."""
    n = len(points)
    _, _, U, params = _closed_system(points, p, rot)
    return max(int(np.sum((params >= U[p + k]) & (params < U[p + k + 1]))) for k in range(n))


def interp(points, degree, closed=False):
    """Control points through which a degree-`degree` B-spline passes
    through `points`, as BOSL2's `nurbs_interp(points, degree,
    closed=closed)` with no constraints. Returns `(control, knots)` in the
    form `curve()` takes.

    Raises ValueError where BOSL2 would assert. One departure: a singular
    closed system raises instead of taking BOSL2's regularised fallback.
    """
    # ponytail: centripetal only, no deriv/curvature/corners/extra_pts --
    # the viewer has nowhere to enter them; port BOSL2's other solvers
    # when it does.
    pts = np.asarray(points, dtype=float)
    p = degree
    n = len(pts)
    if p < 2:
        # BOSL2's default smooth=3 asserts this even with nothing to smooth.
        raise ValueError("interpolation needs degree 2 or more")
    if n < p + 1:
        raise ValueError(f"degree {p} interpolation needs at least {p + 1} points")
    if not closed:
        N, knots = _clamped_system(_interp_params(pts, closed=False), p)
        return np.linalg.solve(N, pts), knots
    chords = np.linalg.norm(np.roll(pts, -1, axis=0) - pts, axis=1)
    ratios = np.roll(chords, -1) / np.maximum(chords, 1e-15)
    rot = (int(np.argmax(ratios)) + 1) % n
    if _collisions(pts, p, rot) > 1:
        rot = min(range(n), key=lambda r: _collisions(pts, p, r))
    rpts, bar, U, params = _closed_system(pts, p, rot)
    return np.linalg.solve(_periodic_matrix(params, p, U, n), rpts), bar


def _clamped_system(params, p):
    """Collocation matrix and short-form knots for a clamped fit through
    points at `params` (BOSL2 `_build_clamped_system`, extra_pts=0)."""
    n = len(params)
    interior = [float(np.mean(params[j:j + p])) for j in range(1, n - p)]
    U = [0.0] * (p + 1) + interior + [1.0] * (p + 1)
    N = np.zeros((n, n))
    for k, t in enumerate(params):
        s = _span(t, U, p, n - 1)
        N[k, s - p:s + 1] = _basis(s, t, p, U)
    return N, [0.0] + interior + [1.0]


def _periodic_matrix(params, p, U, n):
    """Periodic collocation: basis j >= n folds onto j - n
    (BOSL2 `_basis_row_periodic`)."""
    N = np.zeros((n, n))
    for k, t in enumerate(params):
        s = _span(t, U, p, n + p - 1)
        for j, b in zip(range(s - p, s + 1), _basis(s, t, p, U)):
            N[k, j % n] += b
    return N


def _closed_surface_system(params, p):
    """BOSL2 `_build_closed_system` (extra_pts=0), which the surface fit
    uses. Unlike the closed curve fit it searches no seam rotation, and it
    nudges any parameter that lands on a knot."""
    n = len(params)
    avg = [sum(params[(j + k) % n] + (j + k) // n for k in range(p)) / p for j in range(n + 1)]
    bar = _fix_tiny_spans([a - avg[0] for a in avg], n)
    U = _extend_knot_vector(bar, n + 2 * p + 1)
    eps = bar[n] / n * (0.01 if p == 2 else 1e-6)
    safe = [u + eps if min(abs(u - k) for k in U) < eps else u for u in np.asarray(params) + bar[p]]
    return _periodic_matrix(safe, p, U, n), bar


def patch(control, degree, closed=(False, False), splinesteps=16, knots=(None, None)):
    """Sample a uniform-weight NURBS surface, as BOSL2's
    `nurbs_patch_points(patch, degree, splinesteps, type=, knots=)`:
    a curve down every column (u, the first index), then along every row
    of the result (v). `degree`, `closed` and `knots` are (u, v) pairs.

    Returns an (m, k, dim) grid; a closed direction does not repeat its
    start. Leans on `curve()` being linear in its control points: a whole
    row of points is fitted as one long point.
    """
    P = np.asarray(control, dtype=float)
    rows, cols, dim = P.shape
    a = curve(P.reshape(rows, cols * dim), degree[0], closed[0], splinesteps, knots[0])
    m = len(a)
    a = a.reshape(m, cols, dim).transpose(1, 0, 2).reshape(cols, m * dim)
    b = curve(a, degree[1], closed[1], splinesteps, knots[1])
    return b.reshape(len(b), m, dim).transpose(1, 0, 2)


def interp_surface(points, degree, closed=(False, False)):
    """Control grid and (u, v) knots for a surface through every point of
    the rectangular grid `points`, as BOSL2's `nurbs_interp_surface(points,
    degree, row_wrap=, col_wrap=)` with no constraints; `closed` is
    (row_wrap, col_wrap). Returns what `patch()` takes.

    Parameters are each grid line's centripetal parameters averaged across
    the grid; every row is fitted in v, then every column of that in u.
    """
    # ponytail: centripetal and unconstrained only, as interp().
    P = np.asarray(points, dtype=float)
    rows, cols, dim = P.shape
    pu, pv = degree
    if rows < pu + 1 or cols < pv + 1:
        raise ValueError(f"degree {pu}x{pv} needs at least {pu + 1} rows and {pv + 1} columns")
    u = np.mean([_interp_params(P[:, c], closed[0]) for c in range(cols)], axis=0)
    v = np.mean([_interp_params(P[r], closed[1]) for r in range(rows)], axis=0)
    Nv, vk = (_closed_surface_system if closed[1] else _clamped_system)(v, pv)
    Nu, uk = (_closed_surface_system if closed[0] else _clamped_system)(u, pu)
    R = np.linalg.solve(Nv, P.transpose(1, 0, 2).reshape(cols, rows * dim))   # v fits, all rows at once
    R = R.reshape(cols, rows, dim).transpose(1, 0, 2).reshape(rows, cols * dim)
    ctrl = np.linalg.solve(Nu, R).reshape(rows, cols, dim)
    return ctrl, (uk, vk)
