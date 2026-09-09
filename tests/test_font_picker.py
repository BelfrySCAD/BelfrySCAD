"""The font= picker (#384).

The finders and the spec arithmetic are plain functions over source text
-- no Qt, no parser -- and test directly. The dialog itself is driven in a
subprocess, as the other widget tests here are.
"""
import json
import subprocess
import sys

import pytest

from belfryscad.window.customizer import _is_font_parameter
from belfryscad.window.font_picker import (
    DEFAULT_PREVIEW, find_font_argument, find_preview_text, group_by_family, parse_spec, spec_for,
)


SRC = 'text("Hello", size=10, font="Liberation Sans:style=Bold");\n'


class TestFindFontArgument:
    def test_finds_the_spec_and_spans_the_quotes(self):
        at = SRC.index("Liberation")
        start, end, spec = find_font_argument(SRC, at)
        assert spec == "Liberation Sans:style=Bold"
        # The span includes both quotes, so a commit replaces the literal
        # and leaves `font=` alone.
        assert SRC[start:end] == '"Liberation Sans:style=Bold"'

    @pytest.mark.parametrize("needle", ["font=", "font", '"Liberation'])
    def test_matches_from_anywhere_in_the_argument(self, needle):
        assert find_font_argument(SRC, SRC.index(needle)) is not None

    def test_ignores_a_different_argument_ending_in_font(self):
        src = 'foo(subfont="Nope");'
        assert find_font_argument(src, src.index("Nope")) is None

    def test_no_match_away_from_any_font_argument(self):
        assert find_font_argument(SRC, 0) is None

    def test_handles_an_escaped_quote(self):
        src = 'text("x", font="Odd\\"Name");'
        start, end, spec = find_font_argument(src, src.index("Odd"))
        assert spec == 'Odd"Name'


class TestPreviewText:
    def test_uses_the_calls_own_string(self):
        assert find_preview_text(SRC, SRC.index("font")) == "Hello"

    def test_named_text_argument_counts(self):
        src = 'text(text="Label", font="X");'
        assert find_preview_text(src, src.index("font")) == "Label"

    def test_a_variable_text_has_no_literal_to_show(self):
        """Falls back to the pangram rather than showing a font= spec as
        if it were the label."""
        src = 'text(msg, font="X");'
        assert find_preview_text(src, src.index("font")) is None

    def test_outside_a_text_call_there_is_nothing_to_preview(self):
        src = 'my_font = "Liberation Sans";'
        assert find_preview_text(src, src.index("Liberation")) is None


class TestSpecs:
    @pytest.mark.parametrize("spec,family,style", [
        ("Liberation Sans", "Liberation Sans", ""),
        ("Liberation Sans:style=Bold", "Liberation Sans", "Bold"),
        ("Helvetica Neue:style=Condensed Black", "Helvetica Neue", "Condensed Black"),
        ("Liberation Sans:Bold", "Liberation Sans", "Bold"),      # fontconfig's bare form
        ("Liberation Sans:size=12", "Liberation Sans", ""),       # other properties ignored
    ])
    def test_parse(self, spec, family, style):
        assert parse_spec(spec) == (family, style)

    def test_regular_is_written_as_the_bare_family(self):
        assert spec_for("Liberation Sans", "Regular") == "Liberation Sans"
        assert spec_for("Liberation Sans", "") == "Liberation Sans"
        assert spec_for("Liberation Sans", "Bold") == "Liberation Sans:style=Bold"

    def test_round_trip(self):
        for spec in ("Liberation Sans", "Helvetica Neue:style=Thin Italic"):
            assert spec_for(*parse_spec(spec)) == spec


class TestGrouping:
    FONTS = [
        {"family": "B Family", "style": "Bold", "spec": "", "path": ""},
        {"family": "B Family", "style": "Regular", "spec": "", "path": ""},
        {"family": "a family", "style": "Thin", "spec": "", "path": ""},
        {"family": "B Family", "style": "Condensed Black", "spec": "", "path": ""},
    ]

    def test_families_sort_case_insensitively(self):
        assert list(group_by_family(self.FONTS)) == ["a family", "B Family"]

    def test_regular_leads_then_alphabetical(self):
        """Regular is the default, so it goes first; everything else is
        whatever the family actually has -- not a curated Bold/Italic pair,
        which would make Condensed Black unreachable."""
        assert group_by_family(self.FONTS)["B Family"] == ["Regular", "Bold", "Condensed Black"]

    def test_a_face_with_no_style_counts_as_regular(self):
        assert group_by_family([{"family": "X", "style": "", "spec": "", "path": ""}]) == {"X": ["Regular"]}


class TestCustomizerDetection:
    @pytest.mark.parametrize("name", ["font", "Font", "$font", "fontname", "title_font", "font_face"])
    def test_font_parameters_get_the_picker(self, name):
        assert _is_font_parameter(name)

    @pytest.mark.parametrize("name", ["size", "fontsize", "label", "confont_size"])
    def test_others_do_not(self, name):
        assert not _is_font_parameter(name)


