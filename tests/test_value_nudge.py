"""Finding the number or expression under the cursor, for editor nudging.

Deliberately not transform-aware: what encloses the value does not matter,
which is why `cube(10)` and `$fn` work as well as `xrot(45)`.
"""
import pytest

from belfryscad.window.scad_format import find_value_span, nudge_component


def span(src, caret):
    r = find_value_span(src, caret)
    return src[r[0]:r[1]] if r else None


@pytest.mark.parametrize("src, caret, expected", [
    ("cube(10);", 6, "10"),                  # a builtin's argument
    ("xrot(45);", 6, "45"),                  # a BOSL2 scalar transform
    ("left(wall/2) cube(1);", 8, "wall/2"),  # an expression
    ("$fn = 64;", 7, "64"),                  # a special variable
    ("wall = 3;", 8, "3"),                   # a plain assignment
    ("cuboid(10, rounding = 2);", 8, "10"),
])
def test_the_value_under_the_cursor(src, caret, expected):
    assert span(src, caret) == expected


@pytest.mark.parametrize("caret, expected", [
    (11, "1"), (12, "1"),        # on the digit, and on the comma after it
    (14, "2"), (15, "2"),
    (17, "3"), (18, "3"),        # and on the closing bracket
])
def test_which_component_of_a_vector(caret, expected):
    assert span("translate([1, 2, 3]) cube(1);", caret) == expected


@pytest.mark.parametrize("caret", [12, 20, 22, 23])   # on the name, the =, the value
def test_a_named_argument_gives_its_value(caret):
    """Wherever in `rounding = 2` the caret lands, the nudgeable thing is the
    2 -- stepping the binding whole would write `rounding = 2 + 1`."""
    assert span("cuboid(10, rounding = 2);", caret) == "2"


def test_nothing_to_nudge():
    assert span("cube(10);", 0) is None          # on the identifier
    assert span("// just a comment", 5) is None


def test_a_nested_call_resolves_to_the_inner_argument():
    src = "translate([max(1, 2), 0, 0]) cube(1);"
    assert span(src, 16) == "1" or span(src, 16) == "max(1, 2)"


def test_the_finder_and_the_nudger_compose():
    """What the feature actually does, end to end, without any GUI."""
    src = "left(wall/2) cube(1);"
    a, b = find_value_span(src, 8)
    out = src[:a] + nudge_component(src[a:b], 1, "add") + src[b:]
    assert out == "left(wall/2 + 1) cube(1);"

    src2 = "cube(10);"
    a, b = find_value_span(src2, 6)
    out2 = src2[:a] + nudge_component(src2[a:b], 1, "add") + src2[b:]
    assert out2 == "cube(11);"


# -- Step sizing -----------------------------------------------------------

from belfryscad.window.scad_format import enclosing_call_name, is_angle_value


@pytest.mark.parametrize("src, caret, call", [
    ("xrot(45);", 6, "xrot"),
    ("cube(10);", 6, "cube"),
    ("rotate([0, 0, 45]) cube(1);", 13, "rotate"),   # walks out of the vector
    ("translate([1, 2, 3]) cube(1);", 12, "translate"),
])
def test_the_enclosing_call_is_found(src, caret, call):
    assert enclosing_call_name(src, caret) == call


@pytest.mark.parametrize("src, caret, angle", [
    ("xrot(45);", 6, True),
    ("zrot(a = 30);", 9, True),
    ("rotate([0, 0, 45]) cube(1);", 13, True),
    ("cube(10);", 6, False),
    ("translate([1, 2, 3]) cube(1);", 12, False),
    ("wall = 3;", 8, False),
])
def test_angles_are_recognised_for_step_sizing(src, caret, angle):
    assert is_angle_value(src, caret) is angle


def test_an_angle_steps_in_degrees_and_a_length_in_units():
    """The rule the user asked for: 90/15/1 for rotation, 10/1/0.1 for
    distance, with the same coarse/normal/fine relationship."""
    from belfryscad.window.viewport import ROTATION_NUDGE_STEPS
    assert [ROTATION_NUDGE_STEPS[m] for m in (10.0, 1.0, 0.1)] == [90.0, 15.0, 1.0]

    src = "xrot(45);"
    a, b = find_value_span(src, 6)
    assert is_angle_value(src, a)
    assert nudge_component(src[a:b], ROTATION_NUDGE_STEPS[1.0], "add") == "60"

    src2 = "cube(10);"
    a2, b2 = find_value_span(src2, 6)
    assert not is_angle_value(src2, a2)
    assert nudge_component(src2[a2:b2], 1.0, "add") == "11"


