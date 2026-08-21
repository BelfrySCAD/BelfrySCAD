"""`$export_name` — the script-controllable default filename for Export.

Seeded from the input file's basename before the script runs, assignable by
the script, and used (sanitised) as the Export dialog's default.

Needs no evaluator change: `viewport_params` seeds arbitrary `$`-names (the
name is historical, not a restriction) and `Evaluator.dyn` hands every
`$`-variable back afterwards. These cover the pure helpers; the round trip
through a real evaluation is exercised in TestRoundTrip below.
"""
import pytest

from belfryscad.export_name import (
    UNTITLED, default_export_name, resolve_export_name, sanitize_export_name,
    seed_params,
)


class TestDefaultFromPath:
    @pytest.mark.parametrize("path,want", [
        ("/a/b/widget.scad", "widget"),
        ("plate.scad", "plate"),
        ("/x/no_extension", "no_extension"),
        ("/x/dots.in.name.scad", "dots.in.name"),
    ])
    def test_basename_without_extension(self, path, want):
        assert default_export_name(path) == want

    @pytest.mark.parametrize("path", [None, "", 0])
    def test_an_unsaved_buffer_is_untitled(self, path):
        assert default_export_name(path) == UNTITLED

    def test_a_dotfile_keeps_its_name(self):
        # splitext treats a leading dot as the name, not an extension.
        assert default_export_name("/x/.hidden") == ".hidden"

    def test_a_path_object_works(self):
        from pathlib import Path
        assert default_export_name(Path("/a/b/widget.scad")) == "widget"


class TestSanitize:
    """Valid: letters, digits, underscore, dash, plus, period. All else
    becomes an underscore -- one per character, so length is preserved and
    `a//b` reads as `a__b` rather than collapsing."""

    def test_valid_characters_survive_untouched(self):
        assert sanitize_export_name("ok_name-1.2+3") == "ok_name-1.2+3"
        assert sanitize_export_name("ABCxyz089") == "ABCxyz089"

    @pytest.mark.parametrize("raw,want", [
        ("spaces here", "spaces_here"),
        ("sl/ash", "sl_ash"),
        ("a:b*c?d", "a_b_c_d"),
        ("Lid / Rev 3 (final)", "Lid___Rev_3__final_"),
    ])
    def test_invalid_characters_become_underscores(self, raw, want):
        assert sanitize_export_name(raw) == want

    def test_one_underscore_per_character_not_per_run(self):
        assert sanitize_export_name("a///b") == "a___b"

    def test_path_separators_cannot_escape(self):
        # The result must be a plain filename, never a traversal.
        got = sanitize_export_name("../../etc/passwd")
        assert "/" not in got and "\\" not in got
        assert got == ".._.._etc_passwd"

    def test_non_ascii_is_replaced(self):
        assert sanitize_export_name("Ünïcödé") == "_n_c_d_"

    def test_empty_stays_empty(self):
        # "" means "no opinion"; callers fall back to their own default.
        assert sanitize_export_name("") == ""
        assert sanitize_export_name(None) == ""

    def test_a_number_is_stringified(self):
        # A script may assign $export_name = 42. OpenSCAD has one number
        # type, so a whole number must not come back as "42.0".
        assert sanitize_export_name(42.0) == "42"
        assert sanitize_export_name(42) == "42"
        assert sanitize_export_name(3.5) == "3.5"

    def test_a_bool_is_stringified_rather_than_rejected(self):
        assert sanitize_export_name(True) == "True"


class TestResolve:
    def test_a_usable_name_is_kept(self):
        assert resolve_export_name("My Part", "/a/widget.scad") == "My_Part"

    @pytest.mark.parametrize("value", ["", None])
    def test_nothing_usable_falls_back_to_the_file(self, value):
        assert resolve_export_name(value, "/a/widget.scad") == "widget"

    def test_fallback_of_the_fallback_is_untitled(self):
        assert resolve_export_name("", None) == UNTITLED


