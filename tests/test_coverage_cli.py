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


def test_nocov_excludes_the_marked_span_and_everything_in_it(tmp_path):
    """A /* nocov */ marker attaches to the LARGEST span starting on its
    line, so marking a block takes the block."""
    src = tmp_path / "lib.scad"
    src.write_text(
        "module used() { cube(1); }\n"                 # 1
        "module debug_only() {        /* nocov */\n"   # 2
        "    echo(\"tracing\");\n"                     # 3
        "    if (true) { sphere(1); }\n"               # 4
        "}\n"                                          # 5
        "module untested() { cube(3); }\n")            # 6
    origin = str(src)

    def span(line, start, end, kind, hits, arm=False):
        return _span(origin, line, start, end, kind, hits, arm)

    # The module on line 2 spans the whole block; its contents nest inside.
    r = CoverageReport.from_result({"spans": [
        span(1, 0, 26, "statement", 1),
        span(2, 27, 120, "statement", 0),     # module debug_only() { ... }
        span(2, 48, 119, "body", 0),          # its body, same line
        span(3, 70, 86, "statement", 0),      # echo, nested
        span(4, 91, 115, "branch", 0, arm=True),
        span(6, 130, 160, "statement", 0),    # untested, unmarked
    ]})
    assert len(r.spans) == 6
    dropped = r.drop_nocov(read_source=lambda o: src.read_text())
    assert dropped == 4, "the module, its body, the echo and the branch arm"
    kinds = sorted((s["line"], s["kind"]) for s in r.spans.values())
    assert kinds == [(1, "statement"), (6, "statement")]
    # Excluded code leaves the percentage alone rather than counting as run.
    assert r.total().spans == 2 and r.total().spans_hit == 1


def test_nocov_marker_spellings(tmp_path):
    from belfryscad.coverage import nocov_lines
    text = ("a();  /* nocov */\n"
            "b();  /*nocov*/\n"
            "c();  /*  NoCov  */\n"
            "d();  // nocov\n"          # line comment: not a marker
            "e();\n")
    assert nocov_lines(text) == {1, 2, 3}
