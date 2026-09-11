"""belfryscad --test --coverage, and the report types it shares with the
GUI overlay (no Qt: pure CLI)."""
import json
import os

from belfryscad import scadtest
from belfryscad.coverage import CoverageReport, FileSummary, format_report

LIB = """\
function f(x) = x > 0 ? 1 : 2;
module used() { cube(1); }
module unused() { sphere(1); }
"""


def _span(origin, line, start, end, kind, hits, arm=False):
    return {"origin": origin, "line": line, "column": 1, "start": start, "end": end,
            "kind": kind, "arm": arm, "hits": hits}


def _abs(*parts):
    """The key merge_spans will file a span under (it normalises origins)."""
    return os.path.abspath(os.path.join(*parts))


def test_format_report_lists_files_then_every_gap():
    r = CoverageReport.from_result({"spans": [
        _span("/w/lib.scad", 1, 0, 5, "statement", 1),
        _span("/w/lib.scad", 2, 6, 9, "body", 0),
        _span("/w/lib.scad", 3, 10, 14, "branch", 0, arm=True),
        _span("/w/s.scad", 1, 0, 5, "statement", 1),
    ]})
    text = format_report(r, base=_abs("/w"))
    assert text.splitlines()[0].startswith("lib.scad")     # worst is not sorted first by default
    assert "s.scad" in text and "TOTAL" in text
    assert "Not covered (2):" in text
    assert "lib.scad:2:1  body" in text
    assert "lib.scad:3:1  branch (if/else arm)" in text    # arms say so
    assert "Not covered" not in format_report(r, base=_abs("/w"), uncovered=False)


def test_merge_sums_hits_and_drop_origins_filters():
    a = CoverageReport.from_result({"spans": [
        {"origin": "/x.scad", "line": 1, "column": 1, "start": 0, "end": 5, "kind": "statement", "arm": False, "hits": 1},
        {"origin": "/tmp_docsgen_1_a.scad", "line": 1, "column": 1, "start": 0, "end": 5, "kind": "statement", "arm": False, "hits": 1},
    ]})
    b = CoverageReport.from_result({"spans": [
        {"origin": "/x.scad", "line": 1, "column": 1, "start": 0, "end": 5, "kind": "statement", "arm": False, "hits": 2},
        {"origin": "/x.scad", "line": 2, "column": 1, "start": 6, "end": 9, "kind": "body", "arm": False, "hits": 0},
    ]})
    a.merge(b)
    assert a.spans[(_abs("/x.scad"), 0, 5, "statement")]["hits"] == 3
    assert len(a.spans) == 3
    a.drop_origins(lambda o: os.path.basename(o).startswith("tmp_docsgen_"))
    assert {k[0] for k in a.spans} == {_abs("/x.scad")}
    (f,) = a.files()
    assert (f.spans, f.spans_hit, f.bodies, f.bodies_hit) == (2, 1, 1, 0)
    assert CoverageReport.from_json(a.to_json()).spans == a.spans
    assert FileSummary("").percent == 100.0


def test_test_runner_merges_library_coverage_and_drops_snippets(tmp_path, capsys, monkeypatch):
    (tmp_path / "lib.scad").write_text(LIB)
    (tmp_path / "test_lib.scadtest").write_text(
        'include = "include <lib.scad>"\n\n'
        '[[test]]\nname = "test_f"\nscript = """\ninclude <lib.scad>\nassert(f(1) == 1);\n"""\n\n'
        '[[test]]\nname = "test_used"\nscript = """\ninclude <lib.scad>\nused();\n"""\n')
    monkeypatch.chdir(tmp_path)
    json_path = tmp_path / "merged.json"
    code = scadtest.main(["--coverage", "--coverage-json", str(json_path), "test_lib.scadtest"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2 of 2 tests passed" in out
    assert "Coverage (worst first):" in out
    assert "lib.scad" in out and "tmp_docsgen" not in out
    d = json.loads(json_path.read_text())
    origins = {os.path.basename(s["origin"]) for s in d["spans"]}
    assert origins == {"lib.scad"}
    (lib,) = d["files"]
    assert lib["bodies"] == 3 and lib["bodies_hit"] == 2      # f and used, across two tests
    assert scadtest.main(["--coverage-min", "99", "test_lib.scadtest"]) == 1


def test_rel_paths_outside_base_stay_absolute(tmp_path):
    from belfryscad.coverage import _rel
    inside = str(tmp_path / "lib" / "a.scad")
    outside = str(tmp_path.parent / "elsewhere.scad")
    assert _rel(inside, str(tmp_path)) == os.path.join("lib", "a.scad")
    assert _rel(outside, str(tmp_path)) == outside
