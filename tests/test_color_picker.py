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


def test_pale_tints_sort_with_the_neutrals_and_bands_run_dark_to_light():
    from belfryscad.window.color_picker import _PICKABLE_NAMES as names
    # `snow` is nominally hue 0; it belongs with the pastels, not the reds.
    assert names.index("hotpink") < names.index("snow") < names.index("black")
    # The tinted whites are pastels, after the colours and before the greys.
    pastels = names[names.index("snow"):names.index("black")]
    for w in ("snow", "seashell", "linen", "oldlace", "floralwhite", "ivory",
              "mintcream", "ghostwhite"):
        assert w in pastels, w
    for w in ("beige", "thistle", "mistyrose", "lightcyan", "lightyellow",
              "antiquewhite", "cornsilk"):
        assert w in pastels, w
    for w in ("lemonchiffon", "lightgoldenrodyellow", "papayawhip", "lightsteelblue"):
        assert w in pastels, w
    # Low chroma but dim: a colour, not a pastel.
    assert "rosybrown" not in pastels and "darkseagreen" not in pastels
    assert "whitesmoke" not in pastels
    # Greys are chroma 0 or named as one (the slate greys), dark to light.
    greys = names[names.index("black"):]
    assert greys == sorted(greys, key=lambda n: QColor(n).lightnessF())
    assert {n for n in names if "gray" in n or "grey" in n} <= set(greys)
    assert {"black", "white", "whitesmoke", "silver", "gainsboro"} <= set(greys)
    # Within the red band, dark to light.
    assert names.index("maroon") < names.index("red") < names.index("pink")
    # A band stays together: the oranges do not interleave with the reds.
    assert names.index("pink") < names.index("saddlebrown")
