"""Cursors that macOS 27.2 can draw (#703).

Qt has no native macOS cursor for some shapes (WaitCursor, SizeAllCursor)
and builds them from an image, as it does any QPixmap cursor. On macOS 27.2
that image-to-CGImage step crashes in CGColorSpaceGetType (seen with
PySide6 6.11.1), so the app died on every render's wait cursor. There,
those shapes become the nearest native one and pixmap cursors are skipped.

It is Qt's QTBUG-147602 (a dangling colour space in toCGImage), fixed in
Qt 6.11.2 and 6.12.0, so a fixed Qt keeps its own cursors.
"""

import functools
import platform
import sys

from PySide6.QtCore import Qt, qVersion

# ponytail: macOS gate from one report (27.2 crashes, 27.0.1 doesn't).
_BROKEN_FROM = (27, 2)
_QT_FIXED = (6, 11, 2)

_NATIVE = {
    Qt.CursorShape.WaitCursor: Qt.CursorShape.ArrowCursor,
    Qt.CursorShape.SizeAllCursor: Qt.CursorShape.OpenHandCursor,
}


@functools.cache
def image_cursors_broken() -> bool:
    """Whether a cursor built from an image would crash this macOS."""
    if sys.platform != "darwin" or _version(qVersion()) >= _QT_FIXED:
        return False
    try:
        return _version(platform.mac_ver()[0])[:2] >= _BROKEN_FROM
    except ValueError:
        return False


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def shape(s: Qt.CursorShape) -> Qt.CursorShape:
    """`s`, or a native stand-in where image cursors crash."""
    return _NATIVE.get(s, s) if image_cursors_broken() else s
