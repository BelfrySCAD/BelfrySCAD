"""Finding and updating the transform wrapper a gizmo drag edits.

The wrapper used to be located with a regex anchored to the node, which
matched only a literal three-element `translate([a, b, c])`. Anything else
-- a named argument, a two-element vector, a comment in between, a nested
call -- missed, and the drag inserted a SECOND wrapper instead of updating
the one already there (#452).
"""
import pytest

from belfryscad.window.scad_format import find_transform_call, vector_arg


def _find(src, name="translate", child="cube("):
    return find_transform_call(src, src.index(child), name)


@pytest.mark.parametrize("src, expected", [
    ("translate([1, 2, 3]) cube(1);", [1.0, 2.0, 3.0]),
    ("translate([1,2,3]) cube(1);", [1.0, 2.0, 3.0]),
    # Every one of these missed before, and got a second wrapper.
    ("translate(v=[1, 2, 3]) cube(1);", [1.0, 2.0, 3.0]),
    ("translate([1, 2]) cube(1);", [1.0, 2.0, 0.0]),
    ("translate([1,2,3]) /* note */ cube(1);", [1.0, 2.0, 3.0]),
    ("translate([1,2,3]) // note\n  cube(1);", [1.0, 2.0, 3.0]),
    ("translate( [ 1 , 2 , 3 ] )   cube(1);", [1.0, 2.0, 3.0]),
])
def test_wrappers_that_can_be_updated(src, expected):
    call = _find(src)
    assert call is not None, "wrapper not found"
    assert vector_arg(call, "v", 3, 0.0) == expected


@pytest.mark.parametrize("src", [
    "cube(1);",                            # nothing in front
    "mytranslate([1,2,3]) cube(1);",       # a longer identifier
    "rotate([0,0,45]) cube(1);",           # a different transform
])
def test_not_a_translate_wrapper(src):
    assert _find(src) is None


@pytest.mark.parametrize("src", [
    "translate([x, 0, 0]) cube(1);",       # a variable
    "translate([1+1, 0, 0]) cube(1);",     # an expression
    "translate([1,2,3,4]) cube(1);",       # too many components
])
def test_found_but_not_safely_rewritable(src):
    """The call is there, but its vector is not ours to rewrite: a drag
    must wrap rather than silently discard an expression."""
    call = _find(src)
    assert call is not None
    assert vector_arg(call, "v", 3, 0.0) is None


def test_scale_pads_with_one_not_zero():
    """`scale([2,2])` means z=1. Padding with 0 would flatten the model."""
    call = _find("scale([2,2]) cube(1);", name="scale")
    assert vector_arg(call, "v", 3, 1.0) == [2.0, 2.0, 1.0]


def test_rotate_reads_its_own_keyword():
    call = _find("rotate(a=[0,0,45]) cube(1);", name="rotate")
    assert vector_arg(call, "a", 3, 0.0) == [0.0, 0.0, 45.0]


def test_a_string_argument_does_not_confuse_the_paren_match():
    """`)` inside a string is not the end of the call -- which is exactly
    what a backwards regex scan cannot know."""
    src = 'translate([f("a)b"), 0, 0]) cube(1);'
    call = _find(src)
    assert call is not None
    assert call.start == 0
    assert vector_arg(call, "v", 3, 0.0) is None    # an expression, so not rewritten


def test_nested_calls_match_the_right_paren():
    src = "translate([max(1, 2), 0, 0]) cube(1);"
    call = _find(src)
    assert call is not None and call.start == 0
    assert src[call.end:].startswith(" cube(1);")


def test_the_span_covers_exactly_the_call():
    src = "  translate([1,2,3])  cube(1);"
    call = _find(src)
    assert src[call.start:call.end] == "translate([1,2,3])"
