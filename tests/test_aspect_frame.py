"""View ▸ Aspect Ratio: the largest rectangle of the ratio, centred."""
from belfryscad.window.aspect_frame import fit


def test_fill_window_is_the_whole_space():
    assert fit(800, 600, None) == (0, 0, 800, 600)


def test_wide_space_gets_bars_at_the_sides():
    assert fit(1000, 300, 2.0) == (200, 0, 600, 300)


def test_tall_space_gets_bars_top_and_bottom():
    assert fit(640, 653, 16 / 9) == (0, 146, 640, 360)


def test_an_empty_space_stays_empty():
    assert fit(0, 0, 4 / 3) == (0, 0, 0, 0)
