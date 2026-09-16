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


class _FakeWindow:
    """Just enough MainWindow for the selection lookups.

    The methods under test are pure functions of tabs and the id map, so
    they are borrowed from the real class rather than reimplemented -- a
    stub that drifted from MainWindow would test nothing.
    """

    _tab_owns_origin = MainWindow._tab_owns_origin
    _tab_for_origin = MainWindow._tab_for_origin
    _selection_levels = MainWindow._selection_levels
    _default_selection_level = MainWindow._default_selection_level
    _selectable_span_for_id = MainWindow._selectable_span_for_id
    _editable_span_for_id = MainWindow._editable_span_for_id

    def __init__(self, id_to_node, rendered, others=()):
        self.id_to_node = id_to_node
        self._rendered_tab = rendered
        self._tabs_list = [t for t in (rendered, *others) if t is not None]
        self._selection_level = None
        self._selection_level_id = None

    def _all_tabs(self):
        return self._tabs_list


def _build(id_to_node, rendered, others=()):
    return _FakeWindow(id_to_node, rendered, others)


def _span(mw, oid):
    return mw._selectable_span_for_id(oid)


def _editable(mw, oid):
    return mw._editable_span_for_id(oid)


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


def test_stepping_walks_the_chain_and_stops_at_both_ends(tmp_path):
    """Alt+Up/Down moves through the levels: out toward top level, in toward
    the callee. A BOSL2 author stepping in reaches the library's own layers."""
    script = tmp_path / "s.scad"
    src = "include <BOSL2/std.scad>\ncuboid(10);\n"
    script.write_text(src)
    libdir = tmp_path / "BOSL2"
    libdir.mkdir()
    inner = libdir / "vnf.scad"
    outer = libdir / "shapes3d.scad"
    inner.write_text("// stand-in\n")
    outer.write_text("// stand-in\n")

    chain = (_pos(str(inner), 100, 110),
             _pos(str(outer), 200, 210),
             _pos(str(script), 25, 36))
    node = SimpleNamespace(position=_pos(str(inner), 100, 110), call_sites=chain)

    tabs = {p: _tab(str(p), str(p), read_only=(p is not script))
            for p in (script, inner, outer)}
    mw = _build({3: node}, tabs[script], others=[tabs[inner], tabs[outer]])

    levels = mw._selection_levels(3)
    assert [l.origin for l in levels] == [str(inner), str(outer), str(script)]

    # A fresh pick starts at the user's own line, deepest-in-script.
    assert mw._default_selection_level(levels) == 2
    span, owner = _span(mw, 3)
    assert span is chain[2] and owner is tabs[script]

    # Stepping IN reaches the library, which is read-only: selectable only.
    mw._selection_level = 1
    span, owner = _span(mw, 3)
    assert span is chain[1] and owner is tabs[outer]
    assert _editable(mw, 3) is None

    # Clamps rather than running off either end.
    mw._selection_level = 99
    assert _span(mw, 3)[0] is chain[2]
    mw._selection_level = -5
    assert _span(mw, 3)[0] is chain[0]


def test_repeated_frames_collapse_into_one_level(tmp_path):
    """A cuboid() chain names the same line twice -- BOSL2's own translate
    wrapper is a module too -- and stepping through duplicates feels broken."""
    script = tmp_path / "s.scad"
    script.write_text("cuboid(10);\n")
    same = _pos(str(script), 0, 11)
    dup = _pos(str(script), 0, 11)
    node = SimpleNamespace(position=_pos(str(script), 0, 11),
                           call_sites=(same, dup))
    mw = _build({4: node}, _tab(str(script), str(script)))
    assert len(mw._selection_levels(4)) == 1


