"""Right-click navigation on an OpenSCAD builtin (issue #525).

`is_num()` is a builtin, so it has no `.scad` definition to jump to and
BOSL2 will never document it -- both lookups were correct to find nothing.
What was wrong is that they said "not found" instead of "this is a builtin,
here is the language reference", which reads as a dead menu item.

The name sets these consult already existed, for syntax highlighting.
"""
import pytest

from belfryscad.window.editor import CodeEditor


# --- #525: builtins are answered, not reported missing ---------------------

class TestBuiltinDefinitionLookup:
    """A builtin has no .scad definition anywhere, so "no definition found"
    is a true statement that reads as a broken feature. The editor already
    knew which names are builtins -- it highlights them -- it just never
    asked before giving up."""

    def test_the_reported_name_is_recognised(self):
        # is_num() is an OpenSCAD builtin, which is why neither Go to
        # Definition nor the BOSL2 lookup could ever find it.
        assert CodeEditor.builtin_kind("is_num") == "function"

    @pytest.mark.parametrize("word,kind", [
        ("cube", "module"), ("translate", "module"), ("hull", "module"),
        ("abs", "function"), ("is_num", "function"), ("search", "function"),
        ("let", "keyword"), ("for", "keyword"),
        ("PI", "constant"),
        ("$fn", "special variable"), ("$t", "special variable"),
    ])
    def test_each_builtin_category(self, word, kind):
        assert CodeEditor.builtin_kind(word) == kind

    @pytest.mark.parametrize("word", ["my_widget", "bosl2_thing", "x", "anchor"])
    def test_user_names_are_not_builtins(self, word):
        """Anything not a builtin must still go to the real definition
        lookup -- this must not swallow user symbols."""
        assert CodeEditor.builtin_kind(word) is None

    def test_manual_url_anchors_on_the_name(self):
        url = CodeEditor.openscad_manual_url("cylinder")
        assert url.endswith("#cylinder")
        assert url.startswith(CodeEditor.OPENSCAD_MANUAL_URL)

    def test_dollar_names_get_the_page_without_an_anchor(self):
        """Wikibooks mangles `$` in heading ids, and the special variables
        are documented together rather than one section each, so an anchor
        would land nowhere."""
        assert CodeEditor.openscad_manual_url("$fn") == CodeEditor.OPENSCAD_MANUAL_URL

    def test_every_highlighted_builtin_classifies(self):
        """The highlighter's word list and the lookup must not drift: any
        name the editor colours as a builtin has to be one here too."""
        for word in CodeEditor._BUILTIN_WORDS:
            assert CodeEditor.builtin_kind(word) is not None, word
