"""`belfryscad --test`: run .scadtest files through this evaluator.

A drop-in replacement for `openscad-test`, the way `--docsgen` replaces
`openscad-docsgen`: same TOML format, same output, same exit code, so
BOSL2's `scripts/run_tests.sh` needs no change. What differs is that the
scripts run in this process instead of launching the OpenSCAD binary once
per test.

The whole of what a test asserts -- did the script run, what did it echo,
what did it warn about -- is what `docsgen.runner` already collects for
every Example it renders, so this module is an adapter over that rather
than a second evaluator. Geometry is skipped (`generate=False`), matching
the `RenderMode.test_only` the reference uses, which writes a CSG dump and
builds no solids.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestCase:
    """One `[[test]]` block. Mirrors openscad_test.parser.TestCase, whose
    field names are the .scadtest format's own keys."""
    name: str
    script: str | None = None
    script_file: str | None = None
    script_dir: str | None = None
    timeout: int = 60
    set_vars: dict = field(default_factory=dict)
    expect_success: bool = True
    assert_echoes: list = field(default_factory=list)
    assert_no_echoes: bool = True
    assert_warnings: list = field(default_factory=list)
    assert_no_warnings: bool = True


@dataclass
class TestResult:
    test_case: TestCase
    passed: bool
    messages: list = field(default_factory=list)
    coverage: object = None   # the evaluator's coverage result, with --coverage


def parse_scadtest_file(filepath) -> list[TestCase]:
    """Parse a .scadtest file into TestCases.

    The format is TOML: an optional `[config]` table of defaults, then any
    number of `[[test]]` blocks. Read with the standard library rather than
    a vendored parser -- unlike the docsgen comment syntax, there is nothing
    bespoke here to keep byte-identical.
    """
    path = Path(filepath)
    with open(path, "rb") as f:
        data = tomllib.load(f)

    config = data.get("config", {})

    def cfg(key, default):
        return config.get(key, default)

    tests = []
    for td in data.get("test", []):
        name = td.get("name", "Unnamed Test")
        script = td.get("script")
        script_file = td.get("script_file")
        if script is None and script_file is None:
            raise ValueError(f"Test '{name}' must have either 'script' or 'script_file'.")
        if script is not None and script_file is not None:
            raise ValueError(
                f"Test '{name}' must have either 'script' or 'script_file', not both.")
        if script_file is not None:
            # Relative to the .scadtest file, not the working directory.
            script_file = str(path.parent / script_file)
        tests.append(TestCase(
            name=name,
            script=script,
            script_file=script_file,
            script_dir=str(path.parent),
            timeout=td.get("timeout", cfg("timeout", 60)),
            set_vars=td.get("set_vars", {}),
            expect_success=td.get("expect_success", cfg("expect_success", True)),
            assert_echoes=td.get("assert_echoes", []),
            assert_no_echoes=td.get("assert_no_echoes", cfg("assert_no_echoes", True)),
            assert_warnings=td.get("assert_warnings", []),
            assert_no_warnings=td.get("assert_no_warnings", cfg("assert_no_warnings", True)),
        ))
    return tests


def _scad_literal(v) -> str:
    """A TOML value as OpenSCAD source. bool before int deliberately --
    Python's bool IS an int, and `true` must not come out as `1`."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_scad_literal(x) for x in v) + "]"
    raise ValueError(f"set_vars: cannot express {v!r} as OpenSCAD source")


def _script_lines(tc: TestCase) -> list[str]:
    if tc.script is not None:
        lines = tc.script.splitlines()
    else:
        with open(tc.script_file, encoding="utf-8") as f:
            lines = f.read().splitlines()

    # set_vars are APPENDED as assignments, not seeded as parameters. A
    # top-level variable in OpenSCAD resolves declaratively -- the last
    # assignment of a name wins for every use of it, including uses that
    # appear textually earlier -- so appending is what makes an override
    # actually override. Exactly what -D does; see headless.build_define_prelude,
    # where the semantic was established against the real binary.
    if tc.set_vars:
        lines = lines + [f"{name} = {_scad_literal(val)};"
                         for name, val in tc.set_vars.items()]
    return lines


def run_test(tc: TestCase, coverage: bool = False) -> TestResult:
    """Evaluate one test and judge it.

    The four assertions are the reference's own, in its order: whether the
    script succeeded, then the echoes, then the warnings. `assert_no_echoes`
    is skipped when `assert_echoes` names specific ones, since a test that
    expects an echo cannot also expect none -- again matching the reference,
    which would otherwise contradict itself.
    """
    from belfryscad.docsgen.runner import runner

    try:
        lines = _script_lines(tc)
    except OSError as e:
        return TestResult(tc, False, [f"Could not read script: {e}"])

    def evaluate():
        # generate=False: run the language, build no geometry. The reference
        # renders to a .term CSG dump here, which likewise makes no solids.
        # No params here: set_vars went in as appended assignments above.
        return runner.run(lines, tc.script_dir or ".", generate=False, coverage=coverage)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(evaluate)
        try:
            res = future.result(timeout=tc.timeout)
        except concurrent.futures.TimeoutError:
            return TestResult(tc, False,
                              [f"Test timed out after {tc.timeout} seconds."])
        except Exception as e:  # a crash is a failed test, not a crashed run
            return TestResult(tc, False, [f"Evaluation raised {type(e).__name__}: {e}"])

    result = _judge(tc, res)
    result.coverage = res.coverage   # kept even for a failed test: its code still ran
    return result


def _judge(tc: TestCase, res) -> TestResult:
    """The four assertions, in the reference's order."""
    messages: list[str] = []

    if tc.expect_success and not res.success:
        messages.extend(res.echos)
        messages.extend(res.warnings)
        messages.extend(res.errors)
        return TestResult(tc, False, messages)
    if not tc.expect_success and res.success:
        return TestResult(tc, False, ["Expected test to fail, but it succeeded."])

    for expected in tc.assert_echoes:
        if not any(expected in echo for echo in res.echos):
            messages.append(f"Expected echo not found: {expected}")
            messages.extend(res.echos)
            return TestResult(tc, False, messages)

    if tc.assert_no_echoes and not tc.assert_echoes and res.echos:
        messages.append("Expected no echoes, but got:")
        messages.extend(res.echos)
        return TestResult(tc, False, messages)

    for expected in tc.assert_warnings:
        if not any(expected in w for w in res.warnings):
            messages.append(f"Expected warning not found: {expected}")
            messages.extend(res.warnings)
            return TestResult(tc, False, messages)

    if tc.assert_no_warnings and not tc.assert_warnings and res.warnings:
        messages.append("Expected no warnings, but got:")
        messages.extend(res.warnings)
        return TestResult(tc, False, messages)

    return TestResult(tc, True, [])


