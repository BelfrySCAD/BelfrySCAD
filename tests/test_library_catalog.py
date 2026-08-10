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
        assert verbs == {e["include_statement"].split()[0]}, e["install_as"]


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


def test_include_statement_matches_a_listed_file(catalog):
    # BOLTS is the known exception: its repository ships no .scad entry
    # point at all, so the statement names a file the install cannot have.
    mismatched = []
    for e in catalog:
        m = re.search(r"[<\"]([^>\"]+)[>\"]", e["include_statement"])
        assert m, e["install_as"]
        if m.group(1) not in {_named(r) for r in e["includes"]}:
            mismatched.append(e["install_as"])
    assert mismatched == ["BOLTS"]


def test_exactly_one_primary_per_library(catalog):
    for e in catalog:
        primaries = [r for r in e["includes"] if r.get("primary")]
        expected = 0 if e["install_as"] == "BOLTS" else 1
        assert len(primaries) == expected, e["install_as"]


def test_the_primary_is_listed_first(catalog):
    for e in catalog:
        if e["install_as"] == "BOLTS":
            continue
        assert e["includes"][0].get("primary"), e["install_as"]


def test_a_bundled_file_is_never_the_primary(catalog):
    # "bundled" means the entry point already pulls it in; the entry point
    # cannot be pulled in by itself.
    both = [r["statement"] for e in catalog for r in e["includes"]
            if r.get("bundled") and r.get("primary")]
    assert both == []


def test_bosl2_extras_are_separable(catalog):
    # The case that prompted the lists: std.scad is the entry point, but
    # several files are deliberately not in it and are included on their
    # own.
    bosl2 = next(e for e in catalog if e["install_as"] == "BOSL2")
    by_path = {_named(r): r for r in bosl2["includes"]}
    assert by_path["BOSL2/std.scad"].get("primary")
    for extra in ("BOSL2/nurbs.scad", "BOSL2/gears.scad", "BOSL2/screws.scad"):
        assert extra in by_path, extra
        assert not by_path[extra].get("bundled"), extra
    # ...while the ones std.scad does pull in are marked as such.
    assert by_path["BOSL2/shapes3d.scad"].get("bundled")