def test_the_editor_exposes_arming_and_a_signal():
    """The UI contract, without a widget: arm, disarm, and a signal the
    window applies as one undo step."""
    from belfryscad.window.editor import CodeEditor
    assert hasattr(CodeEditor, "arm_value_nudge")
    assert hasattr(CodeEditor, "disarm_value_nudge")
    assert hasattr(CodeEditor, "value_nudged")


def test_the_window_applies_a_nudge_and_debounces_the_render():
    import inspect
    from belfryscad.window.main_window import MainWindow
    src = inspect.getsource(MainWindow._on_value_nudged)
    assert "_undo_stack.push" in src, "one undo entry per step"
    assert "merge_id=1004" in src, "holding the key is one undo, not forty"
    assert "_value_nudge_timer.start" in src, "a render per keystroke is unusable"


# -- Named arguments -------------------------------------------------------

@pytest.mark.parametrize("src, sub, want", [
    ("rotate_extrude(angle=90) square(1);", "90", "90"),
    ("cyl(h=5, chamfang=30);", "30", "30"),
    ("zrot(a = 30);", "30", "30"),
    ("cube(size=10);", "10", "10"),
])
def test_a_named_argument_nudges_its_value_not_the_whole_pair(src, sub, want):
    """`angle=90` must step to `angle=105`, never grow an `angle=90 + 15`."""
    a, b = find_value_span(src, src.index(sub))
    assert src[a:b] == want


def test_a_comparison_is_not_a_named_argument():
    src = "f(a == b, 3);"
    a, b = find_value_span(src, src.index("== b") + 1)
    assert src[a:b] == "a == b"


@pytest.mark.parametrize("src, sub", [
    ("rotate_extrude(angle=90) square(1);", "90"),    # angle= on a non-rotation call
    ("cyl(h=5, chamfang=30);", "30"),
    ("linear_extrude(h=5, twist=90);", "90"),
])
def test_an_angle_argument_steps_in_degrees_whatever_the_call(src, sub):
    assert is_angle_value(src, src.index(sub))


@pytest.mark.parametrize("src, sub", [
    ("cube(size=10);", "10"),
    ("cyl(h=5, chamfang=30);", "5"),
])
def test_a_length_argument_does_not(src, sub):
    assert not is_angle_value(src, src.index(sub))


# -- The armed message -----------------------------------------------------

class _Armed:
    """Just enough of a CodeEditor to build the message -- a real widget in
    pytest takes the whole process down with it (see the project's Qt rule)."""
    def __init__(self, text, span):
        self.toPlainText, self._nudge_span = (lambda: text), span


def test_the_status_message_names_the_value_the_steps_and_the_way_out():
    """Arming is modal and marks nothing else in the window, so the message
    has to carry all three."""
    from belfryscad.window.editor import CodeEditor
    src = "cube(25);\nxrot(45);\n"

    msg = CodeEditor._nudge_status_text(_Armed(src, find_value_span(src, 6)))
    assert "25" in msg, msg                                   # what is armed
    assert "1" in msg and "10" in msg and "0.1" in msg, msg   # the steps
    assert "Esc" in msg, msg                                  # the way out

    ang = CodeEditor._nudge_status_text(_Armed(src, find_value_span(src, 15)))
    assert "90" in ang and "15" in ang, ang                   # degrees, not units
    assert "0.1" not in ang, ang


def test_the_message_is_raised_on_arm_and_cleared_on_disarm():
    import inspect
    from belfryscad.window.editor import CodeEditor
    assert "value_nudge_status" in inspect.getsource(CodeEditor.arm_value_nudge)
    assert 'value_nudge_status.emit("")' in inspect.getsource(
        CodeEditor.disarm_value_nudge), "the message must not outlive the arming"


def test_losing_focus_disarms():
    """Switching tabs must not leave the status bar promising arrow keys
    this editor no longer receives."""
    import inspect
    from belfryscad.window.editor import CodeEditor
    assert "disarm_value_nudge" in inspect.getsource(CodeEditor.focusOutEvent)


def test_the_window_shows_and_clears_it():
    import inspect
    from belfryscad.window.main_window import MainWindow
    src = inspect.getsource(MainWindow._on_value_nudge_status)
    assert "showMessage" in src and "clearMessage" in src
