"""The column guide preference takes a list of columns, not one column.

67 is where a BOSL2 example stops fitting beside its image; 100 is a comment
width cap. Both are worth seeing at once (#467).
"""
import pytest

from belfryscad.window.preferences import _DEFAULTS, parse_guide_columns


@pytest.mark.parametrize("text, want", [
    ("80", [80]),
    ("67, 100", [67, 100]),
    ("67,100", [67, 100]),
    (" 100 ; 67 ", [67, 100]),          # sorted, and ; taken as a separator
    ("80, 80", [80]),                   # deduplicated
])
def test_a_list_of_columns_is_parsed(text, want):
    assert parse_guide_columns(text) == want


def test_a_settings_file_written_before_this_still_reads():
    """The key held a bare int. No migration step: it parses as one column."""
    assert parse_guide_columns(80) == [80]


@pytest.mark.parametrize("text", ["", "abc", ",", "  "])
def test_junk_gives_no_guides_rather_than_an_error(text):
    assert parse_guide_columns(text) == []


def test_a_half_typed_entry_keeps_what_parses():
    """The field applies on every keystroke, so "67, " while reaching for the
    next digit must keep drawing the 67 rather than blanking the guide."""
    assert parse_guide_columns("67, ") == [67]
    assert parse_guide_columns("67, 1") == [1, 67]


@pytest.mark.parametrize("text", ["0", "301", "-5", "1000"])
def test_columns_outside_the_range_are_dropped(text):
    assert parse_guide_columns(text) == []


def test_the_default_is_still_one_guide_at_80():
    assert parse_guide_columns(_DEFAULTS["editor/columnGuide"]) == [80]


def test_the_guide_widget_takes_the_list():
    import inspect
    from belfryscad.window.editor import _ColumnGuide
    assert not hasattr(_ColumnGuide, "set_column"), "the singular API is gone"
    assert hasattr(_ColumnGuide, "set_columns")
    src = inspect.getsource(_ColumnGuide.paintEvent)
    assert "for x in xs" in src, "every column must be drawn, not just the first"


def test_no_caller_still_passes_a_single_column():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "src" / "belfryscad"
    for path in root.rglob("*.py"):
        assert "set_column(" not in path.read_text(), path
