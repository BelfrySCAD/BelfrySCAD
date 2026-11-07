"""BOSL2#2033: an alias was listed in AlphaIndex and Topics under the main
name, so the main name appeared twice and the alias not at all."""
import os

from belfryscad.docsgen import main

SRC = """\
// LibFile: u.scad
//   Utilities.

// Section: Types

// Function: is_int()
// Alias: is_integer()
// Topics: Type Checking
// Synopsis: Returns true if the argument is an integer.
// Usage:
//   bool = is_int(n);
// Description:
//   True for an integer.
function is_int(n) = is_num(n) && n == round(n);
function is_integer(n) = is_int(n);
"""


def test_an_alias_is_indexed_under_its_own_name(tmp_path, monkeypatch):
    (tmp_path / "u.scad").write_text(SRC)
    monkeypatch.chdir(tmp_path)
    main(["-q", "-n", "-i", "-I", "-D", str(tmp_path / "docs"), "u.scad"])
    for name in ("AlphaIndex.md", "Topics.md"):
        text = (tmp_path / "docs" / name).read_text()
        assert text.count("[`is_int()`]") == 1, name
        assert text.count("[`is_integer()`](u.scad#function-is_int)") == 1, name
