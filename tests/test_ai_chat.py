"""Tests for belfryscad.window.ai_chat's pure helpers.

Only `diff_to_html` is covered here -- the rest of the module is live Qt
widgets and a worker thread, verified via throwaway scripts instead (see
feedback_gl_qt_tests_crash_pytest).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from belfryscad.window.ai_chat import diff_to_html


class TestDiffToHtml:
    def test_added_line_is_green(self):
        html = diff_to_html("+cube(2);")
        assert "#116329" in html and "cube(2);" in html

    def test_removed_line_is_red(self):
        html = diff_to_html("-cube(1);")
        assert "#82071e" in html

    def test_hunk_header_is_blue(self):
        assert "#0969da" in diff_to_html("@@ -1 +1 @@")

    def test_file_headers_are_not_treated_as_add_remove(self):
        # "---"/"+++" start with - and + but are headers, not a giant
        # deletion/addition -- they must match before the 1-char tests.
        html = diff_to_html("--- a.scad\n+++ a.scad")
        assert "#116329" not in html and "#82071e" not in html
        assert "#57606a" in html

    def test_context_line_unstyled(self):
        html = diff_to_html(" sphere(3);")
        assert "#116329" not in html and "#82071e" not in html

    def test_html_is_escaped(self):
        html = diff_to_html("+echo(\"<b>&</b>\");")
        assert "&lt;b&gt;" in html and "&amp;" in html
        assert "<b>" not in html.replace("<b>&amp;", "")

    def test_blank_line_kept_as_a_row(self):
        # An empty diff line still needs to occupy a row, or line numbers
        # visually drift against the real file.
        assert diff_to_html("").count("<div") >= 1

    def test_monospace_wrapper(self):
        assert "Menlo" in diff_to_html("+x")
