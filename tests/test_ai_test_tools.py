"""The AI's test tools: read, run, see coverage, propose changes.

No Qt here -- the tools reach scadtest and coverage directly, so they run in
the AI worker thread without waiting on the GUI.
"""
import json

import pytest

from belfryscad.window.ai_tools import AIToolContext, run_tool

LIB = """\
function f(x) = x > 0 ? 1 : 2;
module used() { cube(1); }
module unused() { sphere(1); }
module debug_only() {        /* nocov */
    echo("tracing");
}
"""

SUITE = """\
# a comment that must survive
[config]
timeout = 30

[[test]]
name = "alpha"
script = '''
include <lib.scad>
used();
'''

[[test]]
name = "beta"
script = '''
include <lib.scad>
assert(f(-1) == 2);
'''
"""


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "lib.scad").write_text(LIB)
    (tmp_path / "suite.scadtest").write_text(SUITE)
    proposals = []
    c = AIToolContext(library_dir=tmp_path, tests_dir=lambda: str(tmp_path),
                      on_proposal=proposals.append)
    c.proposals = proposals
    return c


def test_list_and_read(ctx):
    listed = json.loads(run_tool(ctx, "list_tests", {}))
    assert [s["tests"] for s in listed["suites"]] == [["alpha", "beta"]]

    one = json.loads(run_tool(ctx, "read_test", {"path": "suite.scadtest", "name": "alpha"}))
    assert one["name"] == "alpha" and "used();" in one["script"]
    assert one["timeout"] == 30            # inherited from [config]

    whole = json.loads(run_tool(ctx, "read_test", {"path": "suite.scadtest"}))
    assert [t["name"] for t in whole] == ["alpha", "beta"]

    assert "no test called" in run_tool(ctx, "read_test",
                                        {"path": "suite.scadtest", "name": "nope"})


def test_paths_outside_the_tests_directory_are_refused(ctx, tmp_path):
    outside = tmp_path.parent / "elsewhere.scadtest"
    outside.write_text(SUITE)
    assert "outside the tests directory" in run_tool(
        ctx, "read_test", {"path": str(outside)})
    assert "not a .scadtest" in run_tool(ctx, "read_test", {"path": "lib.scad"})


def test_run_tests_reports_results_and_coverage(ctx):
    out = json.loads(run_tool(ctx, "run_tests", {}))
    assert (out["passed"], out["failed"]) == (2, 0)
    assert {r["test"] for r in out["results"]} == {"alpha", "beta"}
    # unused() is never called; debug_only() is /* nocov */ and so is not counted.
    assert 0 < out["coverage"]["percent"] < 100

    gaps = run_tool(ctx, "read_coverage_gaps", {})
    assert "lib.scad:3" in gaps, gaps           # module unused()
    assert "debug_only" not in gaps and "lib.scad:4" not in gaps, "nocov code is excluded"


def test_coverage_gaps_before_any_run(ctx):
    assert "No coverage yet" in run_tool(ctx, "read_coverage_gaps", {})


def test_propose_edit_add_and_delete(ctx, tmp_path):
    suite = tmp_path / "suite.scadtest"
    before = suite.read_text()

    # Edit one field; everything else is kept.
    assert "Proposed" in run_tool(ctx, "propose_test_change", {
        "path": "suite.scadtest", "action": "edit", "name": "alpha", "timeout": 99})
    p = ctx.proposals[-1]
    assert p.kind == "test_edit" and p.filename == str(suite)
    assert "timeout = 99" in p.new_content
    assert "used();" in p.new_content and "a comment that must survive" in p.new_content
    assert suite.read_text() == before, "proposing must not write anything"

    # Add, positioned.
    run_tool(ctx, "propose_test_change", {
        "path": "suite.scadtest", "action": "add", "name": "mid",
        "script": "include <lib.scad>\nunused();", "anchor": "beta", "position": "before"})
    body = ctx.proposals[-1].new_content
    assert body.index('name = "mid"') < body.index('name = "beta"')

    # Delete.
    run_tool(ctx, "propose_test_change", {
        "path": "suite.scadtest", "action": "delete", "name": "beta"})
    assert 'name = "beta"' not in ctx.proposals[-1].new_content
    assert 'name = "alpha"' in ctx.proposals[-1].new_content


def test_proposal_errors_are_reported_not_raised(ctx):
    bad = [
        ({"path": "suite.scadtest", "action": "edit", "name": "nope"}, "no test called"),
        ({"path": "suite.scadtest", "action": "add", "name": "alpha",
          "script": "x();"}, "already has a test"),
        ({"path": "suite.scadtest", "action": "add", "name": "n",
          "script": "x();", "script_file": "f.scad"}, "not both"),
        ({"path": "suite.scadtest", "action": "add", "name": "n"}, "needs a script"),
        ({"path": "suite.scadtest", "action": "sideways", "name": "alpha"}, "must be add"),
        ({"path": "suite.scadtest", "action": "add", "name": "n", "script": "x();",
          "anchor": "ghost", "position": "after"}, "no test called 'ghost'"),
    ]
    for args, expected in bad:
        result = run_tool(ctx, "propose_test_change", args)
        assert expected in result, (args, result)
    assert ctx.proposals == [], "a refused change proposes nothing"


def test_no_tests_directory_chosen():
    c = AIToolContext(library_dir=None, tests_dir=lambda: None)
    assert "no tests directory" in run_tool(c, "read_test", {"path": "x.scadtest"}).lower()
    assert "No tests directory" in run_tool(c, "list_tests", {})