class TestSeedParams:
    def test_it_adds_the_name(self):
        assert seed_params({}, "/a/widget.scad")["$export_name"] == "widget"

    def test_it_does_not_mutate_the_caller_s_dict(self):
        base = {"$t": 0.0}
        seed_params(base, "/a/widget.scad")
        assert base == {"$t": 0.0}

    def test_it_keeps_other_params(self):
        out = seed_params({"$t": 0.5, "$vpd": 100}, "/a/w.scad")
        assert out["$t"] == 0.5 and out["$vpd"] == 100

    def test_an_explicit_value_wins(self):
        # setdefault, not assignment -- a caller that already decided keeps
        # its own value.
        assert seed_params({"$export_name": "given"}, "/a/w.scad")["$export_name"] == "given"

    def test_none_params_is_accepted(self):
        assert seed_params(None, "/a/w.scad")["$export_name"] == "w"


class TestRoundTrip:
    """Seed -> script -> read back, through a real evaluation."""

    @staticmethod
    def _run(tmp_path, body, name="model.scad"):
        from openscad_cpp_evaluator import Evaluator
        src = tmp_path / name
        src.write_text(body)
        ev = Evaluator(echo_fn=lambda _m: None)
        ev.evaluate(str(src), seed_params({}, str(src)))
        return resolve_export_name(ev.dyn.get("$export_name"), str(src))

    def test_a_script_that_leaves_it_alone_gets_the_filename(self, tmp_path):
        assert self._run(tmp_path, "cube(1);") == "model"

    def test_a_script_can_read_it(self, tmp_path):
        seen = []
        from openscad_cpp_evaluator import Evaluator
        src = tmp_path / "widget.scad"
        src.write_text('echo($export_name);\ncube(1);')
        ev = Evaluator(echo_fn=seen.append)
        ev.evaluate(str(src), seed_params({}, str(src)))
        assert any("widget" in m for m in seen)

    def test_a_script_can_change_it(self, tmp_path):
        assert self._run(tmp_path, '$export_name = "custom";\ncube(1);') == "custom"

    def test_the_change_is_sanitised(self, tmp_path):
        assert self._run(tmp_path, '$export_name = "Lid / Rev 3";\ncube(1);') == "Lid___Rev_3"

    def test_a_computed_name_works(self, tmp_path):
        body = 'w=30; h=20;\n$export_name = str("plate_", w, "x", h);\ncube(1);'
        assert self._run(tmp_path, body) == "plate_30x20"

    def test_blanking_it_falls_back_to_the_filename(self, tmp_path):
        assert self._run(tmp_path, '$export_name = "";\ncube(1);') == "model"


class TestGridDefaultsOff:
    """The XY grid starts hidden; Ctrl+G brings it up.

    Two independent defaults have to agree: `SceneRenderer.show_grid` and
    the "Show Grid" menu item's initial checked state. `_add_checkable`
    sets its checkbox BEFORE connecting the toggle slot, so the menu's
    initial value never reaches the renderer -- a mismatch would show an
    unchecked menu item sitting over a visible grid, with the first Ctrl+G
    appearing to do nothing.

    Read out of the source rather than by constructing a renderer, which
    would need a GL context.
    """
    import pathlib
    ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "belfryscad"

    def _renderer_default(self):
        import re
        text = (self.ROOT / "engine" / "renderer.py").read_text()
        m = re.search(r"self\.show_grid:\s*bool\s*=\s*(True|False)", text)
        assert m, "show_grid default not found"
        return m.group(1) == "True"

    def _menu_default(self):
        import re
        text = (self.ROOT / "window" / "main_window.py").read_text()
        m = re.search(r'_add_checkable\(view_menu,\s*"Show Grid",\s*(True|False)', text)
        assert m, '"Show Grid" menu default not found'
        return m.group(1) == "True"

    def test_the_renderer_starts_with_the_grid_off(self):
        assert self._renderer_default() is False

    def test_the_menu_item_starts_unchecked(self):
        assert self._menu_default() is False

    def test_the_two_defaults_agree(self):
        # The invariant that actually matters -- either both on or both off.
        assert self._renderer_default() == self._menu_default()
