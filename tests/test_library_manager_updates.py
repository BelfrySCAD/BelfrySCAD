"""Library Manager installs the latest version and notices a re-used tag (#1179).

Two bugs, both seen with Enclosure on Codeberg (#1135): Install and Upgrade
always fetched the catalogue's pinned version, so an Upgrade re-installed
what was there; and an update was decided by version name alone, so a
release deleted and made again under the same tag name was never offered.
No network and no widgets: the API calls are monkeypatched.
"""
import threading

import pytest

import belfryscad.settings as settings
import belfryscad.window.library_manager as lm

ENCLOSURE = {"name": "Enclosure", "version": "20261113", "download_url": "https://codeberg.org/rrioja/Enclosure"}
BOSL2 = {"name": "BOSL2", "version": "v2.0.745", "download_url": "https://github.com/BelfrySCAD/BOSL2/"}


@pytest.fixture
def scratch_settings(tmp_path):
    previous = settings._scratch_dir
    settings.use_scratch_settings(str(tmp_path), seed=False)
    yield
    settings._scratch_dir = previous


def test_downloads_the_latest_version_once_it_is_known():
    assert lm._version_to_download(ENCLOSURE, "20261121") == "20261121"
    assert lm._version_to_download(ENCLOSURE, None) == "20261113"  # no update check yet


@pytest.mark.parametrize("installed, latest, retagged, expected", [
    ("20261113", "20261121", False, True),
    ("20261121", "20261121", False, False),
    ("20261121", "20261121", True, True),   # same name, the tag moved
    (None, "20261121", True, False),        # installed by hand: version unknown
])
def test_update_available(installed, latest, retagged, expected):
    assert lm._update_available(installed, latest, retagged) is expected


def test_tag_commit_asks_each_host(monkeypatch):
    seen = []

    def fake_get(url, headers):
        seen.append(url)
        return {"commit": {"sha": "765febb9"}} if "codeberg" in url else {"sha": "5786cbf2"}

    monkeypatch.setattr(lm, "_api_get", fake_get)
    assert lm._tag_commit(ENCLOSURE["download_url"], "20261121") == "765febb9"
    assert lm._tag_commit(BOSL2["download_url"], "v2.0.745") == "5786cbf2"
    assert seen == ["https://codeberg.org/api/v1/repos/rrioja/Enclosure/tags/20261121",
                    "https://api.github.com/repos/BelfrySCAD/BOSL2/commits/v2.0.745"]


def test_tag_commit_is_none_when_it_cannot_ask(monkeypatch):
    def fail(url, headers):
        raise OSError("offline")

    monkeypatch.setattr(lm, "_api_get", fail)
    assert lm._tag_commit(ENCLOSURE["download_url"], "20261121") is None


@pytest.mark.parametrize("installed, tag_now, expected", [
    (("20261121", "cad77a6f"), "765febb9", True),   # #1135: re-released on newer code
    (("20261121", "765febb9"), "765febb9", False),  # same commit: up to date
    (("20261121", None), "765febb9", False),        # installed before commits were recorded
    (("20261113", "cad77a6f"), "765febb9", False),  # a different name is an update anyway
    (("20261121", "cad77a6f"), None, False),        # couldn't ask: say nothing
])
def test_retag_detection(monkeypatch, installed, tag_now, expected):
    asked = []
    monkeypatch.setattr(lm, "_tag_commit", lambda url, tag: asked.append(tag) or tag_now)
    worker = lm._UpdateCheckWorker([ENCLOSURE], threading.Event(), {"Enclosure": installed})
    assert worker._retagged(ENCLOSURE, "20261121") is expected
    # The extra request is made only when the names match and a commit is known.
    assert bool(asked) == (installed[0] == "20261121" and installed[1] is not None)


def test_installed_commit_is_recorded_and_forgotten(scratch_settings):
    versions = lm._InstalledVersions()
    versions.set("Enclosure", "20261121", "765febb9")
    assert (versions.get("Enclosure"), versions.commit("Enclosure")) == ("20261121", "765febb9")
    versions.set("Enclosure", "20261122")  # commit unknown this time: none kept from before
    assert versions.commit("Enclosure") is None
    versions.set("Enclosure", "20261122", "aaaa")
    versions.remove("Enclosure")
    assert (versions.get("Enclosure"), versions.commit("Enclosure")) == (None, None)
