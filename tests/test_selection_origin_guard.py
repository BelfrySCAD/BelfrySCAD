"""Which source span a picked geometry id resolves to, and whether it may be
edited.

An originalID names the node that PRODUCED the geometry, which for anything a
library builds is a node inside that library -- including a plain `cube(10)`
once BOSL2 is included, since BOSL2 overrides the primitives with its own
modules. Those nodes carry byte offsets into the library file, and the gizmo
commits splice at that offset in the user's buffer, so an offset past the end
appended a stray transform to it (#450).

The answer is a frame from the node's call chain (#451): the last one in the
rendered script by default, a library frame when the user has that file open.
Read-only files select but do not edit.
"""
from types import SimpleNamespace

from belfryscad.window.main_window import MainWindow


def _pos(origin, start=0, end=5):
    return SimpleNamespace(origin=origin, start_offset=start, end_offset=end)


def _tab(file_path=None, parse_path=None, read_only=False):
    return SimpleNamespace(file_path=file_path, _last_parse_path=parse_path,
                           editor=SimpleNamespace(isReadOnly=lambda: read_only))


def _mw(id_to_node, rendered, others=()):
    tabs = [rendered, *others]
    return SimpleNamespace(
        id_to_node=id_to_node,
        _rendered_tab=rendered,
        _all_tabs=lambda: tabs,
        _tab_owns_origin=lambda t, origin: MainWindow._tab_owns_origin(None, t, origin),
        _selectable_span_for_id=lambda oid: MainWindow._selectable_span_for_id(
            _mw.current, oid),
    )


def _build(id_to_node, rendered, others=()):
    mw = _mw(id_to_node, rendered, others)
    _mw.current = mw
    return mw


def _span(mw, oid):
    return MainWindow._selectable_span_for_id(mw, oid)


def _editable(mw, oid):
    return MainWindow._editable_span_for_id(mw, oid)


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
    tab = _tab(str(script), str(script))
    mw = _build({1: SimpleNamespace(position=_pos(str(script), 0, 9), call_sites=())}, tab)
    span, owner = _span(mw, 1)
    assert (span.start_offset, span.end_offset) == (0, 9)
    assert owner is tab
    assert _editable(mw, 1) is span


def test_a_library_node_resolves_to_the_last_frame_in_the_script(tmp_path):
    """The chain runs innermost-first through the library and out into the
    user's file; the default is the first frame the script owns."""
    script = tmp_path / "s.scad"
    src = "include <BOSL2/std.scad>\ncuboid(10, rounding=2);\n"
    script.write_text(src)
    libdir = tmp_path / "BOSL2"
    libdir.mkdir()
    (libdir / "vnf.scad").write_text("// stand-in\n")
    (libdir / "shapes3d.scad").write_text("// stand-in\n")

    chain = (
        _pos(str(libdir / "vnf.scad"), 85256, 85304),       # innermost, library
        _pos(str(libdir / "shapes3d.scad"), 900, 950),      # still library
        _pos(str(script), 25, 47),                          # the user's own call
    )
    node = SimpleNamespace(position=_pos(str(libdir / "vnf.scad"), 85256, 85304),
                           call_sites=chain)
    tab = _tab(str(script), str(script))
    mw = _build({5: node}, tab)

    span, owner = _span(mw, 5)
    assert span is chain[2], "must be the user's own call, not a library frame"
    assert owner is tab
    assert src[span.start_offset:span.end_offset] == "cuboid(10, rounding=2)"


def test_a_library_frame_is_reachable_when_that_file_is_open(tmp_path):
    """A BOSL2 author with shapes3d.scad open can still be shown it, when the
    chain never re-enters the running script."""
    script = tmp_path / "s.scad"
    script.write_text("use <lib.scad>\n")
    lib = tmp_path / "lib.scad"
    lib.write_text("cube(1);\n")

    frame = _pos(str(lib), 0, 8)
    node = SimpleNamespace(position=_pos(str(lib), 0, 8), call_sites=(frame,))
    rendered = _tab(str(script), str(script))
    libtab = _tab(str(lib), str(lib), read_only=True)
    mw = _build({9: node}, rendered, others=[libtab])

    span, owner = _span(mw, 9)
    assert span is frame
    assert owner is libtab
    # ...but read-only, so it selects and does not edit.
    assert _editable(mw, 9) is None


def test_read_only_selects_but_does_not_edit(tmp_path):
    """The rule the user asked for, and the way out of it."""
    lib = tmp_path / "lib.scad"
    lib.write_text("cube(1);\n")
    node = SimpleNamespace(position=_pos(str(lib), 0, 8), call_sites=())

    ro = _tab(str(lib), str(lib), read_only=True)
    mw = _build({1: node}, ro)
    span, _owner = _span(mw, 1)
    assert span is not None, "a read-only file is still selectable"
    assert _editable(mw, 1) is None, "but not editable"

    # Edit > Read Only unticked: the same pick becomes editable.
    rw = _tab(str(lib), str(lib), read_only=False)
    mw2 = _build({1: node}, rw)
    assert _editable(mw2, 1) is not None


def test_nothing_reachable_is_refused(tmp_path):
    script = tmp_path / "s.scad"
    script.write_text("use <other.scad>\n")
    lib = tmp_path / "other.scad"
    lib.write_text("cube(1);\n")
    node = SimpleNamespace(position=_pos(str(lib), 0, 8),
                           call_sites=(_pos(str(lib), 0, 8),))
    mw = _build({9: node}, _tab(str(script), str(script)))
    assert _span(mw, 9) == (None, None)
    assert _editable(mw, 9) is None
    assert _span(mw, 99) == (None, None)


def test_an_evaluator_without_a_chain_still_refuses(tmp_path):
    """Until 1.21.0 is pinned there is no call_sites; refusing (#450) is
    then the correct behaviour."""
    script = tmp_path / "s.scad"
    script.write_text("include <BOSL2/std.scad>\ncube(10);\n")
    lib = tmp_path / "builtins.scad"
    lib.write_text("x")

    class OldNode:            # no call_sites attribute at all
        def __init__(self, position):
            self.position = position

    mw = _build({2: OldNode(_pos(str(lib), 1110, 1135))},
                _tab(str(script), str(script)))
    assert _editable(mw, 2) is None


def test_the_offsets_that_would_corrupt_are_never_returned(tmp_path):
    """Guard the specific failure: a library offset past the end of the
    script, where a splice lands at the end of the file and wraps nothing."""
    script = tmp_path / "s.scad"
    source = "include <BOSL2/std.scad>\ncuboid(10);\n"
    script.write_text(source)
    lib = tmp_path / "vnf.scad"
    lib.write_text("x")

    node = SimpleNamespace(position=_pos(str(lib), 85256, 85304),
                           call_sites=(_pos(str(lib), 85256, 85304),
                                       _pos(str(script), 25, 36)))
    mw = _build({7: node}, _tab(str(script), str(script)))

    span = _editable(mw, 7)
    assert span.start_offset < len(source), "the span must be inside this buffer"

    bad = node.position.start_offset
    assert bad > len(source)
    corrupted = source[:bad] + "translate([1, 0, 0]) " + source[bad:]
    assert corrupted.endswith("translate([1, 0, 0]) "), "a transform wrapping nothing"
