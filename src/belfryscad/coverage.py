"""Coverage reports: what ran, per file, and where the gaps are.

`belfryscad --test --coverage` runs every test with the evaluator's coverage
on (resolve pass only -- the geometry pass runs no script code) and merges
the result; the GUI's Testing pane (Design > Run Tests…) runs the same code
on a worker thread. Both share this module: the evaluator hands back
`{spans, files, total}`
(see openscad_cpp_evaluator's coverage.hpp), and this file merges, filters,
summarises and formats it. No Qt here, so the GUI's overlay and the test
runner read the same CoverageReport.

There is deliberately no coverage of a single run, from the CLI or the GUI.
Coverage answers "what does my test suite miss", which needs a suite; one
run of one script says very little. A script that wants measuring anyway can
be wrapped in a one-line .scadtest.
"""
from __future__ import annotations

import json
import os
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
        # Normalise the origin before keying on it. Two suites in different
        # directories reach one library by different relative paths
        # (`lib.scad` from beside it, `../lib.scad` from a subdirectory), and
        # keying on the raw string filed those as two separate files --
        # duplicate rows in the report and every percentage roughly halved.
        # Running a whole directory tree at once (the Testing pane) makes
        # that the normal case rather than a curiosity. abspath normalises
        # away the `..` as well as relative roots.
        for s in spans:
            origin = os.path.abspath(s["origin"])
            key = (origin, s["start"], s["end"], s["kind"])
            have = self.spans.get(key)
            if have is None:
                self.spans[key] = dict(s, origin=origin)
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
