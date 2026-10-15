"""A Color List double-click: the colour inserted keeps the shape of the vector it replaces."""
import pytest
from PySide6.QtGui import QColor

from belfryscad.window.color_literals import insertion_for, name_for


@pytest.mark.parametrize("selected, token, expected", [
    # nothing, other text or a string literal: the quoted token
    ("", "red", '"red"'),
    ("foo", "xkcd:dark mint", '"xkcd:dark mint"'),
    ('"blue"', "#0074c3", '"#0074c3"'),
    # a colour vector stays a vector, 0..1, alpha kept only if it had one
    ("[1, 0, 0]", "blue", "[0, 0, 1]"),
    ("[1,0,0]", "#808080", "[0.502, 0.502, 0.502]"),
    ("[1, 0, 0, 0.5]", "blue", "[0, 0, 1, 0.5]"),
    # a vector that is not a colour is just text
    ("[10, 20, 30]", "red", '"red"'),
])
def test_insertion_keeps_a_vectors_shape(selected, token, expected):
    assert insertion_for(selected, token) == expected


def test_name_for():
    assert name_for(QColor("#ff0000")) == "red"
    assert name_for(QColor("#123456")) is None


def test_hue_bands_run_dark_to_light():
    from belfryscad.window.color_names import _PICKABLE_NAMES as names
    # Within the red band, dark to light; a tinted white at its light end.
    assert names.index("maroon") < names.index("red") < names.index("snow")
    # Bands stay together: the reds finish before the oranges start.
    assert names.index("snow") < names.index("saddlebrown")
    # No grey group: a hueless grey sorts in the red band by lightness.
    assert names.index("black") < names.index("maroon") < names.index("white") < names.index("saddlebrown")
