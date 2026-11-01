"""Image-built cursors are avoided on macOS 27.2, where they crash (#703)."""
import platform
import sys

from PySide6.QtCore import Qt

from belfryscad.window import cursors


def gate(monkeypatch, plat, ver):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "mac_ver", lambda: (ver, ("", "", ""), ""))
    cursors.image_cursors_broken.cache_clear()
    try:
        return cursors.image_cursors_broken(), cursors.shape(Qt.CursorShape.WaitCursor)
    finally:
        cursors.image_cursors_broken.cache_clear()


def test_wait_cursor_is_native_from_27_2(monkeypatch):
    assert gate(monkeypatch, "darwin", "27.2") == (True, Qt.CursorShape.ArrowCursor)
    assert gate(monkeypatch, "darwin", "28.0.1") == (True, Qt.CursorShape.ArrowCursor)


def test_wait_cursor_is_kept_elsewhere(monkeypatch):
    for plat, ver in (("darwin", "27.0.1"), ("darwin", "15.5"), ("darwin", ""), ("linux", "")):
        assert gate(monkeypatch, plat, ver) == (False, Qt.CursorShape.WaitCursor)
