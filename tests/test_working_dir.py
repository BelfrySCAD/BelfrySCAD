"""Where a GUI launch with no real working directory should start.

An app launched from Finder has cwd "/" (measured with lsof against the
running bundle), so Save would otherwise offer the filesystem root. These
cover the preference order and, more importantly, that a genuine shell
launch is left alone -- moving a user's cwd out from under them would
change what every relative path in the session means.

Pure filesystem logic, no GL or Qt.
"""

import os
from pathlib import Path

import pytest

from belfryscad.main import _adopt_working_dir, _default_working_dir


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_prefers_belfryscad_over_openscad(fake_home):
    (fake_home / "Documents" / "BelfrySCAD").mkdir(parents=True)
    (fake_home / "Documents" / "OpenSCAD").mkdir(parents=True)
    assert _default_working_dir() == fake_home / "Documents" / "BelfrySCAD"


def test_falls_back_to_openscad(fake_home):
    (fake_home / "Documents" / "OpenSCAD").mkdir(parents=True)
    assert _default_working_dir() == fake_home / "Documents" / "OpenSCAD"


def test_falls_back_to_documents(fake_home):
    (fake_home / "Documents").mkdir()
    assert _default_working_dir() == fake_home / "Documents"


def test_none_when_no_documents_dir(fake_home):
    assert _default_working_dir() is None


def test_a_shell_launch_keeps_its_directory(fake_home, tmp_path, monkeypatch):
    """The load-bearing one: a real cwd must survive untouched, or every
    relative path the user typed would silently change meaning."""
    (fake_home / "Documents" / "BelfrySCAD").mkdir(parents=True)
    work = tmp_path / "somewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    assert _adopt_working_dir() is None
    assert Path.cwd() == work.resolve()


def test_root_launch_adopts_the_default(fake_home, monkeypatch):
    target = fake_home / "Documents" / "BelfrySCAD"
    target.mkdir(parents=True)
    monkeypatch.chdir(Path(Path.cwd().anchor))
    try:
        assert _adopt_working_dir() == target
        assert Path.cwd() == target.resolve()
    finally:
        os.chdir(Path(Path.cwd().anchor))


def test_root_launch_with_nothing_to_adopt_is_left_alone(fake_home, monkeypatch):
    root = Path(Path.cwd().anchor)
    monkeypatch.chdir(root)
    assert _adopt_working_dir() is None
    assert Path.cwd() == root
