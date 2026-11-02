"""View ▸ Aspect Ratio: the viewport size the window is resized to."""
from belfryscad.window.aspect_frame import target

BIG = 10_000


def test_a_wide_viewport_keeps_its_width():
    assert target(800, 700, 16 / 9, BIG, BIG) == (800, 450)
    assert target(800, 300, 4 / 3, BIG, BIG) == (800, 600)


def test_a_tall_viewport_keeps_its_height():
    assert target(400, 900, 16 / 9, BIG, BIG) == (1600, 900)
    assert target(600, 800, 1.0, BIG, BIG) == (800, 800)


def test_too_big_for_the_screen_shrinks_keeping_the_ratio():
    assert target(1200, 500, 1.0, BIG, 900) == (900, 900)      # 1200 tall won't fit
    assert target(400, 900, 2.0, 1500, BIG) == (1500, 750)     # 1800 wide won't fit
    assert target(1600, 900, 4 / 3, 1400, 800) == (1067, 800)  # both limits; height binds


def test_already_at_the_ratio_is_unchanged():
    assert target(640, 480, 4 / 3, BIG, BIG) == (640, 480)
    assert target(480, 640, 3 / 4, BIG, BIG) == (480, 640)
