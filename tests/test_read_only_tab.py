"""Tests for library-file read-only detection."""
from pathlib import Path

from belfryscad.window.library_manager import _library_dir


def _is_library_file(path: str) -> bool:
    return Path(path).resolve().is_relative_to(_library_dir().resolve())


def test_file_inside_library_dir_is_detected():
    lib_file = _library_dir() / "BOSL2" / "std.scad"
    assert _is_library_file(str(lib_file))


def test_file_outside_library_dir_is_not_detected():
    assert not _is_library_file("/tmp/my_project/model.scad")
