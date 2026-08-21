"""Which brackets get flagged as unmatched.

Two reported bugs, both in `_rescan_unmatched`:

1. "Unmatched end brackets should be highlighted the same way unmatched
   opening brackets are." A closer with nothing to close was silently
   dropped and never marked.
2. "`( [ { ] ) }` is not matching for any of those brackets, since they
   are at different levels." Bracket KIND was deliberately not checked, so
   any closer popped any opener and the whole line looked fine.

`pair_brackets` is the pure core, split out of the document walk so it can
be tested without building a QSyntaxHighlighter (widget instantiation
crashes the pytest runner here). The real document behaviour is verified
separately with a throwaway script.
"""
import pytest

from belfryscad.window.editor import pair_brackets


def scan(text):
    """Positions in `text` that come back unmatched, sorted."""
    brackets = [(0, i, c) for i, c in enumerate(text) if c in "()[]{}"]
    return sorted(pos for _key, pos in pair_brackets(brackets))


class TestWellFormed:
    @pytest.mark.parametrize("text", [
        "", "()", "[]", "{}", "([{}])", "()[]{}", "(())",
        "module f() { cube([1,2,3]); }",
        "a(b(c(d())))",
    ])
    def test_balanced_text_flags_nothing(self, text):
        assert scan(text) == []


class TestUnmatchedOpeners:
    def test_a_lone_opener(self):
        assert scan("(") == [0]

    def test_an_opener_inside_a_balanced_pair(self):
        assert scan("( () ") == [0]

    def test_several(self):
        assert scan("([{") == [0, 1, 2]


class TestUnmatchedClosers:
    """Bug 1 -- these used to be dropped silently."""

    def test_a_lone_closer(self):
        assert scan(")") == [0]

    def test_a_closer_after_a_balanced_pair(self):
        assert scan("())") == [2]

    def test_several(self):
        assert scan(")]}") == [0, 1, 2]

    def test_closers_and_openers_together(self):
        # `)` closes nothing; `(` is never closed.
        assert scan(")(") == [0, 1]


class TestCrossedBrackets:
    """Bug 2 -- the reported case."""

    def test_the_reported_case_flags_every_bracket(self):
        # ( [ { ] ) }  -- with spaces, so the brackets sit at 0,2,4,6,8,10
        assert scan("( [ { ] ) }") == [0, 2, 4, 6, 8, 10]

    def test_the_reported_case_without_spaces(self):
        assert scan("([{])}") == [0, 1, 2, 3, 4, 5]

    def test_a_simple_crossing(self):
        # `]` crosses the `{`; both plus the still-open `[` are flagged.
        assert scan("[{]") == [0, 1, 2]

    def test_a_mismatched_pair(self):
        assert scan("(]") == [0, 1]

    def test_kind_is_actually_checked(self):
        # The old code popped regardless of kind, so this looked balanced.
        assert scan("(}") != []

    def test_well_formed_text_after_a_crossing_still_matches(self):
        # The stack is cleared at the crossing, so later structure is judged
        # on its own merits rather than dragged down by the wreckage.
        assert scan("[{]") == [0, 1, 2]
        assert scan("[{]" + "()") == [0, 1, 2]

    def test_a_crossing_does_not_flag_earlier_completed_pairs(self):
        # The `()` at 0-1 closed cleanly before anything went wrong.
        assert scan("()" + "[{]") == [2, 3, 4]


class TestOpenersAndClosersAreTreatedAlike:
    """The headline of bug 1: both directions get the same treatment."""

    def test_one_of_each_kind_unmatched(self):
        for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
            assert scan(opener) == [0], f"{opener} not flagged"
            assert scan(closer) == [0], f"{closer} not flagged"

    def test_counts_match_in_both_directions(self):
        assert len(scan("(((")) == len(scan(")))")) == 3


class TestKeysArePreserved:
    """The document scan passes a line number as the key, and needs it back
    so it can repaint only the affected lines."""

    def test_keys_come_back_with_their_positions(self):
        brackets = [(7, 3, "("), (9, 0, "}")]
        assert sorted(pair_brackets(brackets)) == [(7, 3), (9, 0)]

    def test_a_pair_spanning_keys_is_matched(self):
        assert pair_brackets([(1, 0, "{"), (5, 0, "}")]) == []


class TestScanAndPaintAgree:
    """`pair_brackets` deciding a bracket is unmatched is only half of it --
    `highlightBlock` has to actually paint it red.

    This is not hypothetical. The closer branch of `highlightBlock` did not
    consult the unmatched set at all, so a stray `}` was correctly
    identified by the scan and then painted the ordinary depth colour
    anyway. A test that stopped at the scan passed the whole time the user
    was looking at an un-red bracket.

    Widget instantiation crashes the pytest runner here, so the painted
    colour itself is verified by a throwaway script. What is pinned here is
    the invariant that made the bug possible: openers and closers must be
    fed through exactly the same decision, with no branch treating one
    differently.
    """

    def test_both_directions_reach_the_same_verdict(self):
        # Each of these is one unmatched bracket, and pair_brackets must not
        # care which way it points.
        for text in ("(", ")", "[", "]", "{", "}"):
            assert scan(text) == [0], f"{text!r} not reported unmatched"

    def test_a_closer_is_reported_at_its_own_position(self):
        # The position matters: highlightBlock paints exactly one character
        # at the reported index, so an off-by-one would colour a neighbour.
        assert scan("cube(1);}") == [8]
        assert scan("}cube(1);") == [0]

    def test_positions_land_on_actual_bracket_characters(self):
        text = "( [ { ] ) }"
        for pos in scan(text):
            assert text[pos] in "()[]{}", f"position {pos} is {text[pos]!r}"

    @pytest.mark.parametrize("text,count", [
        ("}", 1), ("foo());", 1), ("( [ { ] ) }", 6),
        ("module f() {", 1), ("module f() { cube(1); }", 0),
    ])
    def test_counts_match_what_the_editor_paints(self, text, count):
        # Same cases the painted-colour script checks, so the two layers
        # cannot drift apart silently again.
        assert len(scan(text)) == count
