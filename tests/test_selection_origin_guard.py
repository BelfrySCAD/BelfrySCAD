"""Picking geometry a library built must not edit the user's file.

An originalID maps to the AST node that PRODUCED the geometry, which for
anything a library builds is a node inside that library -- including plain
`cube(10)` once BOSL2 is included, since BOSL2 overrides the primitives
with its own modules. Those nodes carry byte offsets into the library
file, and the gizmo commits splice at `node.position.start_offset`, so an
offset past the end of the user's buffer appended a stray transform to it.
"""
from types import SimpleNamespace

from belfryscad.window.main_window import MainWindow


def _pos(origin, start=0, end=5):
    return SimpleNamespace(origin=origin, start_offset=start, end_offset=end)


def _tab(file_path=None, parse_path=None):
    return SimpleNamespace(file_path=file_path, _last_parse_path=parse_path)


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


def test_a_library_node_is_not_editable(tmp_path):
    """The reported case: a 59-character script whose geometry all came from
    BOSL2, with node offsets in the tens of thousands."""
    script = tmp_path / "s.scad"
    script.write_text("include <BOSL2/std.scad>\ncube(10);\ncuboid(10);\n")
    lib = tmp_path / "BOSL2" / "builtins.scad"
    lib.parent.mkdir()
    lib.write_text("// stand-in\n")

    mw = SimpleNamespace(
        id_to_node={
            1: SimpleNamespace(position=_pos(str(script), 25, 34)),
            2: SimpleNamespace(position=_pos(str(lib), 1110, 1135)),
        },
        _rendered_tab=_tab(str(script), str(script)),
        _tab_owns_origin=lambda tab, origin: MainWindow._tab_owns_origin(None, tab, origin),
    )
    resolve = MainWindow._editable_node_for_id

    assert resolve(mw, 1) is not None, "the user's own cube(10) must stay editable"
    assert resolve(mw, 2) is None, "a node inside BOSL2 must not be editable"
    assert resolve(mw, 99) is None, "an unknown id resolves to nothing"


def test_offsets_that_would_corrupt_are_the_ones_refused(tmp_path):
    """Guard the specific failure, not just the origin check: the library
    offset is past the end of the script, where a splice lands at the end of
    the file and wraps nothing."""
    script = tmp_path / "s.scad"
    source = "include <BOSL2/std.scad>\ncube(10);\ncuboid(10);\n"
    script.write_text(source)
    lib = tmp_path / "vnf.scad"
    lib.write_text("x")

    node = SimpleNamespace(position=_pos(str(lib), 85256, 85304))
    mw = SimpleNamespace(
        id_to_node={7: node},
        _rendered_tab=_tab(str(script), str(script)),
        _tab_owns_origin=lambda tab, origin: MainWindow._tab_owns_origin(None, tab, origin),
    )
    assert MainWindow._editable_node_for_id(mw, 7) is None

    # What the old code would have done with that node, for the record.
    start = node.position.start_offset
    assert start > len(source)
    corrupted = source[:start] + "translate([1, 0, 0]) " + source[start:]
    assert corrupted.endswith("translate([1, 0, 0]) "), "a transform wrapping nothing"
