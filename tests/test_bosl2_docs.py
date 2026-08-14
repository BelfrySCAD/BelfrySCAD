"""BOSL2 wiki lookup, against stubbed wiki markdown (no network)."""
import pytest

from belfryscad.window import bosl2_docs
from belfryscad.window import ai_tools

ALPHA = """\
# Alphabetical Index

## 0

- [`$slop`](constants.scad#constant-slop) Const – The slop amount to make printed items fit closely.

## C

- [`cuboid()`](shapes3d.scad#module-cuboid) Mod – Creates a cube with chamfering and roundovers. <sup title="Can return geometry.">[<abbr>Geom</abbr>]</sup>
- [`cyl()`](shapes3d.scad#functionmodule-cyl) Func/Mod – Creates an attachable cylinder with roundovers.
- [`rounded\\_prism()`](rounding.scad#functionmodule-rounded_prism) Func/Mod – Make a rounded 3d object.
"""

SHAPES3D = """\
# LibFile: shapes3d.scad

## Section: Cuboids

### Function/Module: cube()

**Synopsis:** Creates a cube.

### Module: cuboid()

**Synopsis:** Creates a cube with chamfering.

**Usage:**

- cuboid(size, [anchor=]);

### Function/Module: cyl()

**Synopsis:** A cylinder.

## Section: Cylinders
"""

TOPICS = """\
# Topic Index

**C**: [Chamfers](#chamfers)

### Attachable

- [`cuboid()`](shapes3d.scad#module-cuboid) Mod – Creates a cube.

### Chamfers

- [`chamfer_edge_mask()`](masks.scad#module-chamfer_edge_mask) Mod – Masks an edge.
"""

TUTORIALS = """\
# Tutorials

1. [VNF Tutorial (AKA: How to build a Polyhedron)](Tutorial-VNF)
1. [Using attach()](Tutorial-Attachment-Attach)
"""

PAGES = {
    "AlphaIndex": ALPHA,
    "shapes3d.scad": SHAPES3D,
    "Topics": TOPICS,
    "Tutorials": TUTORIALS,
    "Tutorial-VNF": "# VNF Tutorial\n\nA VNF is a vertex/face structure.\n",
    "Tutorial-Attachment-Attach": "# Using attach()\n",
}


@pytest.fixture(autouse=True)
def stub_wiki(monkeypatch):
    monkeypatch.setattr(bosl2_docs, "_index", None)
    monkeypatch.setattr(bosl2_docs, "_fetch", lambda page: PAGES[page])
    yield
    bosl2_docs._index = None


def test_lookup_finds_module_page_and_anchor():
    e = bosl2_docs.lookup("cuboid")
    assert (e.name, e.kind, e.page) == ("cuboid", "Module", "shapes3d.scad")
    assert e.url.endswith("/wiki/shapes3d.scad#module-cuboid")
    assert "<sup" not in e.summary


def test_lookup_is_forgiving_about_call_parens_case_and_dollars():
    assert bosl2_docs.lookup("Cuboid()").name == "cuboid"
    assert bosl2_docs.lookup("$slop").kind == "Constant"
    # docsgen escapes underscores in the index; the name must not keep them
    assert bosl2_docs.lookup("rounded_prism").page == "rounding.scad"


def test_lookup_misses_are_none():
    assert bosl2_docs.lookup("my_local_module") is None


def test_find_ranks_name_matches_before_summary_matches():
    names = [e.name for e in bosl2_docs.find("cub")]
    assert names[0] == "cuboid"
    assert "$slop" not in names
    assert "$slop" in [e.name for e in bosl2_docs.find("printed items")]


def test_entry_doc_extracts_just_that_section():
    doc = bosl2_docs.entry_doc(bosl2_docs.lookup("cuboid"))
    assert "### Module: cuboid()" in doc
    assert "chamfering" in doc
    assert "cube()" not in doc      # stops before the previous heading
    assert "A cylinder" not in doc  # and ends at the next one


def test_entry_doc_stops_at_a_higher_level_heading():
    doc = bosl2_docs.entry_doc(bosl2_docs.lookup("cyl"))
    assert doc.rstrip().endswith("**Synopsis:** A cylinder.")


def test_topic_doc_and_topic_list():
    assert bosl2_docs.topics() == ["Attachable", "Chamfers"]
    doc = bosl2_docs.topic_doc("chamfers")
    assert "chamfer_edge_mask" in doc and "cuboid" not in doc
    assert "/Topics#chamfers" in doc
    assert bosl2_docs.topic_doc("nonsense") is None


def test_tutorial_doc_matches_on_title_or_page():
    assert bosl2_docs.tutorial_doc("vnf").endswith("vertex/face structure.\n")
    assert "Tutorial-Attachment-Attach" in bosl2_docs.tutorial_doc("attachment-attach")
    assert bosl2_docs.tutorial_doc("nonsense") is None


# --- the AI tool wrapper ---------------------------------------------------

def _tool(**kwargs):
    return ai_tools.run_tool(None, "bosl2_docs", kwargs)


def test_tool_returns_the_entry_with_its_url():
    out = _tool(name="cuboid")
    assert "#module-cuboid" in out and "**Usage:**" in out


def test_tool_suggests_candidates_for_a_partial_name():
    out = _tool(name="cub")
    assert "No exact match" in out and "cuboid (Module)" in out


def test_tool_lists_what_is_available_when_asked_for_nothing():
    out = _tool()
    assert "Chamfers" in out and "VNF Tutorial" in out


def test_tool_falls_back_to_the_list_on_a_bad_topic_or_tutorial():
    assert "Attachable, Chamfers" in _tool(topic="nope")
    assert "Using attach()" in _tool(tutorial="nope")


def test_tool_clips_a_huge_page(monkeypatch):
    monkeypatch.setattr(ai_tools, "_MAX_DOC_CHARS", 100)
    out = _tool(name="cuboid")
    assert out.endswith("#module-cuboid]") and len(out) < 250
