"""Choose Color...: the literal written back keeps the shape it was found in."""
import pytest
from PySide6.QtGui import QColor

from belfryscad.window.color_literals import name_for
from belfryscad.window.color_picker import literal_for


@pytest.mark.parametrize("original, color, spelling, expected", [
    # picked by name or typed hex: written exactly as chosen
    ('"red"', "blue", "blue", '"blue"'),
    ('"red"', "#ffffff", "#fff", '"#fff"'),               # short hex stays short
    ('"#ff0000"', "SteelBlue", "SteelBlue", '"SteelBlue"'),
    # native panel (no spelling): a name stays a name if the colour has one
    ('"red"', "#0000ff", None, '"blue"'),
    ('"red"', "#123456", None, '"#123456"'),
    ('"#ff0000"', "#0000ff", None, '"#0000ff"'),          # hex stays hex
    # a vector stays a vector, 0..1, alpha kept only if it had one
    ("[1, 0, 0]", "blue", "blue", "[0, 0, 1]"),
    ("[1,0,0]", "#808080", None, "[0.502, 0.502, 0.502]"),
])
def test_written_back_in_the_literals_own_shape(original, color, spelling, expected):
    assert literal_for(original, QColor(color), spelling) == expected


def test_a_four_element_vector_keeps_its_alpha():
    c = QColor("blue")
    c.setAlphaF(0.5)
    assert literal_for("[1, 0, 0, 0.5]", c) == "[0, 0, 1, 0.5]"
    assert literal_for("[1, 0, 0]", c) == "[0, 0, 1]"


def test_name_for():
    assert name_for(QColor("#ff0000")) == "red"
    assert name_for(QColor("#123456")) is None
