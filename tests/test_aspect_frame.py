"""View ▸ Aspect Ratio: the viewport size the window is resized to."""
from belfryscad.window.aspect_frame import target

BIG = 10_000


def test_a_wide_viewport_keeps_its_width():
    assert target(800, 700, 16 / 9, BIG, BIG) == (800, 450)
    assert target(800, 300, 4 / 3, BIG, BIG) == (800, 600)


def test_a_tall_viewport_keeps_its_height():
    assert target(400, 900, 16 / 9, BIG, BIG) == (1600, 900)
    assert target(600, 800, 1.0, BIG, BIG) == (800, 800)


def test_when_the_longer_side_wont_fit_the_shorter_is_kept():
    assert target(1200, 500, 1.0, BIG, 900) == (500, 500)      # 1200 tall won't fit
    assert target(400, 900, 2.0, 1500, BIG) == (400, 200)      # 1800 wide won't fit


def test_already_at_the_ratio_is_unchanged():
    assert target(640, 480, 4 / 3, BIG, BIG) == (640, 480)
    assert target(480, 640, 3 / 4, BIG, BIG) == (480, 640)