def test_a_level_does_not_carry_over_to_a_different_pick(tmp_path):
    """Stepping into a library on one body must not make the next body
    unselectable: its chain never touches that file, so the carried level
    resolved to a frame with no open tab and read as "not selectable"."""
    script = tmp_path / "s.scad"
    script.write_text("cube(10);\ncuboid(8);\n")
    opened = tmp_path / "opened.scad"
    opened.write_text("// open in a tab\n")
    unopened = tmp_path / "unopened.scad"
    unopened.write_text("// exists, never opened\n")

    # Body A steps into a library the user HAS open.
    a = SimpleNamespace(position=_pos(str(opened), 0, 5),
                        call_sites=(_pos(str(opened), 0, 5),
                                    _pos(str(script), 0, 9)))
    # Body B's chain goes through a file that is NOT open, at the same index.
    b = SimpleNamespace(position=_pos(str(unopened), 0, 5),
                        call_sites=(_pos(str(unopened), 0, 5),
                                    _pos(str(script), 10, 20)))
    mw = _build({1: a, 2: b}, _tab(str(script), str(script)),
                others=[_tab(str(opened), str(opened), read_only=True)])

    # Both start on the user's own line.
    assert _span(mw, 1)[0].origin == str(script)
    assert _span(mw, 2)[0].origin == str(script)

    # Step body A in, to the opened library.
    levels = mw._selection_levels(1)
    # What _step_selection_level does: the level and the id it belongs to.
    mw._selection_level = next(i for i, l in enumerate(levels) if l.origin == str(opened))
    mw._selection_level_id = 1
    assert _span(mw, 1)[0].origin == str(opened)

    # Now pick body B. Carried over, level 0 is its unopened file and the
    # pick would be refused; reset, it is the user's own line again.
    span, _owner = _span(mw, 2)
    assert span is not None, "a different pick must not inherit A's level"
    assert span.origin == str(script)


# -- Re-selecting after an edit (#463 follow-up) ---------------------------

def test_the_reselect_only_records_it_cannot_search_yet():
    """`_render()` starts a QThread and returns, so when the undo command
    asks for the node back the id map is still the one from BEFORE the edit
    -- spans at the old offsets, nothing matching, selection cleared. One
    nudge deselected the object and a second was impossible."""
    mw = SimpleNamespace(_pending_reselect=None)
    MainWindow._restore_selection_after_gizmo(mw, 42)
    assert mw._pending_reselect == 42


def test_the_reselect_happens_after_the_new_ids_land():
    """Ordering is the whole fix: _on_render_done must consume it only
    after assigning id_to_node."""
    import inspect
    src = inspect.getsource(MainWindow._on_render_done)
    assign = src.index("self.id_to_node = id_to_node")
    consume = src.index("_apply_pending_reselect()")
    assert assign < consume, "the reselect must run after the map is replaced"


def test_applying_it_selects_the_span_that_starts_there(tmp_path):
    script = tmp_path / "s.scad"
    script.write_text("translate([1,0,0]) cube(10);\n")
    tab = _tab(str(script), str(script))

    selected = {}
    node = SimpleNamespace(position=_pos(str(script), 19, 27), call_sites=())
    mw = _build({7: node}, tab)
    mw._pending_reselect = 19
    mw._viewport = SimpleNamespace(set_selection=lambda i: selected.setdefault("id", i),
                                   update=lambda: None)
    tab.editor.set_selection = lambda a, b: selected.setdefault("span", (a, b))
    tab.editor.clear_selection = lambda: selected.setdefault("cleared", True)

    MainWindow._apply_pending_reselect(mw)
    assert selected.get("id") == 7
    assert selected.get("span") == (19, 27)
    assert mw._pending_reselect is None, "consumed, so a later render does not redo it"


def test_nothing_matching_clears_rather_than_selecting_the_wrong_thing(tmp_path):
    script = tmp_path / "s.scad"
    script.write_text("cube(10);\n")
    tab = _tab(str(script), str(script))
    cleared = {}
    mw = _build({1: SimpleNamespace(position=_pos(str(script), 0, 9), call_sites=())}, tab)
    mw._pending_reselect = 999
    mw._viewport = SimpleNamespace(set_selection=lambda i: cleared.setdefault("id", i),
                                   update=lambda: None)
    tab.editor.clear_selection = lambda: cleared.setdefault("cleared", True)
    MainWindow._apply_pending_reselect(mw)
    assert cleared.get("id") is None and cleared.get("cleared") is True
