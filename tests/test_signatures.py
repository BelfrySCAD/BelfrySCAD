"""Argument hints for the editor (issue #517).

The reporter rated this above completion itself: typing `(` should say what
the arguments are, "to save me from having to view documentation on
syntax". Name completion already worked; this is the part that did not
exist.

Qt-free -- the lookup and the declaration parsing are what can be wrong.
"""
import pytest

from belfryscad.window.signatures import (
    BUILTIN_SIGNATURES, signature_for, signature_from_source,
)


class TestBuiltins:
    def test_the_example_the_reporter_named(self):
        """'especially useful for modules with long argument lists, such as
        text()'."""
        sig = BUILTIN_SIGNATURES["text"]
        for arg in ("size", "font", "halign", "valign", "spacing", "direction"):
            assert arg in sig

    @pytest.mark.parametrize("name", ["cube", "cylinder", "linear_extrude", "rotate", "search"])
    def test_common_builtins_are_covered(self, name):
        assert signature_for(name) == BUILTIN_SIGNATURES[name]

    def test_later_release_builtins_are_covered(self):
        """The request asks specifically for textmetrics/fontmetrics."""
        assert "textmetrics" in BUILTIN_SIGNATURES
        assert "fontmetrics" in BUILTIN_SIGNATURES

    def test_every_signature_starts_with_its_own_name(self):
        for name, sig in BUILTIN_SIGNATURES.items():
            assert sig.startswith(name + "("), (name, sig)

    def test_unknown_name_is_silent(self):
        """A tooltip on every stray paren would be worse than none."""
        assert signature_for("no_such_thing") is None


class TestDeclarationParsing:
    def test_module_with_defaults(self):
        src = "module widget(size=10, spin=0, anchor, $fn=32) { cube(size); }"
        assert signature_from_source(src, "widget", 1) == \
            "module widget(size=10, spin=0, anchor, $fn=32)"

    def test_function(self):
        src = "function myfn(a, b=2, c=[1,2]) = a+b;"
        assert signature_from_source(src, "myfn", 1) == "function myfn(a, b=2, c=[1,2])"

    def test_multi_line_declaration_is_joined(self):
        """BOSL2 routinely puts one parameter per line -- cuboid() spans a
        dozen. Reading only the line the scope points at would truncate
        almost every library signature."""
        src = "module big(\n  one,\n  two=2,\n  three=[1,2]\n) { }"
        assert signature_from_source(src, "big", 1) == "module big(one, two=2, three=[1,2])"

    def test_a_paren_inside_a_string_does_not_end_the_list(self):
        src = 'module gizmo(a, b=3, c="x)y") { }'
        assert signature_from_source(src, "gizmo", 1) == 'module gizmo(a, b=3, c="x)y")'

    def test_comments_are_stripped(self):
        src = "module m(\n  a,   // the first\n  b=2  /* and */\n) { }"
        assert signature_from_source(src, "m", 1) == "module m(a, b=2)"

    def test_a_different_name_on_that_line_is_not_matched(self):
        src = "module other(x) { }"
        assert signature_from_source(src, "wanted", 1) is None

    def test_line_out_of_range(self):
        assert signature_from_source("module m(x) { }", "m", 99) is None

    def test_unbalanced_declaration_gives_up(self):
        """Half-typed code must not hang or return nonsense."""
        assert signature_from_source("module broken(a, b", "broken", 1) is None


class TestLookupOrder:
    class _Decl:
        def __init__(self, origin, line):
            self.position = type("P", (), {"origin": origin, "line": line})()

    class _Scope:
        def __init__(self, modules=None, functions=None):
            self.modules = modules or {}
            self.functions = functions or {}

    def test_builtin_wins_over_a_user_redefinition(self):
        """The hint is for the name being typed, and the builtin table is
        the answer the language guarantees."""
        scope = self._Scope(modules={"cube": self._Decl("/nope.scad", 1)})
        assert signature_for("cube", scope) == BUILTIN_SIGNATURES["cube"]

    def test_live_buffer_is_preferred_over_disk(self, tmp_path):
        """Code in the buffer being edited was parsed from a temp copy, so
        its declaration must be read from the live text -- otherwise a hint
        is only right after the next save."""
        f = tmp_path / "parsed.scad"
        f.write_text("module w(old_param) { }")
        scope = self._Scope(modules={"w": self._Decl(str(f), 1)})
        live = "module w(new_param=7) { }"
        assert signature_for("w", scope, live_text=live, live_origin=str(f)) == \
            "module w(new_param=7)"

    def test_falls_back_to_disk_for_another_file(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text("module libthing(a, b=1) { }")
        scope = self._Scope(modules={"libthing": self._Decl(str(f), 1)})
        assert signature_for("libthing", scope, live_text="unrelated",
                             live_origin="/somewhere/else.scad") == \
            "module libthing(a, b=1)"

    def test_missing_file_is_silent(self):
        scope = self._Scope(functions={"gone": self._Decl("/no/such/file.scad", 1)})
        assert signature_for("gone", scope) is None

    def test_no_scope_still_answers_builtins(self):
        assert signature_for("sphere", None) == BUILTIN_SIGNATURES["sphere"]
