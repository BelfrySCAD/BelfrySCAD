"""Hand memory the allocator is caching back to the operating system.

A render frees most of what it allocated, but the C library's allocator keeps
freed blocks for reuse instead of returning them, so one long process that
renders many scripts (a docs build, a GUI session) keeps some of its
high-water mark. `release_free_memory()` asks the allocator to give that back.

Its measured effect is modest. After the shapes3d docs it trimmed the
footprint by 2-24 MB (of ~390 MB). It did NOT reclaim the much larger cache of
freed large blocks that evaluator 1.35.0's list growth left behind (205 MB of
it stayed resident) -- that regression was fixed in the evaluator itself, by
not producing those blocks. Kept as a cheap bound on whatever else a long
session accumulates. Call it when a render is over, not during one: it walks
the allocator's free lists.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys

_release = None


def _resolve():
    """The platform's release call, or a no-op where there is none."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        fn = libc.malloc_zone_pressure_relief       # (zone, goal); NULL zone = all zones
        fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_size_t
        return lambda: fn(None, 0)
    if sys.platform.startswith("linux"):
        try:
            fn = ctypes.CDLL("libc.so.6").malloc_trim   # glibc only
        except (OSError, AttributeError):
            return lambda: 0
        fn.argtypes, fn.restype = [ctypes.c_size_t], ctypes.c_int
        return lambda: fn(0)
    return lambda: 0     # Windows' heap returns free memory on its own


def release_free_memory() -> int:
    """Return cached free memory to the OS; what the platform reports
    (bytes released on macOS, 1/0 on glibc, 0 where unsupported)."""
    global _release
    if _release is None:
        try:
            _release = _resolve()
        except (OSError, AttributeError):
            _release = lambda: 0
    return int(_release() or 0)
