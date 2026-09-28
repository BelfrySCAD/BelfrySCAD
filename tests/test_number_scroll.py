"""Alt/Option + wheel number stepping (number_scroll.step_number)."""
import pytest

from belfryscad.window.number_scroll import step_number


def at(text):
    """Split 'cube(1|0);' into ('cube(10);', 6): '|' marks the cursor."""
    col = text.index("|")
    return text.replace("|", ""), col


@pytest.mark.parametrize("before, delta, after", [
    ("cube(1|0);", 1, "cube(2|0);"),          # tens digit steps by 10
    ("cube(10|);", 1, "cube(11|);"),          # units digit
    ("cube(10|);", -1, "cube(9|);"),
    ("x = 1.2|5;", 1, "x = 1.3|5;"),          # tenths, decimals kept
    ("x = 0.0|5;", -1, "x = -0.0|5;"),         # crosses zero, keeps places
    ("x = 1.25|;", -1, "x = 1.24|;"),
    ("x = -3|;", 1, "x = -2|;"),              # unary minus is the number's
    ("x = -1|;", 2, "x = 1|;"),
    ("y = 2-3|;", 1, "y = 2-4|;"),            # binary minus is not
    ("y = a -3|;", 1, "y = a -4|;"),
    ("v = [1, -2|];", 1, "v = [1, -1|];"),    # after a comma it is a sign
    ("t = 09|;", 1, "t = 10|;"),
    ("t = 0|9;", 1, "t = 1|9;"),
    ("m = +5|;", 1, "m = +6|;"),
])
def test_steps_the_digit_left_of_the_cursor(before, delta, after):
    line, col = at(before)
    result = step_number(line, col, delta)
    assert result is not None
    new, newcol = result
    assert new[:newcol] + "|" + new[newcol:] == after


@pytest.mark.parametrize("before", [
    "cube(|10);",      # no digit to the left
    "x2| = 1;",        # part of a name
    "foo|(1);",        # not a number at all
    "x = -|3;",        # only the sign to the left
])
def test_no_number_to_step(before):
    assert step_number(*at(before), 1) is None
