"""Where a GUI launch with no real working directory should start.

An app launched from Finder has cwd "/" (measured with lsof against the
running bundle), so Save would otherwise offer the filesystem root. These
cover the per-platform choice and, more importantly, that a genuine shell
launch is left alone -- moving a user's cwd out from under them would
change what every relative path in the session means.

Pure filesystem logic, no GL and no live widgets.
"""

import os
import sys
from pathlib import Path

import pytest

from belfryscad import main as bs_main
from belfryscad.main import _adopt_working_dir, _default_working_dir


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """A fake Documents folder, on a non-Linux platform."""
    d = tmp_path / "Documents"
    d.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(bs_main, "_documents_dir", lambda: d)
    return d


class TestDocumentsPlatforms:
    """macOS and Windows: a BelfrySCAD/OpenSCAD subfolder, else Documents."""

    def test_prefers_belfryscad_over_openscad(self, docs):
        (docs / "BelfrySCAD").mkdir()
        (docs / "OpenSCAD").mkdir()
        assert _default_working_dir() == docs / "BelfrySCAD"

    def test_falls_back_to_openscad(self, docs):
        (docs / "OpenSCAD").mkdir()
        assert _default_working_dir() == docs / "OpenSCAD"

    def test_falls_back_to_documents_itself(self, docs):
        assert _default_working_dir() == docs

    def test_none_when_there_is_no_documents_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(bs_main, "_documents_dir", lambda: None)
        assert _default_working_dir() is None

    def test_windows_takes_the_same_path_as_macos(self, docs, monkeypatch):
        """Windows must not hard-code ~/Documents -- _documents_dir asks Qt,
        which resolves a OneDrive-redirected known folder."""
        monkeypatch.setattr(sys, "platform", "win32")
        (docs / "OpenSCAD").mkdir()
        assert _default_working_dir() == docs / "OpenSCAD"


class TestLinux:
    def test_linux_uses_home_not_documents(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        # Even with a Documents/BelfrySCAD present, Linux gets $HOME.
        (tmp_path / "Documents" / "BelfrySCAD").mkdir(parents=True)
        assert _default_working_dir() == tmp_path


class TestAdopt:
    def test_a_shell_launch_keeps_its_directory(self, docs, tmp_path, monkeypatch):
        """The load-bearing one: a real cwd must survive untouched, or every
        relative path the user typed would silently change meaning."""
        (docs / "BelfrySCAD").mkdir()
        work = tmp_path / "somewhere"
        work.mkdir()
        monkeypatch.chdir(work)
        assert _adopt_working_dir() is None
        assert Path.cwd() == work.resolve()

    def test_root_launch_adopts_the_default(self, docs, monkeypatch):
        target = docs / "BelfrySCAD"
        target.mkdir()
        monkeypatch.chdir(Path(Path.cwd().anchor))
        try:
            assert _adopt_working_dir() == target
            assert Path.cwd() == target.resolve()
        finally:
            os.chdir(Path(Path.cwd().anchor))

    def test_root_launch_with_nothing_to_adopt_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(bs_main, "_documents_dir", lambda: None)
        root = Path(Path.cwd().anchor)
        monkeypatch.chdir(root)
        assert _adopt_working_dir() is None
        assert Path.cwd() == root
