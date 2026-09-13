"""#411: two dist-info directories, and About reported the older one.

A Windows installer that fails to replace its predecessor leaves both
`belfryscad-1.18.6.dist-info` and `belfryscad-1.23.2.dist-info` in one
folder. importlib.metadata answers with whichever it enumerates first --
directory order, which is alphabetical -- and 1.18.6 sorts before 1.23.2.
The installs share a directory, so the newest one's files are what is
actually running; only the metadata stacked up.
"""
import sys

import pytest

from belfryscad.versions import _version_key, duplicate_installs, package_version


def _install(root, name, version):
    """A minimal but real dist-info importlib.metadata will read."""
    d = root / f"{name}-{version}.dist-info"
    d.mkdir()
    (d / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return d


@pytest.fixture
def stacked(tmp_path, monkeypatch):
    """Two versions of one package visible at once, older sorting first."""
    _install(tmp_path, "demopkg", "1.18.6")
    _install(tmp_path, "demopkg", "1.23.2")
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


class _FakeDist:
    def __init__(self, name, version):
        self.metadata = {"Name": name}
        self.version = version


@pytest.fixture
def older_first(monkeypatch):
    """Enumeration order forced, older first.

    Not left to the filesystem: importlib hands back whichever it finds
    first, and on macOS that happens to be the NEWER one -- so a test that
    trusts directory order passes even with the bug reinstated, which is
    worth nothing. The reporter's Windows box enumerates the other way.
    """
    import importlib.metadata
    dists = [_FakeDist("demopkg", "1.18.6"), _FakeDist("demopkg", "1.23.2")]
    monkeypatch.setattr(importlib.metadata, "distributions", lambda **kw: iter(dists))


def test_the_newest_wins_whatever_the_order(older_first):
    assert package_version("demopkg") == "1.23.2", \
        "the first one enumerated is 1.18.6; the newest must win anyway"


def test_the_newest_wins(stacked):
    assert package_version("demopkg") == "1.23.2"


def test_importlib_alone_is_not_dependable(stacked):
    """Why this module exists: the obvious call answers with whichever
    distribution it enumerates FIRST, which is directory order -- a
    filesystem detail. It returns the older one on the reporter's Windows
    machine and the newer one on macOS, which is exactly the problem: it is
    not a choice at all, so it cannot be relied on either way."""
    import importlib.metadata
    assert importlib.metadata.version("demopkg") in ("1.18.6", "1.23.2")
    # package_version, by contrast, is the same answer on every platform.
    assert package_version("demopkg") == "1.23.2"


def test_duplicates_are_reported(stacked):
    assert duplicate_installs("demopkg") == ["1.18.6", "1.23.2"]


def test_a_single_install_is_not_a_duplicate(tmp_path, monkeypatch):
    _install(tmp_path, "solopkg", "2.0.0")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert package_version("solopkg") == "2.0.0"
    assert duplicate_installs("solopkg") == []


def test_missing_package_returns_the_default():
    assert package_version("no_such_package_at_all") == "unknown"
    assert package_version("no_such_package_at_all", default="not installed") == "not installed"
    assert duplicate_installs("no_such_package_at_all") == []


def test_the_running_version_is_found():
    """Not a tautology: it must find the real belfryscad, not fall back."""
    assert package_version("belfryscad") != "unknown"


@pytest.mark.parametrize("lo,hi", [
    ("1.18.6", "1.23.2"),      # the reported case: alphabetically backwards
    ("1.9.0", "1.10.0"),       # and the classic numeric-vs-lexical trap
    ("1.18.4", "1.18.6"),
    ("1.23.2", "2.0.0"),
])
def test_version_ordering(lo, hi):
    assert _version_key(lo) < _version_key(hi)


def test_an_unparsable_version_does_not_raise():
    assert _version_key("1.0.0-rc1") < _version_key("1.0.1")
