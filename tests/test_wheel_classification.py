"""Wheel vs swipe is decided by scroll phase or a TouchPad device, never by
pixelDelta(): Qt fills that in for plain wheels on macOS and on X11 with
libinput hi-res wheels, which made them pan instead of zoom."""
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QInputDevice, QPointingDevice, QWheelEvent

from belfryscad.window.viewport import _is_swipe

_TOUCHPAD = QPointingDevice("trackpad or magic mouse", 9001, QInputDevice.DeviceType.TouchPad,
                            QPointingDevice.PointerType.Generic,
                            QInputDevice.Capability.Scroll, 1, 0)


def _event(pixel, phase=Qt.ScrollPhase.NoScrollPhase, device=None):
    args = [QPointF(10, 10), QPointF(10, 10), QPoint(*pixel), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, phase, False,
            Qt.MouseEventSource.MouseEventNotSynthesized]
    return QWheelEvent(*args, device) if device else QWheelEvent(*args)


def test_a_wheel_with_a_pixel_delta_is_still_a_wheel():
    # macOS line-based wheel (20 px per line) / X11 hi-res wheel.
    assert not _is_swipe(_event((0, 20)))
    assert not _is_swipe(_event((0, 0)))


def test_a_phased_scroll_is_a_swipe():
    # macOS trackpad and Magic Mouse; Wayland finger scrolling.
    for phase in (Qt.ScrollPhase.ScrollBegin, Qt.ScrollPhase.ScrollUpdate,
                  Qt.ScrollPhase.ScrollMomentum, Qt.ScrollPhase.ScrollEnd):
        assert _is_swipe(_event((0, 3), phase))


def test_a_touchpad_device_is_a_swipe_even_without_a_phase():
    # X11 reports no phases at all, but types the device.
    assert _is_swipe(_event((0, 0), device=_TOUCHPAD))
