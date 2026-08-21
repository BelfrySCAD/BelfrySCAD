"""Indent-stop arithmetic behind Tab / Shift+Tab / Backspace.

Reported: "When a line is indented by a number of spaces not divisible by
the indent size, typing delete on one of the extra spaces should delete
that space, instead it unindents one level."

All three keys had the same shape of bug -- they moved by a fixed amount
rather than to the nearest indent STOP, so a line that was off-grid stayed
off-grid whichever key you pressed.

These call the stop helpers unbound, against a stand-in holding only
`_indent_size`, so no Qt widget is constructed -- widget instantiation
crashes the pytest runner here. The real key-event behaviour is verified
separately with a throwaway script driving actual QKeyEvents through a real
CodeEditor.
"""
import pytest

from belfryscad.window.editor import CodeEditor


class _Fake:
    """Just enough of a CodeEditor for the stop math."""
    def __init__(self, size):
        self._indent_size = size


def nxt(width, size=4):
    return CodeEditor._next_indent_stop(_Fake(size), width)


def prv(width, size=4):
    return CodeEditor._prev_indent_stop(_Fake(size), width)


class TestIndentStops:
    def test_leading_width(self):
        assert CodeEditor._leading_width("    cube(1);") == 4
        assert CodeEditor._leading_width("cube(1);") == 0
        assert CodeEditor._leading_width("      ") == 6
        assert CodeEditor._leading_width("") == 0

    # -- Tab ---------------------------------------------------------------

    @pytest.mark.parametrize("width,want", [(0, 4), (1, 4), (2, 4), (3, 4),
                                             (4, 8), (5, 8), (6, 8), (7, 8),
                                             (8, 12)])
    def test_next_stop_is_always_forward_and_on_grid(self, width, want):
        assert nxt(width) == want

    def test_tab_from_a_stop_advances_a_whole_level(self):
        assert nxt(0) == 4 and nxt(4) == 8 and nxt(8) == 12

    def test_tab_from_off_grid_snaps_onto_the_grid(self):
        # The bug: a fixed +4 took 6 to 10, leaving it off-grid forever.
        assert nxt(6) == 8
        assert nxt(5) == 8

    # -- Shift+Tab ---------------------------------------------------------

    @pytest.mark.parametrize("width,want", [(0, 0), (1, 0), (2, 0), (3, 0),
                                             (4, 0), (5, 4), (6, 4), (7, 4),
                                             (8, 4), (12, 8)])
    def test_prev_stop_is_always_backward_and_on_grid(self, width, want):
        assert prv(width) == want

    def test_unindent_from_a_stop_drops_a_whole_level(self):
        assert prv(8) == 4 and prv(4) == 0

    def test_unindent_from_off_grid_sheds_only_the_extra_spaces(self):
        # The bug: a fixed -4 took 6 to 2, skipping straight past the stop.
        assert prv(6) == 4
        assert prv(5) == 4

    def test_never_goes_negative(self):
        assert prv(0) == 0
        assert prv(1) == 0

    # -- the two are inverses on the grid ----------------------------------

    @pytest.mark.parametrize("width", [0, 4, 8, 12, 16])
    def test_round_trip_on_grid(self, width):
        assert prv(nxt(width)) == width

    @pytest.mark.parametrize("width", range(0, 17))
    def test_stops_are_always_multiples_of_the_indent_size(self, width):
        assert nxt(width) % 4 == 0
        assert prv(width) % 4 == 0

    @pytest.mark.parametrize("width", range(0, 17))
    def test_movement_is_strictly_monotonic(self, width):
        assert nxt(width) > width
        assert prv(width) < width or width == 0

    # -- other indent sizes ------------------------------------------------

    @pytest.mark.parametrize("size", [2, 3, 4, 8])
    def test_holds_for_any_indent_size(self, size):
        for w in range(0, 3 * size + 1):
            assert nxt(w, size) % size == 0
            assert prv(w, size) % size == 0
            assert nxt(w, size) > w
            assert prv(w, size) <= w

    def test_size_two(self):
        assert nxt(3, 2) == 4
        assert prv(3, 2) == 2
        assert prv(4, 2) == 2


class TestGuideColors:
    """The editor's indent and column guides are meant to be barely there.

    Reported: they were too bright in dark mode. The cause was hardcoded
    near-white pens (#E0E0E0 / #DDDDDD) -- 1.3:1 against a white page, but
    13.6:1 against the dark editor background, so ten times louder than the
    same lines are in light mode.

    The dark values were chosen to land at the SAME contrast ratio as the
    light ones rather than picked by eye, which is what this pins.
    """

    EDITOR_BG = {"light": "#FFFFFF", "dark": "#171717"}

    @staticmethod
    def _luminance(hex_color):
        h = hex_color.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    @classmethod
    def _contrast(cls, a, b):
        x, y = cls._luminance(a), cls._luminance(b)
        hi, lo = max(x, y), min(x, y)
        return (hi + 0.05) / (lo + 0.05)

    # The values ui_colors.guide_colors() returns for each theme. Duplicated
    # here deliberately: the point is to pin the RATIOS, and reading them
    # back from the function under test would pin nothing.
    LIGHT = ("#E0E0E0", "#DDDDDD")
    DARK = ("#2D2D2D", "#303030")

    def test_guides_are_subtle_in_both_themes(self):
        for theme, colors in (("light", self.LIGHT), ("dark", self.DARK)):
            for kind, color in zip(("indent", "column"), colors):
                r = self._contrast(color, self.EDITOR_BG[theme])
                assert 1.15 < r < 1.8, f"{theme} {kind} guide at {r:.2f}:1 is not subtle"

    def test_both_themes_carry_the_same_visual_weight(self):
        for i, kind in enumerate(("indent", "column")):
            light = self._contrast(self.LIGHT[i], self.EDITOR_BG["light"])
            dark = self._contrast(self.DARK[i], self.EDITOR_BG["dark"])
            assert abs(light - dark) < 0.15, (
                f"{kind} guide is {light:.2f}:1 in light but {dark:.2f}:1 in dark")

    def test_the_old_hardcoded_value_would_fail(self):
        # Guards the regression itself: #E0E0E0 on the dark editor
        # background is 13.6:1 -- what "too bright" actually measured.
        assert self._contrast("#E0E0E0", self.EDITOR_BG["dark"]) > 10

    def test_guide_colors_returns_a_pair_for_each_theme(self):
        from belfryscad.window import ui_colors
        got = ui_colors.guide_colors()
        assert isinstance(got, tuple) and len(got) == 2
        assert all(c.startswith("#") and len(c) == 7 for c in got)


