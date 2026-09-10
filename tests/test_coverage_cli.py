"""belfryscad --coverage and --test --coverage (no Qt: pure CLI)."""
import json
import os

from belfryscad import coverage, scadtest
from belfryscad.coverage import CoverageReport, FileSummary, format_report

LIB = """\
function f(x) = x > 0 ? 1 : 2;
module used() { cube(1); }
module unused() { sphere(1); }
"""


def _report_for(tmp_path, script):
    p = tmp_path / "s.scad"
    p.write_text(script)
    return coverage.run_coverage(str(p))


def test_run_coverage_reports_per_file_and_gaps(tmp_path):
    (tmp_path / "lib.scad").write_text(LIB)
    r = _report_for(tmp_path, "include <lib.scad>\na = f(1);\nused();\n")
    files = {os.path.basename(f.origin): f for f in r.files()}
    assert set(files) == {"lib.scad", "s.scad"}
    lib = files["lib.scad"]
    assert (lib.bodies, lib.bodies_hit) == (3, 2)
    assert (lib.branches, lib.branches_hit) == (2, 1)
    assert files["s.scad"].percent == 100.0
    gaps = r.uncovered()
    assert {(os.path.basename(g["origin"]), g["kind"]) for g in gaps} == {("lib.scad", "branch"), ("lib.scad", "body"), ("lib.scad", "statement")}
    text = format_report(r, base=str(tmp_path))
    assert text.splitlines()[0].startswith("lib.scad")
    assert "TOTAL" in text and "Not covered (3):" in text and "lib.scad:3:" in text


def test_defines_keep_the_real_origin(tmp_path):
    r = _report_for(tmp_path, "a = 1;\nif (a == 2) cube(1);\n")
    p = tmp_path / "s.scad"
    r2 = coverage.run_coverage(str(p), defines=["a=2"])
    assert all(os.path.basename(s["origin"]) == "s.scad" for s in r2.spans.values())
    hit = lambda rep: sum(s["hits"] > 0 for s in rep.spans.values() if s["arm"])
    assert hit(r) == 0 and hit(r2) == 1         # -D a=2 takes the arm


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
    assert a.spans[("/x.scad", 0, 5, "statement")]["hits"] == 3
    assert len(a.spans) == 3
    a.drop_origins(lambda o: os.path.basename(o).startswith("tmp_docsgen_"))
    assert {k[0] for k in a.spans} == {"/x.scad"}
    (f,) = a.files()
    assert (f.spans, f.spans_hit, f.bodies, f.bodies_hit) == (2, 1, 1, 0)
    assert CoverageReport.from_json(a.to_json()).spans == a.spans
    assert FileSummary("").percent == 100.0


def test_cli_main_exit_codes_and_json(tmp_path, capsys):
    p = tmp_path / "s.scad"
    p.write_text("function f(x) = x > 0 ? 1 : 2;\na = f(1);\n")
    out_json = tmp_path / "cov.json"
    assert coverage.main([str(p), "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "TOTAL" in text and "Not covered (1):" in text
    d = json.loads(out_json.read_text())
    assert d["total"]["spans"] == 4 and d["total"]["spans_hit"] == 3
    assert coverage.main([str(p), "--min", "90"]) == 1
    assert coverage.main([str(p), "--min", "50", "--no-gaps"]) == 0
    assert "Not covered" not in capsys.readouterr().out.split("TOTAL")[-1]
    assert coverage.main([str(tmp_path / "missing.scad")]) == 2


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
