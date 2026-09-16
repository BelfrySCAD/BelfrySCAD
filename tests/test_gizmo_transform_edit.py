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


# -- Where a new wrapper goes (#453) ---------------------------------------

from belfryscad.window.scad_format import find_transform_chain


def _chain_names(src, child="cube("):
    return [n for n, _ in find_transform_chain(src, src.index(child))]


def test_the_chain_is_listed_innermost_first():
    assert _chain_names("translate([1,0,0]) rotate([0,0,45]) cube(10);") == \
        ["rotate", "translate"]


@pytest.mark.parametrize("src, expected", [
    # A loop is a wrapper too, but hoisting outside it would move every
    # iteration rather than the one the user grabbed.
    ("for (i=[0:3]) translate([i*5,0,0]) cube(1);", ["translate"]),
    ("difference() { translate([1,0,0]) cube(1); }", ["translate"]),
    ("if (x) translate([1,0,0]) cube(1);", ["translate"]),
    ("mymodule() translate([1,0,0]) cube(1);", ["translate"]),
])
def test_the_walk_stops_at_anything_that_is_not_a_transform(src, expected):
    assert _chain_names(src) == expected


def _apply(source, name="translate", kw="v", fill=0.0, delta=(1.0, 0.0, 0.0),
           child="cube("):
    """The insertion _commit_transform performs, without the GUI."""
    start = source.index(child)
    chain = find_transform_chain(source, start)
    outer_name, outer = chain[-1] if chain else (None, None)
    call = outer if outer_name == name else None
    vals = vector_arg(call, kw, 3, fill) if call else None
    base = vals if vals else [0.0, 0.0, 0.0]
    new = [base[i] + delta[i] for i in range(3)]
    text = f"{name}([{new[0]:.4g}, {new[1]:.4g}, {new[2]:.4g}]) "
    if vals is not None:
        return source[:call.start] + text.rstrip() + source[call.end:]
    at = outer.start if outer else start
    return source[:at] + text + source[at:]


def test_a_new_transform_goes_outside_an_existing_rotate():
    """The #453 case. Inside the rotate, a drag along the world-x handle
    moved the object along the ROTATED x."""
    out = _apply("rotate([0,0,45]) cube(10);")
    assert out == "translate([1, 0, 0]) rotate([0,0,45]) cube(10);"
    assert out.index("translate") < out.index("rotate")


def test_an_outermost_translate_is_updated_not_stacked():
    assert _apply("translate([1,0,0]) cube(10);") == "translate([2, 0, 0]) cube(10);"
    assert _apply("translate([1,0,0]) rotate([0,0,45]) cube(10);") == \
        "translate([2, 0, 0]) rotate([0,0,45]) cube(10);"


def test_an_inner_translate_is_left_alone():
    """Merging into it would move the object along the rotated axis, which
    is not where the handle pointed."""
    out = _apply("rotate([0,0,45]) translate([1,0,0]) cube(10);")
    assert out == "translate([1, 0, 0]) rotate([0,0,45]) translate([1,0,0]) cube(10);"


def test_nothing_is_hoisted_out_of_a_loop():
    out = _apply("for (i=[0:3]) translate([i*5,0,0]) cube(1);")
    assert out.startswith("for (i=[0:3]) "), "the loop still wraps the edit"


# -- Expressions keep their relationship (#459) ----------------------------

from belfryscad.window.scad_format import nudge_component, vector_texts


@pytest.mark.parametrize("text, amount, expected", [
    ("5", 1, "6"),
    ("1.5", 1, "2.5"),
    ("wall/2", 1, "wall/2 + 1"),       # the point: not overwritten with 7.5
    ("x", -2, "x - 2"),
    ("x", 0, "x"),                      # a zero nudge changes nothing
])
def test_add_keeps_an_expression(text, amount, expected):
    assert nudge_component(text, amount, "add") == expected


@pytest.mark.parametrize("text, amount, expected", [
    ("wall/2 + 1", 1, "wall/2 + 2"),   # folds instead of chaining + 1 + 1
    ("wall/2 + 2", -2, "wall/2"),      # ...and cancels back out entirely
    ("x - 1", -1, "x - 2"),
])
def test_a_repeated_nudge_folds_into_the_one_it_added(text, amount, expected):
    assert nudge_component(text, amount, "add") == expected


@pytest.mark.parametrize("text, factor, expected", [
    ("2", 2, "4"),
    ("w", 2, "w * 2"),
    ("w * 2", 2, "w * 4"),              # folds
    ("w", 1, "w"),                      # identity changes nothing
])
def test_scale_multiplies(text, factor, expected):
    assert nudge_component(text, factor, "mul") == expected


def test_scale_parenthesises_a_sum():
    """`w+1` scaled by 2 is (w+1)*2. Without the parens it silently becomes
    w + 2, because * binds tighter than +."""
    assert nudge_component("w+1", 2, "mul") == "(w+1) * 2"


def test_vector_texts_keeps_original_spelling_and_pads():
    call = _find("translate([wall/2, 1e3]) cube(1);")
    assert vector_texts(call, "v", 3, "0") == ["wall/2", "1e3", "0"]


def test_an_untouched_component_is_not_reformatted():
    """1e3 stays 1e3 when the drag did not move that axis -- the old code
    rewrote all three through f"{v:.4g}" and turned it into 1000."""
    call = _find("translate([0, 1e3, 0]) cube(1);")
    texts = vector_texts(call, "v", 3, "0")
    out = [nudge_component(t, a, "add") for t, a in zip(texts, (1, 0, 0))]
    assert out == ["1", "1e3", "0"]