def main(argv=None) -> int:
    """CLI. Output and exit code match openscad-test exactly, so a project's
    own runner script (BOSL2's scripts/run_tests.sh calls the binary and
    reads its status) does not have to know which one it got."""
    parser = argparse.ArgumentParser(
        prog="belfryscad --test",
        description="Run OpenSCAD tests defined in .scadtest files.")
    parser.add_argument("files", nargs="*", metavar="file.scadtest")
    parser.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                        help="Number of tests to run at once (default: 1). "
                             "Each is a thread in this process, not a fresh "
                             "OpenSCAD launch, so serial is already faster "
                             "than the reference's parallel default.")
    parser.add_argument("--coverage", action="store_true",
                        help="Record which statements, branch arms and bodies of the LIBRARY files "
                             "the tests exercised, merged over every test, and report it per file "
                             "after the results. The test snippets themselves are left out.")
    parser.add_argument("--coverage-json", metavar="PATH", help="Also write the merged coverage as JSON")
    parser.add_argument("--coverage-min", type=float, metavar="PERCENT",
                        help="Exit 1 if overall coverage is below this, even when every test passed")
    args = parser.parse_args(argv)
    if args.coverage_json or args.coverage_min is not None:
        args.coverage = True

    if not args.files:
        print("Usage: belfryscad --test [-j N] <file.scadtest> [file2.scadtest ...]")
        return 1

    passed = failed = 0
    failed_names = []
    from belfryscad.coverage import CoverageReport, format_report
    merged = CoverageReport() if args.coverage else None

    for filepath in args.files:
        try:
            tests = parse_scadtest_file(filepath)
        except Exception as e:
            print(f"Error parsing {filepath}: {e}", file=sys.stderr)
            return 1

        print(filepath)
        file_passed = file_failed = 0

        if args.jobs == 1 or len(tests) <= 1:
            results = [run_test(tc, args.coverage) for tc in tests]
        else:
            # Threads, not processes: the evaluator releases the GIL for the
            # duration of a script, and a process pool would pay a fresh
            # interpreter and re-import per test.
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
                results = list(ex.map(lambda tc: run_test(tc, args.coverage), tests))

        for result in results:

            if merged is not None and result.coverage:

                merged.merge_spans(result.coverage["spans"])   # main thread: no locking needed
            if result.passed:
                print(f"  {result.test_case.name} PASSED")
                file_passed += 1
                passed += 1
            else:
                print(f"  {result.test_case.name} FAILED")
                for msg in result.messages:
                    print(f"    {msg}")
                file_failed += 1
                failed += 1
                failed_names.append((filepath, result.test_case.name))

        print(f"  {file_passed} of {file_passed + file_failed} passed, {file_failed} failed.")
        print()

    total = passed + failed
    print(f"{passed} of {total} tests passed, {failed} failed.")
    if failed_names:
        print("\nFailed tests:")
        for filepath, name in failed_names:
            print(f"  {filepath}: {name}")

    if merged is not None:
        # The snippets the runner wrote for each test are not the subject.
        from belfryscad.docsgen.runner import _TEMP_PREFIX
        merged.drop_origins(lambda origin: os.path.basename(origin).startswith(_TEMP_PREFIX))
        print("\nCoverage (worst first):")
        print(format_report(merged, base=os.getcwd(), uncovered=False, worst_first=True))
        if args.coverage_json:
            with open(args.coverage_json, "w", encoding="utf-8") as f:
                f.write(merged.to_json())
        if args.coverage_min is not None and merged.total().percent < args.coverage_min:
            print(f"belfryscad: coverage {merged.total().percent:.1f}% is below --coverage-min {args.coverage_min}",
                  file=sys.stderr)
            return 1
    return 1 if failed > 0 else 0
