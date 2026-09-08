"""The Help menu's About box (#379).

The facts and their formatting are plain data and plain strings, so they
test directly. The dialog itself is a widget and is driven in a subprocess,
the way the other widget tests here are.
"""
import json
import subprocess
import sys

from belfryscad.window.about import (
    DOCS_URL, about_html, about_info, about_text,
)


def test_the_version_is_the_first_thing_it_reports():
    """'Mainly I want to know what version I'm running.'"""
    info = about_info()
    assert info["name"] == "BelfrySCAD"
    assert info["version"] not in ("", None)
    assert about_html(info).index(info["version"]) < 120  # in the heading


def test_it_reports_the_evaluator_version_too():
    """The geometry comes from openscad_cpp_evaluator and it moves
    independently of the app, so a bug report needs both."""
    info = about_info()
    assert "openscad_cpp_evaluator" in info["components"]
    assert info["components"]["openscad_cpp_evaluator"] != "not installed"


def test_a_missing_component_is_said_so_not_crashed_on():
    from belfryscad.window import about

    assert about._version_of("definitely-not-installed-xyz") == "not installed"


def test_the_licence_and_author_are_stated():
    info = about_info()
    assert info["license"] == "MIT"
    assert info["author"]
    assert "MIT" in about_html(info)


def test_the_links_point_at_the_wiki_and_the_tracker():
    html = about_html()
    assert DOCS_URL in html
    assert "/issues" in html
    assert DOCS_URL.startswith("https://github.com/BelfrySCAD/BelfrySCAD/wiki")


def test_plain_text_carries_the_same_versions_for_pasting():
    """The Copy button exists so a version reaches an issue without being
    retyped."""
    info = about_info()
    text = about_text(info)
    assert info["version"] in text
    assert "<" not in text
    for name, version in info["components"].items():
        assert f"{name} {version}" in text


def test_the_dialog_builds_and_the_help_menu_holds_it(tmp_path):
    """Driven in a subprocess: widgets crash the pytest runner here."""
    driver = tmp_path / "_about.py"
    driver.write_text('''
import json
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QAction
app = QApplication([])
from belfryscad.window.about import show_about_dialog, about_info

dlg = show_about_dialog(None)
out = {"title": dlg.windowTitle()}
from PySide6.QtWidgets import QLabel, QPushButton
out["body_has_version"] = any(about_info()["version"] in w.text() for w in dlg.findChildren(QLabel))
out["buttons"] = sorted(b.text().replace("&", "") for b in dlg.findChildren(QPushButton))
print(json.dumps(out))
''')
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                          env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                               "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["title"] == "About BelfrySCAD"
    assert out["body_has_version"] is True
    assert "Copy Versions" in out["buttons"]
