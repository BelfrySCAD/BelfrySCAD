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


# Fuller behaviour table. "[corrected]" rows keep a digit left of the cursor
# when the new magnitude is shorter than the digits right of it; stepping
# used to leave the cursor at the number's start, where it could not step
# back (e.g. 1|00 - 1 gave |00).
@pytest.mark.parametrize("before, delta, after", [
    # integers, carry/borrow, width changes
    ("9|", 1, "10|"), ("99|", 1, "100|"), ("9|9", 1, "10|9"),
    ("10|", -1, "9|"), ("100|", -1, "99|"), ("12|34", -1, "11|34"),
    ("12|34", 1, "13|34"), ("x = 1|23;", 5, "x = 6|23;"),
    ("1|", 10, "11|"), ("1|", -10, "-9|"), ("5|", 123, "128|"),
    ("1|00", -1, "0|00"),                       # [corrected]
    ("1|", -1, "0|"), ("0|", -1, "-1|"), ("0|", -10, "-10|"), ("1|", -2, "-1|"),
    ("x = 1|5;", -2, "x = -0|5;"),              # [corrected]
    # signs
    ("-1|", 1, "0|"), ("-1|", -1, "-2|"), ("-10|", 1, "-9|"),
    ("-1|0", 1, "0|0"),                         # [corrected]
    ("-1|0", 2, "1|0"), ("-0|", 1, "1|"), ("-0|", -1, "-1|"),
    ("+1|", -1, "+0|"), ("+1|", -2, "-1|"), ("+0|", 1, "+1|"),
    ("+0|", -1, "-1|"), ("+0.0|", -1, "-0.1|"),
    ("x=-3|", 1, "x=-2|"), ("x=\t-3|", 1, "x=\t-2|"), ("f(-3|)", 1, "f(-2|)"),
    ("[+3|]", 1, "[+4|]"), ("a*-3|", 1, "a*-2|"), ("a- 3|", 1, "a- 4|"),
    ("a+3|", 1, "a+4|"), ("1 -3|", 1, "1 -4|"), ("2 - 3|", 1, "2 - 4|"),
    ("x[0]-3|", 1, "x[0]-4|"), ("f()-3|", 1, "f()-4|"), ("2-3|", -5, "2--2|"),
    ("--3|", 1, "--2|"), ("+-3|", 1, "+-2|"), ("x--3|", 1, "x--2|"),
    # decimals
    ("1.5|", -2, "1.3|"), ("1.|5", 1, "2.|5"), ("1|.5", 1, "2|.5"),
    ("1|.5", -2, "-0|.5"), (".5|", 1, "0.6|"), (".|5", 1, "1.|5"),
    ("0.05|", -1, "0.04|"), ("0.0|0", -1, "-0.1|0"), ("1.0|0", -1, "0.9|0"),
    ("1|.00", -2, "-1|.00"), ("9.9|9", 1, "10.0|9"), ("-0.0|5", 1, "0.0|5"),
    ("-0.05|", 10, "0.05|"), ("x = 0.1|;", -1, "x = 0.0|;"),
    ("x = -0.1|;", 1, "x = 0.0|;"), ("10.0|", -100, "0.0|"),
    ("-.|5", 1, "0.|5"), ("-.5|", 1, "-0.4|"),
    ("1|2.5", -2, "-0|7.5"),                    # [corrected]
    ("1|0.25", -2, "-0|9.75"),                  # [corrected]
    # leading zeros
    ("007|", 1, "8|"), ("007|", -8, "-1|"), ("00|7", 1, "1|7"),
    ("00|", -1, "-1|"), ("0|07", 1, "1|07"),
    ("0|07", -1, "-0|93"),                      # [corrected]
    # context
    ("a=1;b=2|;", 1, "a=1;b=3|;"), ("10 2|0", 1, "10 3|0"),
    ("$fn=3|2", 1, "$fn=4|2"), ("x = 1;  // 1|0", 1, "x = 1;  // 2|0"),
    ("1|e5", 1, "2|e5"), ("1.2.3|", 1, "1.2.4|"),
    # delta 0 normalises
    ("1.5|", 0, "1.5|"), (".5|", 0, "0.5|"), ("007|", 0, "7|"), ("-0|", 0, "0|"),
    ("0|07", 0, "0|07"),                        # [corrected]
])
def test_behaviour_table(before, delta, after):
    new, newcol = step_number(*at(before), delta)
    assert new[:newcol] + "|" + new[newcol:] == after


@pytest.mark.parametrize("before", [
    "10 |20", "x = 1 |;", "|", "abc|", "-|", "+|", ".|", "1.|", "12.|",
    "|.5", "-|.5", "-|0", "1.2|.3", "1|.2.3", "1e5|", "1e|5", "a_1|", "_1|",
    "ab1|2", "é1|",
])
def test_more_places_with_nothing_to_step(before):
    assert step_number(*at(before), 1) is None
