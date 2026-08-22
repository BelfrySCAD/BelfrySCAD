"""Cmd+C copies from the widget that has focus.

Reported: "to copy text from BelfrySCAD, I generally have to select the
text, then use Copy from the context menu. The expected Cmd+C keyboard
shortcut doesn't work."

Cut/Copy/Paste/Select All live on menu QActions carrying the standard key
sequences, and a QAction shortcut is WindowShortcut by default -- it fires
for the whole window, intercepting the key before the focused widget's own
built-in handling sees it. All four then sent the operation to the code
editor unconditionally, so selecting text in the console or the AI pane and
pressing Cmd+C did nothing visible. Copy from that widget's own context
menu never goes through the menu action, which is why it worked.

`_clipboard_target` is exercised unbound against stand-ins, so no Qt widget
is built -- widget instantiation crashes the pytest runner here. The real
focus behaviour is verified by a throwaway script.
"""
import pytest

from belfryscad.window.main_window import MainWindow


class _Editor:
    def copy(self): pass
    def cut(self): pass
    def paste(self): pass
    def selectAll(self): pass


class _Console:
    """Read-only text view: can copy and select, cannot cut."""
    def copy(self): pass
    def selectAll(self): pass


class _Viewport:
    """A QOpenGLWidget stand-in -- no clipboard methods at all."""


class _MW:
    def __init__(self, editor, focused):
        self._editor = editor
        self._focused = focused

    def _current_editor(self):
        return self._editor


def target(method, focused, editor=None, monkeypatch=None):
    ed = editor if editor is not None else _Editor()
    mw = _MW(ed, focused)
    import belfryscad.window.main_window as mod
    monkeypatch.setattr(mod.QApplication, "focusWidget", staticmethod(lambda: focused))
    return MainWindow._clipboard_target(mw, method), ed


class TestClipboardTarget:
    def test_the_focused_widget_wins(self, monkeypatch):
        console = _Console()
        got, _ed = target("copy", console, monkeypatch=monkeypatch)
        assert got is console

    def test_the_editor_is_used_when_it_has_focus(self, monkeypatch):
        ed = _Editor()
        got, _ = target("copy", ed, editor=ed, monkeypatch=monkeypatch)
        assert got is ed

    def test_it_falls_back_when_the_focused_widget_cannot(self, monkeypatch):
        # Focus on the viewport, which has no copy() at all -- Cmd+C should
        # still copy from the editor, as it always did.
        got, ed = target("copy", _Viewport(), monkeypatch=monkeypatch)
        assert got is ed

    def test_it_falls_back_when_nothing_has_focus(self, monkeypatch):
        got, ed = target("copy", None, monkeypatch=monkeypatch)
        assert got is ed

    def test_each_operation_is_dispatched_on_its_own_capability(self, monkeypatch):
        # A read-only view can copy and select all, but not cut. Cut must
        # fall back rather than raise or silently no-op on the wrong widget.
        console = _Console()
        got_copy, _ = target("copy", console, monkeypatch=monkeypatch)
        got_sel, _ = target("selectAll", console, monkeypatch=monkeypatch)
        got_cut, ed = target("cut", console, monkeypatch=monkeypatch)
        assert got_copy is console
        assert got_sel is console
        assert got_cut is ed, "cut should not go to a widget that cannot cut"

    @pytest.mark.parametrize("method", ["cut", "copy", "paste", "selectAll"])
    def test_every_operation_is_covered(self, method, monkeypatch):
        ed = _Editor()
        got, _ = target(method, ed, editor=ed, monkeypatch=monkeypatch)
        assert got is ed

    def test_no_editor_and_no_usable_focus_yields_none(self, monkeypatch):
        mw = _MW(None, None)
        import belfryscad.window.main_window as mod
        monkeypatch.setattr(mod.QApplication, "focusWidget", staticmethod(lambda: None))
        assert MainWindow._clipboard_target(mw, "copy") is None


class TestHandlersUseTheTarget:
    """Every one of the four goes through _clipboard_target -- none may
    reach for _current_editor() directly again."""

    @pytest.mark.parametrize("name", ["_edit_cut", "_edit_copy", "_edit_paste",
                                       "_edit_select_all"])
    def test_handler_dispatches_on_focus(self, name):
        import inspect
        body = inspect.getsource(getattr(MainWindow, name))
        assert "_clipboard_target" in body, f"{name} does not dispatch on focus"
        assert "_current_editor" not in body, f"{name} still targets the editor directly"
