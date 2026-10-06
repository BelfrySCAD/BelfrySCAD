"""The Linux desktop files agree with each other and with the app.

The desktop file, the MIME definition and the AppStream MetaInfo are
separate files read by different parts of the desktop; a name that drifts
in one of them silently breaks file association, the dock icon, or the
software-centre listing."""
import configparser
import xml.etree.ElementTree as ET
from pathlib import Path

RES = Path(__file__).parent.parent / "resources"
APP_ID = "com.belfrydw.belfryscad"
MIME = "application/x-openscad"


def _desktop():
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(RES / f"{APP_ID}.desktop", encoding="utf-8")
    return cp["Desktop Entry"]


def test_desktop_file_names_the_app_id_icon_and_mime_type():
    d = _desktop()
    assert d["Icon"] == APP_ID
    assert d["MimeType"].rstrip(";").split(";") == [MIME]


def test_mime_definition_claims_scad_under_the_same_type():
    ns = {"m": "http://www.freedesktop.org/standards/shared-mime-info"}
    t = ET.parse(RES / f"{APP_ID}.mime.xml").getroot().find("m:mime-type", ns)
    assert t.get("type") == MIME
    assert t.find("m:glob", ns).get("pattern") == "*.scad"


def test_metainfo_launches_the_desktop_file_and_handles_the_type():
    root = ET.parse(RES / f"{APP_ID}.metainfo.xml").getroot()
    assert root.findtext("id") == APP_ID
    assert root.findtext("launchable") == f"{APP_ID}.desktop"
    assert root.findtext("provides/mediatype") == MIME


def test_the_running_app_names_its_desktop_file():
    # Wayland matches windows to the desktop file by this.
    src = (Path(__file__).parent.parent / "src" / "belfryscad" / "main.py").read_text(encoding="utf-8")
    assert f'setDesktopFileName("{APP_ID}")' in src
