"""#560: an example sees the file it documents.

Upstream builds an example's script from the file's Includes: lines only, so
a library outside BOSL2's arrangement had every example fail with `Ignoring
unknown function` for the functions it documents. The file is now included
after the Includes: lines -- unless they already reach it, since including a
file twice re-runs (and warns about) all of its top-level assignments.
"""
import os

import pytest

from belfryscad.docsgen import self_include
from belfryscad.docsgen.preview import build_preview
from belfryscad.docsgen.self_include import self_include_lines

DOC = """\
// LibFile: foo.scad
// Includes:
{includes}

// Function: foo()
// Synopsis: Returns a number.
// Usage:
//   x = foo();
// Description:
//   Returns FOO_VALUE.
// Log:
//   echo(foo_says = foo());
FOO_VALUE = 42;
function foo() = FOO_VALUE;
"""


@pytest.fixture
def libdir(tmp_path, monkeypatch):
    """A libraries folder holding MYLIB, whose std.scad includes core.scad
    but not extra.scad -- BOSL2's shape, without needing BOSL2."""
    lib = tmp_path / "libraries" / "MYLIB"
    lib.mkdir(parents=True)
    (lib / "std.scad").write_text("include <core.scad>\n")
    (lib / "core.scad").write_text("CORE = 1;\n")
    (lib / "extra.scad").write_text("EXTRA = 2;\n")
    monkeypatch.setenv("OPENSCADPATH", str(tmp_path / "libraries"))
    self_include._reached.cache_clear()
    return lib


def test_a_file_outside_the_library_is_included(tmp_path, libdir):
    src = tmp_path / "mine" / "foo.scad"
    src.parent.mkdir()
    src.write_text("")
    assert self_include_lines(str(src), ["include <MYLIB/std.scad>"]) == ["include <foo.scad>"]


def test_a_file_the_includes_already_reach_is_not_included_twice(libdir):
    # core.scad is reached through std.scad, as BOSL2's core files are.
    assert self_include_lines(str(libdir / "core.scad"), ["include <MYLIB/std.scad>"]) == []
    # extra.scad lists itself, as gears.scad does.
    lines = ["include <MYLIB/std.scad>", "include <MYLIB/extra.scad>"]
    assert self_include_lines(str(libdir / "extra.scad"), lines) == []
    # ...and without that line it is not reached, so it is added.
    assert self_include_lines(str(libdir / "extra.scad"), ["include <MYLIB/std.scad>"]) \
        == ["include <extra.scad>"]


def test_a_checkout_outside_the_libraries_folder_reaches_itself(tmp_path, libdir):
    """A clone previewed from elsewhere: the libshim sends MYLIB to the
    clone during the run, so reach must be decided against the clone too."""
    clone = tmp_path / "clone"
    clone.mkdir()
    for f in ("std.scad", "core.scad", "extra.scad"):
        (clone / f).write_text((libdir / f).read_text())
    assert self_include_lines(str(clone / "core.scad"), ["include <MYLIB/std.scad>"]) == []


def test_the_docs_pane_runs_examples_against_the_live_buffer(tmp_path, libdir):
    """End to end through the Docs pane, with a Log block (no GL needed).
    The saved file on disk says 7; the buffer being edited says 42."""
    src = tmp_path / "mine" / "foo.scad"
    src.parent.mkdir()
    text = DOC.format(includes="//   include <MYLIB/std.scad>")
    src.write_text(text.replace("FOO_VALUE = 42;", "FOO_VALUE = 7;"))
    pv = build_preview(text, str(src), gen_images=False)
    assert pv.errors == [], pv.errors
    assert "foo_says = 42" in pv.markdown
    assert not any(p.name.startswith("tmp_docsgen_") for p in src.parent.iterdir()), \
        "the live copy is cleaned up"


def test_the_markdown_still_shows_the_example_as_written(tmp_path, libdir):
    """The include is added to what RUNS, not to the code the docs show."""
    src = tmp_path / "mine" / "foo.scad"
    src.parent.mkdir()
    text = DOC.format(includes="//   include <MYLIB/std.scad>")
    src.write_text(text)
    pv = build_preview(text, str(src), gen_images=False)
    assert "include <foo.scad>" not in pv.markdown
