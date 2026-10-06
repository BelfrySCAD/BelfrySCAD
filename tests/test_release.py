"""release.py's date belongs to its version, and the two move together.

The date is set by hand in the release bump commit. Tying it to
pyproject.toml's version is what stops a release going out still showing
the previous one's date: bump the version alone and this fails.
"""
import datetime
import tomllib
from pathlib import Path

from belfryscad import release


def test_the_date_belongs_to_the_current_version():
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert release.VERSION == pyproject["project"]["version"], (
        "pyproject.toml's version changed: set release.VERSION and release.DATE "
        "in src/belfryscad/release.py to the new release")


def test_the_date_is_a_real_iso_date():
    datetime.date.fromisoformat(release.DATE)


def test_about_reports_it():
    from belfryscad.window.about import about_html, about_info, about_text

    info = about_info()
    assert info["released"] == release.DATE
    assert release.DATE in about_html(info) and release.DATE in about_text(info)


def test_the_metainfo_lists_this_release_first():
    # Software centres show the newest <release> as "what's new"; one left
    # behind advertises the previous version.
    import xml.etree.ElementTree as ET
    root = ET.parse(Path(__file__).parent.parent / "resources" / "com.belfrydw.belfryscad.metainfo.xml").getroot()
    newest = root.find("releases/release")
    assert (newest.get("version"), newest.get("date")) == (release.VERSION, release.DATE), (
        "add a <release> for this version at the top of <releases> in "
        "resources/com.belfrydw.belfryscad.metainfo.xml")
