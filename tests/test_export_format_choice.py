"""Which format the Export dialog actually writes.

The dialog hands back a path and the filter that was selected, and those
two can disagree -- the user picks 3MF from the dropdown but types
"part.ply", or picks PLY and types a bare name. Before this, the selected
filter was discarded entirely and anything without a recognised suffix was
written as STL.

Pure function, no widgets: only the resolution rule is under test, not
QFileDialog.
"""
import pytest

from belfryscad.window.main_window import (
    _EXPORT_FORMATS, _resolve_export_format,
)

THREE_MF = "3MF Files (*.3mf)"
STL = "STL Files (*.stl)"
PLY = "PLY Files (*.ply)"


def test_3mf_is_the_default_format():
    """First in the list is what the dialog opens on, and what an
    unrecognised filter falls back to."""
    assert _EXPORT_FORMATS[0][1] == ".3mf"
    assert _resolve_export_format("/x/part", "not a real filter")[1] == ".3mf"


def test_every_writable_format_is_offered():
    """Asked of the evaluator, not restated. Both sides of this used to be
    hand-written lists that agreed with each other and not with the writer
    table, so the suite passed while .off could be written by neither the
    dialog nor the CLI."""
    from belfryscad.exporters import export_extensions
    assert {e for _f, e in _EXPORT_FORMATS} == set(export_extensions())


def test_the_dialog_and_the_cli_accept_the_same_formats():
    """A format the Export dialog offers but `-o` rejects (or the reverse)
    is the kind of gap nobody notices until it bites."""
    from belfryscad.headless import _export_extensions
    assert {e for _f, e in _EXPORT_FORMATS} == _export_extensions()


# --- a typed suffix wins ----------------------------------------------
@pytest.mark.parametrize("name,ext", [
    ("/x/part.amf", ".amf"),
    ("/x/part.off", ".off"),
    ("/x/part.wrl", ".wrl"),
    ("/x/part.x3d", ".x3d"),
    ("/x/part.ply", ".ply"),
    ("/x/part.stl", ".stl"),
    ("/x/part.obj", ".obj"),
    ("/x/part.3mf", ".3mf"),
])
def test_a_typed_suffix_beats_the_dropdown(name, ext):
    # 3MF selected in every case; the typed suffix still decides.
    path, resolved = _resolve_export_format(name, THREE_MF)
    assert resolved == ext
    assert path == name          # nothing appended


def test_a_typed_suffix_is_matched_case_insensitively():
    path, ext = _resolve_export_format("/x/PART.STL", PLY)
    assert ext == ".stl"
    # ...but the path keeps the user's own case. Appending ".stl" here --
    # which the old code did, since "PART.STL".endswith(".stl") is False --
    # produced "PART.STL.stl".
    assert path == "/x/PART.STL"


# --- otherwise the dropdown decides -----------------------------------
@pytest.mark.parametrize("chosen,ext", [
    (THREE_MF, ".3mf"),
    (STL, ".stl"),
    (PLY, ".ply"),
    ("OBJ Files (*.obj)", ".obj"),
])
def test_a_bare_name_takes_the_selected_format(chosen, ext):
    path, resolved = _resolve_export_format("/x/part", chosen)
    assert resolved == ext
    assert path == "/x/part" + ext


def test_selecting_a_format_no_longer_silently_writes_stl():
    """The bug this closes: the selected filter was discarded, so picking
    PLY and typing a bare name wrote an STL."""
    _path, ext = _resolve_export_format("/x/part", PLY)
    assert ext != ".stl"


# --- an unrecognised suffix -------------------------------------------
def test_an_unknown_suffix_is_appended_to_not_replaced():
    # "part.v2" is a version, not a format. Replacing the "suffix" would
    # throw away text the user typed on purpose.
    path, ext = _resolve_export_format("/x/part.v2", THREE_MF)
    assert path == "/x/part.v2.3mf"
    assert ext == ".3mf"


def test_the_resolved_extension_always_matches_the_resolved_path():
    """Whatever comes back, the dispatch downstream keys off `ext` while
    the writer gets `path` -- they must never disagree."""
    known = {e for _f, e in _EXPORT_FORMATS}
    for name in ("/x/part", "/x/part.ply", "/x/part.v2", "/x/PART.STL", "/x/a.b.c"):
        for chosen, _e in _EXPORT_FORMATS:
            path, ext = _resolve_export_format(name, chosen)
            assert ext in known
            assert path.lower().endswith(ext)
