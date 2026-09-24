"""Extrude gizmo: the source rewrite for a drag on a 2D shape."""
import pytest

from belfryscad.window.scad_format import extrude_edit


def edit(src, delta, centered=False, node="circle"):
    out = extrude_edit(src, src.index(node), delta, centered)
    return out[0] if out else None


@pytest.mark.parametrize("src, delta, centered, expected", [
    # a new extrusion wraps the node, outside its transform chain
    ("circle(5);", 10, False, "linear_extrude(height=10) circle(5);"),
    ("circle(5);", 10, True, "linear_extrude(height=10, center=true) circle(5);"),
    ("translate([1, 0]) circle(5);", 3, False,
     "linear_extrude(height=3) translate([1, 0]) circle(5);"),
    # an existing one is updated in place, keeping its other arguments
    ("linear_extrude(height=10, twist=90) circle(5);", 5, False,
     "linear_extrude(height=15, twist=90) circle(5);"),
    ("linear_extrude(10) circle(5);", -2.5, False, "linear_extrude(7.5) circle(5);"),
    ("linear_extrude(h + 1) circle(5);", 2, False, "linear_extrude(h + 3) circle(5);"),
    ("linear_extrude(height=10, center=true) circle(5);", 1, False,
     "linear_extrude(height=11, center=false) circle(5);"),
    ("linear_extrude(10, false, 4) circle(5);", 1, True,
     "linear_extrude(11, true, 4) circle(5);"),
    ("linear_extrude(twist=90) circle(5);", -50, False,
     "linear_extrude(height=50, twist=90) circle(5);"),       # default height 100
    # found through the node's own wrappers
    ("linear_extrude(4) color(\"red\") translate([1, 0]) circle(5);", 1, False,
     "linear_extrude(5) color(\"red\") translate([1, 0]) circle(5);"),
])
def test_extrude_edit(src, delta, centered, expected):
    assert edit(src, delta, centered) == expected


def test_selection_starting_at_the_extrude_itself():
    src = "linear_extrude(height=2) square(3);"
    assert extrude_edit(src, 0, 1, False)[0] == "linear_extrude(height=3) square(3);"


def test_no_edit_that_is_not_a_positive_height():
    assert edit("circle(5);", -3) is None                        # new, drag down
    assert edit("circle(5);", 0) is None
    assert edit("linear_extrude(height=2) circle(5);", -2) is None  # to zero


def test_node_start_is_where_the_edit_began():
    src = "cube(1);\ntranslate([1, 0]) circle(5);"
    assert extrude_edit(src, src.index("circle"), 2, False)[1] == src.index("translate")