class TestUnmatchedOpenBrackets:
    """Return auto-indents one level per bracket still open at the cursor.

    Replaces a heuristic that asked whether the line ENDED with an opener,
    or STARTED with `function`/`module`. That missed a line broken mid-call
    and fired on complete statements.

    Pure function, no Qt widget -- the key-event behaviour is verified
    separately with a throwaway script.
    """
    from belfryscad.window.editor import unmatched_open_brackets as _f
    f = staticmethod(_f)

    # -- the basic rule ----------------------------------------------------

    def test_an_open_brace_counts(self):
        assert self.f("module foo() {") == 1

    def test_matched_pairs_cancel(self):
        assert self.f("cube([1,2,3]);") == 0
        assert self.f("translate([0,0,0]) rotate([0,0,90]) cube(1);") == 0

    def test_nesting_counts_each_level(self):
        # The COUNT is per bracket; the Return handler deliberately indents
        # by only one level however many are open (see its own comment), so
        # a line opening several does not march off to the right.
        assert self.f("foo({") == 2
        assert self.f("a([{(") == 4

    # -- what the old rule got wrong ---------------------------------------

    def test_a_line_broken_mid_call_indents(self):
        # Did NOT end with an opener, so the old rule missed it.
        assert self.f("foo(a,") == 1
        assert self.f("x = [1, 2,") == 1

    def test_a_complete_statement_does_not_indent(self):
        # The old rule indented these because of their first word.
        assert self.f("function f(x) = x + 1;") == 0
        assert self.f("module foo();") == 0

    # -- closers belonging to an earlier line ------------------------------

    def test_else_on_the_same_line_still_indents(self):
        # The `}` closes a PREVIOUS line's block, so it must not cancel the
        # `{` that follows. Counting a net total would give 0 here.
        assert self.f("} else {") == 1

    def test_a_closing_line_does_not_indent(self):
        assert self.f("});") == 0
        assert self.f("}") == 0
        assert self.f("    ]);") == 0

    def test_a_stray_closer_never_goes_negative(self):
        assert self.f(")))") == 0
        assert self.f("}}}") == 0

    # -- brackets that are not brackets ------------------------------------

    def test_brackets_in_a_string_do_not_count(self):
        assert self.f('s = "a { brace";') == 0
        assert self.f('echo("[");') == 0

    def test_an_escaped_quote_does_not_end_the_string(self):
        assert self.f(r'echo("say \" {");') == 0

    def test_brackets_in_a_line_comment_do_not_count(self):
        assert self.f("cube(1); // trailing {") == 0
        assert self.f("// {[(") == 0

    def test_brackets_in_a_block_comment_do_not_count(self):
        assert self.f("cube(1); /* { */") == 0
        assert self.f("/* { */ foo() {") == 1

    def test_an_unterminated_block_comment_swallows_the_rest(self):
        assert self.f("cube(1); /* { ") == 0

    def test_a_division_is_not_a_comment(self):
        assert self.f("x = a / b; foo(") == 1

    # -- edges -------------------------------------------------------------

    def test_empty_and_whitespace(self):
        assert self.f("") == 0
        assert self.f("        ") == 0

    def test_mismatched_kinds_do_not_cancel(self):
        # `)` cannot close `[`, so the `[` stays open.
        assert self.f("foo[)") == 1


class TestReturnIndentsOneLevel:
    """Return adds ONE level when the line leaves any bracket open --
    never one per bracket.

    `unmatched_open_brackets` returns a count, but the Return handler only
    tests whether it is non-zero. A line opening several at once (`foo({`)
    therefore indents the same as one opening a single bracket, instead of
    marching off to the right.

    Pinned here as arithmetic rather than by driving key events, since
    widget instantiation crashes the pytest runner.
    """
    from belfryscad.window.editor import unmatched_open_brackets as _f
    f = staticmethod(_f)

    INDENT = 4

    def indent_after(self, line):
        """What the Return handler computes for `line`, at end of line."""
        base = len(line) - len(line.lstrip())
        return base + (self.INDENT if self.f(line) else 0)

    def test_one_open_bracket_adds_one_level(self):
        assert self.indent_after("module foo() {") == 4

    def test_several_open_brackets_still_add_only_one_level(self):
        assert self.indent_after("foo({") == 4
        assert self.indent_after("a([{(") == 4

    def test_it_adds_to_the_existing_indent(self):
        assert self.indent_after("    translate([0,0,0]) {") == 8
        assert self.indent_after("        foo(") == 12

    def test_a_balanced_line_keeps_its_indent(self):
        assert self.indent_after("    cube(10);") == 4
        assert self.indent_after("cube(10);") == 0

    def test_a_closing_line_keeps_its_indent(self):
        assert self.indent_after("    });") == 4
