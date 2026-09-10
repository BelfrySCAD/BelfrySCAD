"""Coverage reports: what ran, per file, and where the gaps are.

`belfryscad --coverage FILE.scad` runs the script (resolve pass only -- the
geometry pass runs no script code) with the evaluator's coverage on and
prints one line per file plus every uncovered span. `belfryscad --test
--coverage` does the same over every test's evaluation, merged. Both share
this module: the evaluator hands back `{spans, files, total}` (see
openscad_cpp_evaluator's coverage.hpp), and this file merges, filters,
summarises and formats it. No Qt here: the GUI's overlay reads the same
CoverageReport.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

KINDS = ("statement", "branch", "body")


@dataclass
class FileSummary:
    origin: str
    statements: int = 0
    statements_hit: int = 0
    branches: int = 0
    branches_hit: int = 0
    bodies: int = 0
    bodies_hit: int = 0
    spans: int = 0
    spans_hit: int = 0

    @staticmethod
    def _pct(hit: int, total: int) -> float:
        return 100.0 * hit / total if total else 100.0

    @property
    def percent(self) -> float:
        return self._pct(self.spans_hit, self.spans)

    @property
    def statement_percent(self) -> float:
        return self._pct(self.statements_hit, self.statements)

    @property
    def branch_percent(self) -> float:
        return self._pct(self.branches_hit, self.branches)

    @property
    def body_percent(self) -> float:
        return self._pct(self.bodies_hit, self.bodies)

    def add(self, span: dict) -> None:
        hit = span["hits"] > 0
        self.spans += 1
        self.spans_hit += hit
        if span["kind"] == "statement":
            self.statements += 1
            self.statements_hit += hit
        if span["kind"] == "branch" or span["arm"]:
            self.branches += 1
            self.branches_hit += hit
        if span["kind"] == "body":
            self.bodies += 1
            self.bodies_hit += hit


@dataclass
class CoverageReport:
    """Spans keyed by (origin, start, end, kind) so runs can be merged."""
    spans: dict = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: dict | None) -> "CoverageReport":
        report = cls()
        if result:
            report.merge_spans(result["spans"])
        return report

    def merge_spans(self, spans) -> None:
        for s in spans:
            key = (s["origin"], s["start"], s["end"], s["kind"])
            have = self.spans.get(key)
            if have is None:
                self.spans[key] = dict(s)
            else:
                have["hits"] += s["hits"]

    def merge(self, other: "CoverageReport") -> None:
        self.merge_spans(other.spans.values())

    def drop_origins(self, predicate) -> None:
        """Forget every span whose origin `predicate` accepts -- the test
        snippets themselves under --test --coverage."""
        self.spans = {k: v for k, v in self.spans.items() if not predicate(k[0])}

    def files(self) -> list[FileSummary]:
        by = {}
        for s in self.spans.values():
            by.setdefault(s["origin"], FileSummary(s["origin"])).add(s)
        return [by[o] for o in sorted(by)]

    def total(self) -> FileSummary:
        t = FileSummary("")
        for s in self.spans.values():
            t.add(s)
        return t

    def uncovered(self) -> list[dict]:
        return sorted((s for s in self.spans.values() if s["hits"] == 0),
                      key=lambda s: (s["origin"], s["line"], s["column"]))

    def to_json(self) -> str:
        return json.dumps({"spans": sorted(self.spans.values(),
                                           key=lambda s: (s["origin"], s["start"], s["end"], s["kind"])),
                           "files": [vars(f) for f in self.files()],
                           "total": vars(self.total())}, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "CoverageReport":
        return cls.from_result(json.loads(text))


def _rel(origin: str, base: str | None) -> str:
    if base:
        try:
            rel = os.path.relpath(origin, base)
        except ValueError:      # different drive on Windows
            return origin
        if not rel.startswith(os.pardir):   # a path that climbs out of cwd reads worse than the absolute one
            return rel
    return origin


def format_report(report: CoverageReport, base: str | None = None, uncovered: bool = True,
                  worst_first: bool = False) -> str:
    """The text `--coverage` prints. One line per file, then the total, then
    (optionally) every uncovered span as `file:line:col  kind`, which the
    GUI console makes clickable and grep does not mind."""
    files = report.files()
    if worst_first:
        files.sort(key=lambda f: (f.percent, f.origin))
    width = max((len(_rel(f.origin, base)) for f in files), default=8)
    out = []
    for f in files + [report.total()]:
        name = _rel(f.origin, base) if f.origin else "TOTAL"
        out.append(f"{name:<{width}}  {f.percent:5.1f}%  "
                   f"statements {f.statements_hit}/{f.statements}  "
                   f"branches {f.branches_hit}/{f.branches}  "
                   f"bodies {f.bodies_hit}/{f.bodies}")
    if uncovered:
        gaps = report.uncovered()
        if gaps:
            out.append("")
            out.append(f"Not covered ({len(gaps)}):")
            for s in gaps:
                kind = s["kind"] + (" (if/else arm)" if s["arm"] else "")
                out.append(f"  {_rel(s['origin'], base)}:{s['line']}:{s['column']}  {kind}")
    return "\n".join(out)


def run_coverage(source_path: str, defines: list[str] = (), strict_commas: bool = False,
                 echo_fn=None) -> CoverageReport | None:
    """Evaluate `source_path` with coverage on (resolve pass only) and return
    the report, or None after printing the error -- the same evaluate path
    the headless exporter uses, minus the geometry."""
    from belfryscad.headless import _prepare_source, _print_error
    from belfryscad.export_name import seed_params
    from openscad_cpp_evaluator import Evaluator, EvalError, ParseError, parse as _oce_parse
    parse_path, tmp_path = _prepare_source(source_path, list(defines))
    if parse_path is None:
        return None
    try:
        try:
            _oce_parse(parse_path)
        except ParseError as e:
            _print_error(e)
            return None
        ev = Evaluator(echo_fn=echo_fn or (lambda m: print(m, file=sys.stderr)), coverage=True)
        try:
            ev.evaluate(parse_path, seed_params({}, source_path), generate=False, strict_commas=strict_commas)
        except EvalError as e:
            _print_error(e)
            return None
        report = CoverageReport.from_result(ev.coverage_result)
        if tmp_path:
            # -D rewrote the script into a temp copy; report it under its real name.
            for key in list(report.spans):
                if key[0] == parse_path:
                    s = report.spans.pop(key)
                    s["origin"] = os.path.abspath(source_path)
                    report.spans[(s["origin"],) + key[1:]] = s
        return report
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="belfryscad --coverage",
        description="Run a script with coverage on and report which statements, branch arms "
                    "and function/module bodies ran, per file. The geometry pass is skipped: "
                    "it runs no script code.")
    parser.add_argument("file", metavar="FILE.scad")
    parser.add_argument("-D", dest="defines", action="append", default=[], metavar="var=value",
                        help="Override a top-level variable (repeatable), as with -o")
    parser.add_argument("--json", metavar="PATH", help="Also write the full report as JSON")
    parser.add_argument("--min", type=float, metavar="PERCENT",
                        help="Exit 1 if overall coverage is below this (for CI)")
    parser.add_argument("--no-gaps", action="store_true", help="Print only the per-file lines")
    parser.add_argument("--strict-commas", action="store_true")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.file):
        print(f"belfryscad: {args.file}: no such file", file=sys.stderr)
        return 2
    report = run_coverage(args.file, args.defines, strict_commas=args.strict_commas)
    if report is None:
        return 1
    print(format_report(report, base=os.getcwd(), uncovered=not args.no_gaps))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(report.to_json())
    if args.min is not None and report.total().percent < args.min:
        print(f"belfryscad: coverage {report.total().percent:.1f}% is below --min {args.min}", file=sys.stderr)
        return 1
    return 0
