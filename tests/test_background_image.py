"""#788: placing a background image under the 2D path and region editors."""
import pytest

from belfryscad.window.background_image import BackgroundImage


def test_anchor_names_the_point_at_x_y():
    bg = BackgroundImage(path="i.png", aspect=0.5, width=40, x=0, y=0, anchor="Bottom Left")
    assert bg.center() == pytest.approx((20, 10))
    bg.anchor = "Top Right"
    assert bg.center() == pytest.approx((-20, -10))


def test_rotation_turns_about_the_anchor():
    bg = BackgroundImage(path="i.png", aspect=0.5, width=40, anchor="Bottom Left", rotation=90)
    assert bg.center() == pytest.approx((-10, 20))


def test_fit_centres_inside_the_bounds_at_any_rotation():
    bg = BackgroundImage(path="i.png", aspect=0.5, anchor="Top Left")
    bg.fit((0, 0, 40, 40))
    assert bg.width == pytest.approx(40) and bg.center() == pytest.approx((20, 20))
    bg.rotation = 90
    bg.fit((0, 0, 40, 40))
    assert bg.width == pytest.approx(40) and bg.center() == pytest.approx((20, 20))
    bg.rotation = 0
    bg.fit((0, 0, 100, 10))     # height-limited
    assert bg.width == pytest.approx(20)


def test_two_point_scale_keeps_the_first_point_still():
    bg = BackgroundImage(path="i.png", aspect=1, width=10, x=5, y=5, anchor="Center")
    bg.scale_about(0, 0, 2)
    assert bg.width == 20 and bg.center() == pytest.approx((10, 10))
