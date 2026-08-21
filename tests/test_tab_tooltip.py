"""File-tab tooltips show the full path.

A tab label is only the basename, so two files of the same name in
different directories are indistinguishable without it.

`FileTab.tooltip_text` is exercised unbound against a stand-in, so no Qt
widget is constructed -- widget instantiation crashes the pytest runner
here. The real tabs (including that the tooltip follows a Save As) are
verified by a throwaway script.
"""
import pytest

from belfryscad.window.main_window import FileTab


class _Stub:
    """Just enough of a FileTab for tooltip_text."""
    def __init__(self, file_path=None, suggested_name=None):
        self.file_path = file_path
        self.suggested_name = suggested_name


def tip(file_path=None, suggested_name=None):
    return FileTab.tooltip_text(_Stub(file_path, suggested_name))


class TestTooltipText:
    def test_a_saved_file_shows_its_full_path(self):
        assert tip("/a/b/widget.scad") == "/a/b/widget.scad"

    def test_the_path_is_the_whole_point(self):
        # Two tabs whose labels are identical must differ here, which is
        # the entire reason the tooltip exists.
        assert tip("/tmp/A/part.scad") != tip("/tmp/B/part.scad")

    def test_a_relative_path_is_shown_as_given(self):
        assert tip("part.scad") == "part.scad"

    def test_a_path_object_is_stringified(self):
        # Compared against str(Path(...)), not a hardcoded POSIX string:
        # on Windows a Path stringifies with backslashes, and the tooltip
        # is meant to show the path in its NATIVE form -- which is what a
        # user would paste into their own shell.
        from pathlib import Path
        p = Path("/a/b/widget.scad")
        assert tip(p) == str(p)
        assert tip(p) != repr(p)

    def test_an_unsaved_buffer_says_so(self):
        # Better than an empty tooltip, which reads as "no information"
        # rather than "there is no file yet".
        got = tip(None)
        assert "Untitled" in got and "not saved" in got

    def test_an_unsaved_buffer_uses_its_suggested_name(self):
        got = tip(None, suggested_name="scratch")
        assert got.startswith("scratch") and "not saved" in got

    def test_an_unsaved_tooltip_is_not_a_bare_name(self):
        # It must not look like a path, or it invites a double-take.
        assert tip(None) != "Untitled"


class TestLabelAndTooltipStayTogether:
    """`_sync_tab_label` sets both, so a rename cannot update one and leave
    the other stale -- which is exactly what four separate call sites
    setting only the text had allowed."""

    def test_every_rename_path_goes_through_the_helper(self):
        import inspect
        from belfryscad.window import main_window
        src = inspect.getsource(main_window)
        # No bare setTabText outside the helper and the facade passthrough.
        bare = [ln.strip() for ln in src.splitlines()
                if "setTabText(idx, tab.display_name())" in ln]
        assert len(bare) == 1, (
            "a rename site sets the label without the tooltip: " + repr(bare))

    def test_the_helper_sets_both(self):
        import inspect
        from belfryscad.window.main_window import MainWindow
        body = inspect.getsource(MainWindow._sync_tab_label)
        assert "setTabText" in body and "setTabToolTip" in body

    def test_the_helper_does_not_call_itself(self):
        # It did, briefly -- a regex rewrote the line inside its own body.
        import inspect
        from belfryscad.window.main_window import MainWindow
        body = inspect.getsource(MainWindow._sync_tab_label)
        after_docstring = body.split('"""')[2]
        assert "_sync_tab_label" not in after_docstring

    def test_the_facade_exposes_a_tooltip_setter(self):
        # The tab strip is a hand-rolled QTabWidget facade, not a real one,
        # so anything QTabWidget offers has to be added explicitly.
        from belfryscad.window.main_window import _DetachedTabBar
        assert hasattr(_DetachedTabBar, "setTabToolTip")
