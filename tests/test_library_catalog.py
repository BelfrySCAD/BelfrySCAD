"""The bundled library catalogue is well-formed.

Pure data, so it needs no Qt. The list of includes per library is derived
from the libraries' own sources (see the entries' `includes` key); these
tests guard the shape of it, and the one rule that actually bites: each
statement is pasted into a script as it stands, so the file it names must
match what the installer lays down on disk.
"""
import json
import re
from pathlib import Path

import pytest

CATALOG = (Path(__file__).resolve().parent.parent
           / "src" / "belfryscad" / "resources" / "libraries.json")


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_every_library_lists_its_includes(catalog):
    missing = [e["install_as"] for e in catalog if not e.get("includes")]
    assert missing == []


def _named(row):
    """The file a statement refers to."""
    m = re.search(r"[<\"]([^>\"]+)[>\"]", row["statement"])
    assert m, row["statement"]
    return m.group(1)


def test_every_row_is_a_usable_statement(catalog):
    # Pasted into a script as-is, so it has to be a whole statement rather
    # than a bare path.
    bad = [r["statement"] for e in catalog for r in e["includes"]
           if not re.fullmatch(r"(include|use) <[^<>]+>", r["statement"])]
    assert bad == []


def test_each_library_uses_one_verb_throughout(catalog):
    # Which of include<> or use<> a library wants is a property of the
    # library, not of the file.
    for e in catalog:
        verbs = {r["statement"].split()[0] for r in e["includes"]}
        assert len(verbs) == 1, (e["install_as"], verbs)


def test_paths_are_rooted_at_the_install_directory(catalog):
    # The installer moves the archive's root to <libraries>/<install_as>,
    # so every named file has to start with that name or the statement
    # will not resolve once installed.
    wrong = [(e["install_as"], _named(r))
             for e in catalog for r in e["includes"]
             if not _named(r).startswith(e["install_as"] + "/")]
    assert wrong == []


def test_every_include_has_a_description(catalog):
    blank = [r["statement"] for e in catalog for r in e["includes"]
             if not r.get("description", "").strip()]
    assert blank == []


def test_no_duplicate_paths_within_a_library(catalog):
    dupes = [e["install_as"] for e in catalog
             if len({r["statement"] for r in e["includes"]}) != len(e["includes"])]
    assert dupes == []


def test_the_entry_point_lives_in_the_list_only(catalog):
    # There is no separate include_statement field any more: the entry
    # point is the row marked primary, so nothing can drift out of step
    # with the list beside it.
    assert all("include_statement" not in e for e in catalog)


def test_exactly_one_primary_per_library(catalog):
    # BOLTS is the standing exception: its repository ships no .scad entry
    # point at all, only the generator that builds one, so there is nothing
    # to mark.
    for e in catalog:
        primaries = [r for r in e["includes"] if r.get("primary")]
        expected = 0 if e["install_as"] == "BOLTS" else 1
        assert len(primaries) == expected, e["install_as"]


def test_the_primary_is_listed_first(catalog):
    for e in catalog:
        if e["install_as"] == "BOLTS":
            continue
        assert e["includes"][0].get("primary"), e["install_as"]


def test_nothing_the_entry_point_already_pulls_in_is_listed(catalog):
    # Listing them again is noise: including the entry point has already
    # brought them in. Only the entry point itself and what it leaves out
    # are worth offering.
    leftover = [r["statement"] for e in catalog for r in e["includes"]
                if r.get("bundled")]
    assert leftover == []


def test_bosl2_lists_the_entry_point_and_its_extras_only(catalog):
    # The case that prompted the lists: std.scad is the entry point, and
    # several files are deliberately not in it, to be included on their own.
    bosl2 = next(e for e in catalog if e["install_as"] == "BOSL2")
    by_path = {_named(r): r for r in bosl2["includes"]}
    assert by_path["BOSL2/std.scad"].get("primary")
    for extra in ("BOSL2/nurbs.scad", "BOSL2/gears.scad", "BOSL2/screws.scad"):
        assert extra in by_path, extra
    # ...while what std.scad already pulls in is absent.
    for inside in ("BOSL2/shapes3d.scad", "BOSL2/affine.scad", "BOSL2/paths.scad"):
        assert inside not in by_path, inside


def test_bosl2_omits_the_files_its_docsgen_config_ignores(catalog):
    # BOSL2's .openscad_docsgen_rc lists files it does not document:
    # a BOSL1 compatibility shim, a superseded screws file, internals.
    # std.scad is on that list too, because there is nothing to document
    # in an aggregator -- but it is the way in, so it stays.
    bosl2 = next(e for e in catalog if e["install_as"] == "BOSL2")
    listed = {_named(r) for r in bosl2["includes"]}
    for ignored in ("BOSL2/bosl1compat.scad", "BOSL2/metric_screws.scad",
                    "BOSL2/builtins.scad", "BOSL2/foo.scad"):
        assert ignored not in listed, ignored
    assert "BOSL2/std.scad" in listed


def test_no_test_harnesses_are_offered(catalog):
    # A library's own test file is includable but is not something to
    # offer -- same reasoning as skipping its tests/ directory.
    import os
    harnesses = [r["statement"] for e in catalog for r in e["includes"]
                 if re.search(r"(^|[_-])tests?$|^tests?([_-]|$)|^libtest$",
                              os.path.splitext(os.path.basename(_named(r)))[0], re.I)]
    assert harnesses == []
