"""Evaluator messages name the tab, not the temp file.

Every render writes the live buffer to a temp .scad -- saved or not, since
the C++ parser is path-based -- so every position the evaluator reports
names a `belfryscad-XXXX.scad` nobody recognises. Reported as: "the
evaluator gives the tempfile name instead of the tab name".

The `_path_labels` map already existed but was only consulted by the
profile report; console output went through untouched.

Both functions are exercised unbound against stand-ins, so no Qt widget is
built -- widget instantiation crashes the pytest runner here.
"""
import pytest

from belfryscad.window.main_window import FileTab, MainWindow


class _Tab:
    def __init__(self, file_path=None, is_modified=False, suggested_name=None):
        self.file_path = file_path
        self.is_modified = is_modified
        self.suggested_name = suggested_name


def label(**kw):
    return FileTab.message_label(_Tab(**kw))


class _MW:
    def __init__(self, labels):
        self._path_labels = dict(labels)


def relabel(text, labels):
    return MainWindow._relabel_paths(_MW(labels), text)


class TestMessageLabel:
    def test_a_saved_unmodified_buffer_is_just_its_name(self):
        assert label(file_path="/a/b/part.scad") == "part.scad"

    def test_a_modified_buffer_says_unsaved(self):
        # Spelled out rather than display_name()'s bare "*", which is
        # legible on a tab but cryptic mid-sentence.
        assert label(file_path="/a/b/part.scad", is_modified=True) == "part.scad (unsaved)"

    def test_an_untitled_buffer_is_not_told_it_is_unsaved(self):
        # There is no saved version to differ from; the name says it.
        assert label(is_modified=True) == "Untitled"
        assert label(is_modified=True, suggested_name="scratch") == "scratch"

    def test_only_the_basename_is_used(self):
        assert "/" not in label(file_path="/very/long/path/part.scad")


class TestRelabelPaths:
    TMP = "/tmp/relab/belfryscad-abc123.scad"
    REAL = "/private/tmp/relab/belfryscad-abc123.scad"
    LABELS = {TMP: "part.scad (unsaved)", REAL: "part.scad (unsaved)"}

    def test_a_temp_path_becomes_the_tab_name(self):
        msg = f"ERROR: boom in file {self.TMP}, line 4"
        assert relabel(msg, self.LABELS) == "ERROR: boom in file part.scad (unsaved), line 4"

    def test_the_realpath_spelling_is_also_matched(self):
        # macOS resolves /var to /private/var, so a message can come back
        # carrying either spelling of the path we handed out.
        msg = f"TRACE: at {self.REAL}, line 4"
        assert "belfryscad-" not in relabel(msg, self.LABELS)

    def test_a_real_file_is_left_alone(self):
        # An include or library path is genuinely useful and must survive.
        msg = "WARNING: something in file /usr/lib/BOSL2/std.scad, line 9"
        assert relabel(msg, self.LABELS) == msg

    def test_every_occurrence_is_replaced(self):
        msg = f"in file {self.TMP}, line 4, from {self.TMP}, line 9"
        out = relabel(msg, self.LABELS)
        assert "belfryscad-" not in out
        assert out.count("part.scad (unsaved)") == 2

    def test_multiline_output_is_handled(self):
        msg = f"ERROR: a {self.TMP}\nTRACE: b {self.REAL}"
        assert "belfryscad-" not in relabel(msg, self.LABELS)

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_input_is_returned_unchanged(self, text):
        assert relabel(text, self.LABELS) == text

    def test_no_labels_yet_is_a_no_op(self):
        msg = f"ERROR: boom in file {self.TMP}"
        assert relabel(msg, {}) == msg

    def test_longest_path_wins(self):
        # A directory must never eat a filename nested inside it.
        labels = {"/tmp/a": "DIR", "/tmp/a/b.scad": "FILE"}
        assert relabel("see /tmp/a/b.scad now", labels) == "see FILE now"
