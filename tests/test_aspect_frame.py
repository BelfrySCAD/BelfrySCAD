"""View ▸ Aspect Ratio: the viewport size the window is resized to."""
from belfryscad.window.aspect_frame import target


def test_width_is_kept_and_height_fitted():
    assert target(800, 700, 16 / 9, 1000) == (800, 450)
    assert target(800, 300, 4 / 3, 1000) == (800, 600)


def test_too_tall_for_the_screen_fits_the_width_instead():
    assert target(1200, 500, 1.0, 900) == (500, 500)


def test_already_at_the_ratio_is_unchanged():
    assert target(640, 480, 4 / 3, 1000) == (640, 480)
