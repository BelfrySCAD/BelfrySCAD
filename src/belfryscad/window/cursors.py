"""Cursors that macOS 27.2 can draw (#703).

Qt has no native macOS cursor for some shapes (WaitCursor, SizeAllCursor)
and builds them from an image, as it does any QPixmap cursor. On macOS 27.2
that image-to-CGImage step crashes in CGColorSpaceGetType (seen with
PySide6 6.11), so the app died on every render's wait cursor. There, those
shapes become the nearest native one and pixmap cursors are skipped.
"""

import functools
import platform
import sys

from PySide6.QtCore import Qt

# ponytail: version gate from one report (27.2 crashes, 27.0.1 doesn't);
# widen or drop it once Qt or macOS fixes the crash.
_BROKEN_FROM = (27, 2)

_NATIVE = {
    Qt.CursorShape.WaitCursor: Qt.CursorShape.ArrowCursor,
    Qt.CursorShape.SizeAllCursor: Qt.CursorShape.OpenHandCursor,
}


@functools.cache
def image_cursors_broken() -> bool:
    """Whether a cursor built from an image would crash this macOS."""
    if sys.platform != "darwin":
        return False
    try:
        version = tuple(int(p) for p in platform.mac_ver()[0].split(".")[:2])
    except ValueError:
        return False
    return version >= _BROKEN_FROM


def shape(s: Qt.CursorShape) -> Qt.CursorShape:
    """`s`, or a native stand-in where image cursors crash."""
    return _NATIVE.get(s, s) if image_cursors_broken() else s
