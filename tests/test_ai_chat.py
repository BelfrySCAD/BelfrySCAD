"""Tests for belfryscad.window.ai_chat's pure helpers.

Only `diff_to_html` is covered here -- the rest of the module is live Qt
widgets and a worker thread, verified via throwaway scripts instead (see
feedback_gl_qt_tests_crash_pytest).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from belfryscad.window.ai_chat import diff_to_html


class TestDiffToHtml:
    # Asserted by hue family rather than by exact hex: the palette is chosen
    # for contrast against whatever background the diff is drawn on (see
    # scripts/verify_diff_contrast.py), and pinning the literals here made
    # these tests fail for a change that improved what they were guarding.
    @staticmethod
    def _hues(html):
        import re
        import colorsys
        out = []
        for hexc in re.findall(r"color:(#[0-9A-Fa-f]{6})", html):
            r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
            h, _s, v = colorsys.rgb_to_hsv(r, g, b)
            out.append((h * 360, v))
        return out

    def _has_hue(self, html, low, high):
        return any(low <= h <= high for h, _v in self._hues(html))

    def test_added_line_is_green(self):
        html = diff_to_html("+cube(2);")
        assert "cube(2);" in html
        assert self._has_hue(html, 80, 160), self._hues(html)

    def test_removed_line_is_red(self):
        html = diff_to_html("-cube(1);")
        hues = self._hues(html)
        assert any(h <= 20 or h >= 340 for h, _v in hues), hues

    def test_hunk_header_is_blue(self):
        html = diff_to_html("@@ -1 +1 @@")
        assert self._has_hue(html, 190, 260), self._hues(html)

    def test_file_headers_are_not_treated_as_add_remove(self):
        # "---"/"+++" start with - and + but are headers, not a giant
        # deletion/addition -- they must match before the 1-char tests.
        html = diff_to_html("--- a.scad\n+++ a.scad")
        assert "background-color" not in html, html
        assert not self._has_hue(html, 80, 160), self._hues(html)

    def test_context_line_unstyled(self):
        html = diff_to_html(" sphere(3);")
        assert "background-color" not in html and "color:" not in html, html

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
