"""#788: placing a background image under the 2D path and region editors."""
import pytest

from belfryscad.window.background_image import BackgroundImage


def test_anchor_names_the_point_at_x_y():
    bg = BackgroundImage(path="i.png", aspect=0.5, width=40, x=0, y=0, anchor=(0, 0))
    assert bg.center() == pytest.approx((20, 10))
    bg.anchor = (1, 1)
    assert bg.center() == pytest.approx((-20, -10))


def test_rotation_turns_about_the_anchor():
    bg = BackgroundImage(path="i.png", aspect=0.5, width=40, anchor=(0, 0), rotation=90)
    assert bg.center() == pytest.approx((-10, 20))


def test_fit_centres_inside_the_bounds_at_any_rotation():
    bg = BackgroundImage(path="i.png", aspect=0.5, anchor=(0, 1))
    bg.fit((0, 0, 40, 40))
    assert bg.width == pytest.approx(40) and bg.center() == pytest.approx((20, 20))
    bg.rotation = 90
    bg.fit((0, 0, 40, 40))
    assert bg.width == pytest.approx(40) and bg.center() == pytest.approx((20, 20))
    bg.rotation = 0
    bg.fit((0, 0, 100, 10))     # height-limited
    assert bg.width == pytest.approx(20)


def test_two_point_scale_keeps_the_first_point_still():
    bg = BackgroundImage(path="i.png", aspect=1, width=10, x=5, y=5, anchor=(0.5, 0.5))
    bg.scale_about(0, 0, 2)
    assert bg.width == 20 and bg.center() == pytest.approx((10, 10))


def _image():
    return BackgroundImage(path="i.png", aspect=0.5, width=40, x=0, y=0, anchor=(0, 0))


def test_dragging_a_handle_resizes_about_the_anchor():
    from dataclasses import replace
    from belfryscad.window.background_image import drag_image
    bg = _image()
    start = replace(bg)
    # The top-right handle, at (40, 20), dragged to (80, 40): twice the size.
    drag_image(bg, "resize", (1, 1), start, (40, 20), (80, 40))
    assert bg.width == pytest.approx(80) and (bg.x, bg.y) == (0, 0)
    # The right-middle handle pulled straight up does not change the size.
    bg = _image()
    drag_image(bg, "resize", (1, 0.5), replace(bg), (40, 10), (40, 30))
    assert bg.width == pytest.approx(40 * (40 * 40 + 10 * 30) / (40 * 40 + 10 * 10))


def test_option_dragging_a_handle_rotates_about_the_anchor():
    from dataclasses import replace
    from belfryscad.window.background_image import drag_image
    bg = _image()
    drag_image(bg, "rotate", (1, 0), replace(bg), (40, 0), (0, 40))
    assert bg.rotation == pytest.approx(90) and (bg.x, bg.y) == (0, 0)
    assert bg.point_at(1, 0) == pytest.approx((0, 40))


def test_dragging_the_image_moves_it():
    from dataclasses import replace
    from belfryscad.window.background_image import drag_image
    bg = _image()
    drag_image(bg, "move", None, replace(bg), (10, 10), (13, 6))
    assert (bg.x, bg.y, bg.width) == (3, -4, 40)


def test_size_dialog_links_width_and_height():
    from belfryscad.window.background_image import ImageSizeDialog
    d = ImageSizeDialog(40, 0.5)
    assert d._h.value() == pytest.approx(20)
    d._h.setValue(30)
    assert d.image_width() == pytest.approx(60)
    d._w.setValue(10)
    assert d._h.value() == pytest.approx(5)
