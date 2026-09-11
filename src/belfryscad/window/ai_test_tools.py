"""AI tools for the test suite: read, run, and propose changes to tests.

Reads and runs happen here and now, in the AI worker thread, straight through
`belfryscad.scadtest` -- it is Qt-free, so there is no GUI thread to wait on
and the model gets its answer within the call.

Writes do NOT. They go back as a Proposal, the same review bar an ordinary
script edit goes through, because a .scadtest is a file on the user's disk
and the rule for those is already settled: never write one unprompted.

Coverage is the reason several of these exist at all. The model can ask what
a suite misses and get spans back, which is the one question it cannot answer
by reading the source.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from belfryscad.coverage import CoverageReport
from belfryscad.window.ai_tools import AIToolContext, Proposal, _diff, is_path_within

MAX_SPANS = 200


def _tests_dir(ctx: AIToolContext) -> Path | None:
    if ctx.tests_dir is None:
        return None
    directory = ctx.tests_dir()
    return Path(directory) if directory else None


def _resolve_suite(ctx: AIToolContext, path: str) -> "tuple[Path | None, str]":
    """A .scadtest inside the chosen tests directory, or (None, why not)."""
    root = _tests_dir(ctx)
    if root is None:
        return None, ("Error: no tests directory has been chosen. The user "
                      "picks one with Design > Run Tests….")
    candidate = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    if not is_path_within(root.resolve(), candidate):
        return None, f"Error: {path} is outside the tests directory."
    if candidate.suffix != ".scadtest":
        return None, f"Error: {path} is not a .scadtest file."
    if not candidate.is_file():
        return None, f"Error: {path} does not exist."
    return candidate, ""


def _suite_files(ctx: AIToolContext) -> list[Path]:
    from belfryscad.window.testing import find_test_files
    root = _tests_dir(ctx)
    return [Path(p) for p in find_test_files(str(root))] if root else []


def _describe(tc) -> dict:
    out = {"name": tc.name, "timeout": tc.timeout}
    if tc.script_file:
        out["script_file"] = tc.script_file
    else:
        out["script"] = tc.script
    if tc.set_vars:
        out["set_vars"] = tc.set_vars
    if not tc.expect_success:
        out["expect_success"] = False
    if tc.assert_echoes:
        out["assert_echoes"] = list(tc.assert_echoes)
    if not tc.assert_no_echoes:
        out["assert_no_echoes"] = False
    if tc.assert_warnings:
        out["assert_warnings"] = list(tc.assert_warnings)
    if not tc.assert_no_warnings:
        out["assert_no_warnings"] = False
    return out


def list_tests(ctx: AIToolContext) -> str:
    from belfryscad.scadtest import parse_scadtest_file
    root = _tests_dir(ctx)
    if root is None:
        return ("No tests directory has been chosen. The user picks one with "
                "Design > Run Tests….")
    suites = []
    for path in _suite_files(ctx):
        entry = {"path": str(path.relative_to(root))}
        try:
            entry["tests"] = [t.name for t in parse_scadtest_file(path)]
        except Exception as e:  # noqa: BLE001 -- a broken suite is worth reporting, not fatal
            entry["error"] = str(e)
        suites.append(entry)
    if not suites:
        return f"No .scadtest files under {root}."
    return json.dumps({"tests_dir": str(root), "suites": suites}, indent=1)


def read_test(ctx: AIToolContext, path: str, name: str = "") -> str:
    from belfryscad.scadtest import parse_scadtest_file
    suite, why = _resolve_suite(ctx, path)
    if suite is None:
        return why
    try:
        tests = parse_scadtest_file(suite)
    except Exception as e:  # noqa: BLE001
        return f"Error: {path} will not parse: {e}"
    if not name:
        return json.dumps([_describe(t) for t in tests], indent=1)
    found = next((t for t in tests if t.name == name), None)
    if found is None:
        return (f"Error: {path} has no test called {name!r}. It has: "
                + ", ".join(t.name for t in tests))
    return json.dumps(_describe(found), indent=1)


def run_tests(ctx: AIToolContext, path: str = "", coverage: bool = True) -> str:
    """Run a suite (or all of them) here and now, and report what happened."""
    from belfryscad.docsgen.runner import _TEMP_PREFIX
    from belfryscad.scadtest import parse_scadtest_file, run_test
    if path:
        suite, why = _resolve_suite(ctx, path)
        if suite is None:
            return why
        files = [suite]
    else:
        files = _suite_files(ctx)
    if not files:
        return "No .scadtest files to run."

    root = _tests_dir(ctx)
    merged = CoverageReport() if coverage else None
    results, passed, failed = [], 0, 0
    for suite in files:
        try:
            tests = parse_scadtest_file(suite)
        except Exception as e:  # noqa: BLE001
            results.append({"file": str(suite.relative_to(root)), "error": str(e)})
            continue
        for tc in tests:
            result = run_test(tc, coverage=coverage)
            if merged is not None and result.coverage:
                merged.merge_spans(result.coverage["spans"])
            if result.passed:
                passed += 1
            else:
                failed += 1
            results.append({"file": str(suite.relative_to(root)), "test": tc.name,
                            "passed": result.passed,
                            **({"messages": list(result.messages)} if not result.passed else {})})
    out = {"passed": passed, "failed": failed, "results": results}
    if merged is not None:
        merged.drop_origins(lambda o: os.path.basename(o).startswith(_TEMP_PREFIX))
        merged.drop_nocov()
        ctx.last_test_coverage = merged
        total = merged.total()
        out["coverage"] = {
            "percent": round(total.percent, 1),
            "statements": f"{total.statements_hit}/{total.statements}",
            "branches": f"{total.branches_hit}/{total.branches}",
            "bodies": f"{total.bodies_hit}/{total.bodies}",
            "note": "read_coverage_gaps lists what was never reached.",
        }
    return json.dumps(out, indent=1)


def read_coverage_gaps(ctx: AIToolContext, path: str = "") -> str:
    """Every span the last run never reached, as file:line:col kind."""
    report = getattr(ctx, "last_test_coverage", None)
    if report is None and ctx.gui_coverage is not None:
        report = ctx.gui_coverage()
    if report is None:
        return ("No coverage yet. Call run_tests first, or the user can run "
                "them from the Testing pane with Collect coverage ticked.")
    gaps = report.uncovered()
    if path:
        wanted = os.path.basename(path)
        gaps = [g for g in gaps if os.path.basename(g["origin"]) == wanted]
    root = _tests_dir(ctx)
    lines = []
    for span in gaps[:MAX_SPANS]:
        origin = span["origin"]
        if root:
            try:
                origin = str(Path(origin).relative_to(root))
            except ValueError:
                pass
        kind = span["kind"] + (" (if/else arm)" if span["arm"] else "")
        lines.append(f"{origin}:{span['line']}:{span['column']}  {kind}")
    if not lines:
        return "Nothing uncovered." if not path else f"Nothing uncovered in {path}."
    head = f"{len(gaps)} uncovered span(s)"
    if len(gaps) > MAX_SPANS:
        head += f", first {MAX_SPANS} shown"
    return head + ":\n" + "\n".join(lines)


def propose_test_change(ctx: AIToolContext, path: str, action: str, name: str = "",
                        new_name: str = "", script: str = "", script_file: str = "",
                        timeout: int = 0, set_vars: dict | None = None,
                        expect_success: bool | None = None,
                        assert_echoes: list | None = None,
                        assert_no_echoes: bool | None = None,
                        assert_warnings: list | None = None,
                        assert_no_warnings: bool | None = None,
                        anchor: str = "", position: str = "end",
                        summary: str = "") -> str:
    """Add, edit or delete one test -- as a proposal the user reviews."""
    from belfryscad.scadtest import (TestCase, delete_test_block, format_test_block,
                                     insert_test_block, parse_scadtest_file,
                                     replace_test_block)
    if ctx.on_proposal is None:
        return "Error: changes cannot be proposed in this session."
    suite, why = _resolve_suite(ctx, path)
    if suite is None:
        return why
    try:
        text = suite.read_text(encoding="utf-8")
        tests = parse_scadtest_file(suite)
    except Exception as e:  # noqa: BLE001
        return f"Error: {path} will not parse: {e}"
    by_name = {t.name: t for t in tests}

    if action == "delete":
        if name not in by_name:
            return f"Error: {path} has no test called {name!r}."
        new_text = delete_test_block(text, name)
        what = f"Delete test {name!r} from {suite.name}"
    elif action in ("add", "edit"):
        if action == "edit":
            if name not in by_name:
                return f"Error: {path} has no test called {name!r}."
            base = by_name[name]
        else:
            # script=None, not "": the "did you give me a script" check
            # below is `is None`, and an empty string would sail past it and
            # propose a test with no body.
            base = TestCase(name=new_name or name, script=None)
        final_name = new_name or (name if action == "edit" else base.name)
        if not final_name:
            return "Error: a test needs a name."
        if action == "add" and final_name in by_name:
            return f"Error: {path} already has a test called {final_name!r}."
        if action == "edit" and final_name != name and final_name in by_name:
            return f"Error: {path} already has a test called {final_name!r}."
        if script and script_file:
            return "Error: give script or script_file, not both."
        edited = TestCase(
            name=final_name,
            script=script if script else (None if script_file else base.script),
            script_file=script_file or (None if script else base.script_file),
            timeout=timeout or base.timeout,
            set_vars=base.set_vars if set_vars is None else set_vars,
            expect_success=base.expect_success if expect_success is None else expect_success,
            assert_echoes=base.assert_echoes if assert_echoes is None else assert_echoes,
            assert_no_echoes=(base.assert_no_echoes if assert_no_echoes is None
                              else assert_no_echoes),
            assert_warnings=base.assert_warnings if assert_warnings is None else assert_warnings,
            assert_no_warnings=(base.assert_no_warnings if assert_no_warnings is None
                                else assert_no_warnings),
        )
        if edited.script is None and edited.script_file is None:
            return "Error: a test needs a script or a script_file."
        block = format_test_block(edited)
        if action == "edit":
            if final_name != name:
                text = delete_test_block(text, name)
            new_text = replace_test_block(text, final_name, block)
            what = f"Update test {final_name!r} in {suite.name}"
        elif position in ("before", "after") and anchor:
            if anchor not in by_name:
                return f"Error: {path} has no test called {anchor!r} to put this {position}."
            new_text = insert_test_block(text, block, anchor, after=(position == "after"))
            what = f"Add test {final_name!r} {position} {anchor!r} in {suite.name}"
        else:
            new_text = replace_test_block(text, final_name, block)
            what = f"Add test {final_name!r} to {suite.name}"
    else:
        return "Error: action must be add, edit or delete."

    if new_text == text:
        return "That change leaves the file exactly as it is; nothing proposed."
    ctx.on_proposal(Proposal(
        kind="test_edit",
        summary=summary or what,
        new_content=new_text,
        diff_text=_diff(text, new_text, suite.name),
        filename=str(suite),
    ))
    return (f"Proposed: {what}. The user sees the diff and accepts or rejects it; "
            "nothing has been written yet.")


TEST_TOOLS: list[dict] = [
    {
        "name": "list_tests",
        "description": (
            "List the .scadtest suites in the user's tests directory and the "
            "tests in each. Start here: every other test tool takes a suite "
            "path relative to that directory."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": list_tests,
    },
    {
        "name": "read_test",
        "description": (
            "Read one test's full definition -- script, timeout, variable "
            "overrides and every expectation -- or the whole suite when name "
            "is omitted."),
        "json_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Suite path, relative to the tests directory."},
                "name": {"type": "string",
                         "description": "One test. Omit for all of them."},
            },
            "required": ["path"],
        },
        "handler": read_test,
    },
    {
        "name": "run_tests",
        "description": (
            "Run one suite, or every suite when path is omitted, and report "
            "pass/fail per test with the failure messages, plus overall "
            "coverage. Runs immediately and returns the result in this call. "
            "This is the tool that tells you whether a change actually works."),
        "json_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Suite to run. Omit to run all of them."},
                "coverage": {"type": "boolean",
                             "description": "Collect coverage too. Default true."},
            },
            "required": [],
        },
        "handler": run_tests,
    },
    {
        "name": "read_coverage_gaps",
        "description": (
            "List the statements, branch arms and module bodies the last run "
            "never reached, as file:line:col. This is what a suite MISSES -- "
            "the one thing you cannot work out by reading the source. Code "
            "marked /* nocov */ is excluded, as it is from the percentage."),
        "json_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Only gaps in this source file. Omit for all."},
            },
            "required": [],
        },
        "handler": read_coverage_gaps,
    },
    {
        "name": "propose_test_change",
        "description": (
            "Add, edit or delete a test. The user reviews the diff and "
            "accepts or rejects it -- nothing is written when you call this. "
            "Omitted fields keep their current values on an edit, so a "
            "one-field change sends one field."),
        "json_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Suite path, relative to the tests directory."},
                "action": {"type": "string", "enum": ["add", "edit", "delete"]},
                "name": {"type": "string",
                         "description": "The test to edit or delete; the new test's name on add."},
                "new_name": {"type": "string", "description": "Rename the test to this."},
                "script": {"type": "string", "description": "Inline OpenSCAD source."},
                "script_file": {"type": "string",
                                "description": "Run this file instead, relative to the suite."},
                "timeout": {"type": "integer", "description": "Seconds."},
                "set_vars": {"type": "object",
                             "description": "Variable overrides, appended as assignments."},
                "expect_success": {"type": "boolean",
                                   "description": "false if the script is meant to fail."},
                "assert_echoes": {"type": "array", "items": {"type": "string"}},
                "assert_no_echoes": {"type": "boolean"},
                "assert_warnings": {"type": "array", "items": {"type": "string"}},
                "assert_no_warnings": {"type": "boolean"},
                "anchor": {"type": "string",
                           "description": "Existing test to place the new one beside."},
                "position": {"type": "string", "enum": ["end", "before", "after"],
                             "description": "Where to add, relative to anchor. Default end."},
                "summary": {"type": "string",
                            "description": "One line for the review bar."},
            },
            "required": ["path", "action"],
        },
        "handler": propose_test_change,
    },
]
