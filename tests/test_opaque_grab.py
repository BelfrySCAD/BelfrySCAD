"""A grabbed viewport frame is opaque with its RGB untouched (#921): the
background clears to alpha 0, so where a grab keeps alpha the saved
pictures had a transparent background."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402

from belfryscad.window.viewport import opaque_image  # noqa: E402


def test_alpha_is_dropped_and_rgb_kept():
    for fmt in (QImage.Format.Format_ARGB32_Premultiplied, QImage.Format.Format_ARGB32):
        img = QImage(2, 1, fmt)
        img.setPixel(0, 0, 0x00C86432)    # background: RGB right, alpha 0
        img.setPixel(1, 0, 0x80406080)    # blended: partial alpha
        out = opaque_image(img)
        assert not out.hasAlphaChannel()
        a, b = out.pixelColor(0, 0), out.pixelColor(1, 0)
        assert (a.red(), a.green(), a.blue(), a.alpha()) == (200, 100, 50, 255)
        assert (b.red(), b.green(), b.blue(), b.alpha()) == (64, 96, 128, 255)


def test_an_opaque_grab_is_left_alone():
    img = QImage(1, 1, QImage.Format.Format_RGB32)
    img.setPixel(0, 0, 0xFF102030)
    assert opaque_image(img).pixel(0, 0) == 0xFF102030