def test_the_dialog_lists_styles_per_family_and_saves_a_spec(tmp_path):
    driver = tmp_path / "_fp.py"
    driver.write_text('''
import json
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.window.font_picker import FontPickerDialog

FONTS = [
    {"family": "Liberation Sans", "style": "Regular", "spec": "Liberation Sans", "path": "<bundled>"},
    {"family": "Liberation Sans", "style": "Bold", "spec": "Liberation Sans:style=Bold", "path": "<bundled>"},
    {"family": "Helvetica Neue", "style": "Thin", "spec": "Helvetica Neue:style=Thin", "path": "<bundled>"},
    {"family": "Helvetica Neue", "style": "Regular", "spec": "Helvetica Neue", "path": "<bundled>"},
]
dlg = FontPickerDialog("Helvetica Neue:style=Thin", "Hello", None, fonts=FONTS)
out = {"title": dlg.windowTitle()}

from PySide6.QtWidgets import QListWidget, QLineEdit, QLabel
fam_list, style_list = dlg.findChildren(QListWidget)[:2]
out["opened_family"] = fam_list.currentItem().text()
out["opened_style"] = style_list.currentItem().text()
out["styles_for_helvetica"] = [style_list.item(i).text() for i in range(style_list.count())]
out["spec_at_open"] = dlg.chosen_spec()

# Switching family reloads only that family's styles.
fam_list.setCurrentRow([fam_list.item(i).text() for i in range(fam_list.count())].index("Liberation Sans"))
out["styles_for_liberation"] = [style_list.item(i).text() for i in range(style_list.count())]
out["spec_after_switch"] = dlg.chosen_spec()

# Regular selected -> bare family, no :style=Regular.
style_list.setCurrentRow([style_list.item(i).text() for i in range(style_list.count())].index("Regular"))
out["spec_regular"] = dlg.chosen_spec()

from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtGui import QFontInfo
edit = dlg.findChild(QPlainTextEdit)
# The preview IS the editor -- you type into the font, there is no
# separate sample label.
out["preview_seeded"] = edit.toPlainText() == "Hello"
out["preview_is_editable"] = not edit.isReadOnly()
out["label_count"] = len([w for w in dlg.findChildren(QLabel) if w.text().startswith("The quick")])

# The preview font must FOLLOW the style selection. Asserted on the
# REQUESTED font rather than QFontInfo's resolved one: what a given
# platform substitutes for an absent family is not this dialog's
# business, but asking for the right face is.
requested = []
for row in range(style_list.count()):
    style_list.setCurrentRow(row)
    requested.append(edit.font().styleName())
out["requested_styles"] = requested
out["style_names"] = [style_list.item(i).text() for i in range(style_list.count())]
print(json.dumps(out))
''')
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                          env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                               "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["title"] == "Choose Font"
    # Opens on the spec it was given.
    assert out["opened_family"] == "Helvetica Neue"
    assert out["opened_style"] == "Thin"
    assert out["spec_at_open"] == "Helvetica Neue:style=Thin"
    # Each family offers its OWN styles, not a fixed set.
    assert out["styles_for_helvetica"] == ["Regular", "Thin"]
    assert out["styles_for_liberation"] == ["Regular", "Bold"]
    assert out["spec_after_switch"].startswith("Liberation Sans")
    assert out["spec_regular"] == "Liberation Sans"
    assert out["preview_seeded"] is True
    assert out["preview_is_editable"] is True
    # No leftover pangram label: the editor is the preview.
    assert out["label_count"] == 0
    # Selecting a style asks for THAT face. Without setStyleName every
    # style of a family rendered identically as Regular, which is what
    # was reported.
    assert out["requested_styles"] == out["style_names"]


def test_saving_commits_the_spec_after_exec_returns(tmp_path):
    """The crash this test exists for: Save raised

        RuntimeError: Internal C++ object (QListWidget) already deleted

    because the dialog carried WA_DeleteOnClose and the caller read the
    selection back AFTER exec() returned -- by which time the widgets were
    gone. Found by running the built app, not by any test that only ever
    touched a live dialog.
    """
    driver = tmp_path / "_fpsave.py"
    driver.write_text('''
import json
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QListWidget
app = QApplication([])
from belfryscad.window.font_picker import FontPickerDialog, open_font_picker
import belfryscad.window.font_picker as fp

FONTS = [
    {"family": "Liberation Sans", "style": "Regular", "spec": "Liberation Sans", "path": "<bundled>"},
    {"family": "Liberation Sans", "style": "Bold", "spec": "Liberation Sans:style=Bold", "path": "<bundled>"},
]
# open_font_picker builds its own dialog; force it to use the fake list.
real = FontPickerDialog.__new__
fp.FontPickerDialog = lambda spec, preview, parent: real(FontPickerDialog, spec, preview, parent, fonts=FONTS)

out = {}
committed = []
# exec() blocks, so the click has to come from the event loop.
def press_save():
    for dlg in app.topLevelWidgets():
        if dlg.windowTitle() == "Choose Font":
            dlg.findChildren(QListWidget)[1].setCurrentRow(1)   # Bold
            for b in dlg.findChildren(type(dlg).__bases__[0]):
                pass
            from PySide6.QtWidgets import QDialogButtonBox
            box = dlg.findChild(QDialogButtonBox)
            box.button(QDialogButtonBox.StandardButton.Save).click()
            return
QTimer.singleShot(0, press_save)
try:
    fp.open_font_picker("Liberation Sans", "Hi", committed.append, None)
    out["committed"] = committed
    out["error"] = None
except Exception as e:
    out["committed"] = committed
    out["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
''')
    import subprocess, sys, json
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                          env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                               "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["error"] is None, out["error"]
    assert out["committed"] == ["Liberation Sans:style=Bold"]
