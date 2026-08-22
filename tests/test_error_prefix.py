"""CLI errors carry exactly one `ERROR:` prefix.

Reported after seeing `ERROR: ERROR: Assertion 'false' failed ...`.

The prefix was added unconditionally, but only some messages arrive
without one. The evaluator's EvalError already reads "ERROR: Assertion
..."; a ParseError reads "Syntax error in ...". So the prefix is
genuinely wanted in one case and doubled in the other -- a blanket removal
would have stripped it from the parse errors that need it.
"""
import pytest

from belfryscad.headless import _print_error


def emitted(msg, capsys):
    _print_error(msg)
    return capsys.readouterr().err.rstrip("\n")


class TestErrorPrefix:
    def test_an_unprefixed_message_gains_one(self, capsys):
        assert emitted("Syntax error in foo.scad at line 1", capsys) == \
            "ERROR: Syntax error in foo.scad at line 1"

    def test_an_already_prefixed_message_is_left_alone(self, capsys):
        # The reported bug: this used to come out doubled.
        msg = "ERROR: Assertion 'false' failed: \"boom\" in file a.scad, line 1"
        assert emitted(msg, capsys) == msg

    def test_never_doubles(self, capsys):
        out = emitted("ERROR: something", capsys)
        assert out.count("ERROR:") == 1

    def test_it_goes_to_stderr_not_stdout(self, capsys):
        _print_error("boom")
        cap = capsys.readouterr()
        assert cap.out == ""
        assert "boom" in cap.err

    def test_an_exception_object_is_stringified(self, capsys):
        # Call sites pass the caught exception straight in.
        assert emitted(ValueError("bad thing"), capsys) == "ERROR: bad thing"

    def test_an_already_prefixed_exception_is_not_doubled(self, capsys):
        assert emitted(RuntimeError("ERROR: already said so"), capsys) == \
            "ERROR: already said so"

    def test_a_multiline_message_keeps_its_body(self, capsys):
        # Parse errors carry the offending source line beneath the headline;
        # only the first line is prefixed.
        out = emitted("Syntax error in a.scad at line 1:\ncube(\n     ^", capsys)
        assert out.startswith("ERROR: Syntax error")
        assert out.endswith("     ^")
        assert out.count("ERROR:") == 1

    def test_a_lowercase_error_word_is_still_prefixed(self, capsys):
        # Only the exact prefix counts -- a message merely mentioning
        # "error" still needs one.
        assert emitted("error opening file", capsys) == "ERROR: error opening file"

    def test_empty_message_still_gets_a_prefix(self, capsys):
        # Degenerate input that never actually occurs; the exact trailing
        # whitespace is not worth pinning, only that it does not crash and
        # still says ERROR once.
        out = emitted("", capsys)
        assert out.startswith("ERROR:") and out.count("ERROR:") == 1


class TestCallSitesUseIt:
    def test_no_bare_error_prints_remain(self):
        import inspect
        from belfryscad import headless
        src = inspect.getsource(headless)
        # Everything but the helper's own construction must go through it.
        bare = [ln.strip() for ln in src.splitlines()
                if 'print(f"ERROR:' in ln or 'print("ERROR:' in ln]
        assert bare == [], f"error printed without the helper: {bare}"
