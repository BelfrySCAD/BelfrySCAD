"""Which source span a picked geometry id resolves to.

An originalID names the node that PRODUCED the geometry, which for anything
a library builds is a node inside that library -- including a plain
`cube(10)` once BOSL2 is included, since BOSL2 overrides the primitives with
its own modules. Those nodes carry byte offsets into the library file, and
the gizmo commits splice at that offset in the user's buffer, so an offset
past the end appended a stray transform to it (#450).

The answer for those is the node's `call_site`: the call in the user's own
file that reached the geometry (#451).
"""
from types import SimpleNamespace

from belfryscad.window.main_window import MainWindow


def _pos(origin, start=0, end=5):
    return SimpleNamespace(origin=origin, start_offset=start, end_offset=end)


def _tab(file_path=None, parse_path=None):
    return SimpleNamespace(file_path=file_path, _last_parse_path=parse_path)


def _mw(id_to_node, tab):
    return SimpleNamespace(
        id_to_node=id_to_node,
        _rendered_tab=tab,
        _tab_owns_origin=lambda t, origin: MainWindow._tab_owns_origin(None, t, origin),
    )


def test_tab_owns_its_own_file_and_the_temp_copy_that_was_parsed(tmp_path):
    real = tmp_path / "part.scad"
    real.write_text("cube(1);\n")
    tmp = tmp_path / "part.parsed.scad"
    tmp.write_text("cube(1);\n")
    tab = _tab(str(real), str(tmp))

    own = MainWindow._tab_owns_origin
    assert own(None, tab, str(real))
    assert own(None, tab, str(tmp))         # what the worker actually parsed
    assert not own(None, tab, str(tmp_path / "BOSL2" / "vnf.scad"))
    assert not own(None, tab, None)
    assert not own(None, tab, "")


def test_a_node_the_user_wrote_resolves_to_itself(tmp_path):
    script = tmp_path / "s.scad"
    script.write_text("cube(10);\n")
    mw = _mw({1: SimpleNamespace(position=_pos(str(script), 0, 9), call_site=None)},
             _tab(str(script), str(script)))
    span = MainWindow._editable_span_for_id(mw, 1)
    assert span is not None and (span.start_offset, span.end_offset) == (0, 9)


def test_a_library_node_resolves_to_the_users_call_site(tmp_path):
    """The #451 case. Produced deep in BOSL2, attributed to the user's line."""
    script = tmp_path / "s.scad"
    src = "include <BOSL2/std.scad>\ncuboid(10, rounding=2);\n"
    script.write_text(src)
    lib = tmp_path / "BOSL2" / "vnf.scad"
    lib.parent.mkdir()
    lib.write_text("// stand-in\n")

    call = _pos(str(script), 25, 47)
    node = SimpleNamespace(position=_pos(str(lib), 85256, 85304), call_site=call)
    mw = _mw({5: node}, _tab(str(script), str(script)))

    span = MainWindow._editable_span_for_id(mw, 5)
    assert span is call, "must be the user's call, not the producing node"
    assert src[span.start_offset:span.end_offset] == "cuboid(10, rounding=2)"


def test_a_library_node_with_no_reachable_call_site_is_refused(tmp_path):
    """Top-level geometry in a `use`d file: nothing in this buffer to point
    at, and guessing would edit somewhere the user cannot see."""
    script = tmp_path / "s.scad"
    script.write_text("use <other.scad>\n")
    lib = tmp_path / "other.scad"
    lib.write_text("cube(1);\n")
    mw = _mw({9: SimpleNamespace(position=_pos(str(lib), 0, 8), call_site=None)},
             _tab(str(script), str(script)))
    assert MainWindow._editable_span_for_id(mw, 9) is None

    # A call site that is itself inside a library is no better.
    other = _pos(str(lib), 0, 8)
    mw2 = _mw({9: SimpleNamespace(position=_pos(str(lib), 0, 8), call_site=other)},
              _tab(str(script), str(script)))
    assert MainWindow._editable_span_for_id(mw2, 9) is None
    assert MainWindow._editable_span_for_id(mw2, 99) is None


def test_an_evaluator_without_call_site_still_refuses(tmp_path):
    """The pin is still on an evaluator that predates call_site; there the
    old behaviour -- refuse rather than corrupt -- must survive."""
    script = tmp_path / "s.scad"
    script.write_text("include <BOSL2/std.scad>\ncube(10);\n")
    lib = tmp_path / "builtins.scad"
    lib.write_text("x")

    class OldNode:            # no call_site attribute at all
        def __init__(self, position):
            self.position = position

    mw = _mw({2: OldNode(_pos(str(lib), 1110, 1135))}, _tab(str(script), str(script)))
    assert MainWindow._editable_span_for_id(mw, 2) is None


def test_the_offsets_that_would_corrupt_are_never_returned(tmp_path):
    """Guard the specific failure: a library offset past the end of the
    script, where a splice lands at the end of the file and wraps nothing."""
    script = tmp_path / "s.scad"
    source = "include <BOSL2/std.scad>\ncuboid(10);\n"
    script.write_text(source)
    lib = tmp_path / "vnf.scad"
    lib.write_text("x")

    node = SimpleNamespace(position=_pos(str(lib), 85256, 85304),
                           call_site=_pos(str(script), 25, 36))
    mw = _mw({7: node}, _tab(str(script), str(script)))

    span = MainWindow._editable_span_for_id(mw, 7)
    assert span.start_offset < len(source), "the span must be inside this buffer"

    # What the producing node's offset would have done, for the record.
    bad = node.position.start_offset
    assert bad > len(source)
    corrupted = source[:bad] + "translate([1, 0, 0]) " + source[bad:]
    assert corrupted.endswith("translate([1, 0, 0]) "), "a transform wrapping nothing"
