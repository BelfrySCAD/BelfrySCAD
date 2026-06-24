"""
Tests for debugger stepping behavior.

Each test evaluates OpenSCAD source with a recording debug hook,
then asserts the expected sequence of debug stops (line, depth,
expr_level, origin).
"""
import pytest
from openscad_lalr_parser import getASTfromString, build_scopes

from neuscad.engine.evaluator import Evaluator, EvalError


def _make_recorder():
    """Return (hook_fn, stops_list).

    The hook records every call as a dict and always returns
    ("continue", {}) so execution runs to completion.
    """
    stops = []

    def hook(line, depth, *, forced=False, expr_level=False,
             expr_depth=0, origin=None, get_frames=None):
        stops.append({
            "line": line,
            "depth": depth,
            "forced": forced,
            "expr_level": expr_level,
            "origin": origin,
        })
        return ("continue", {})

    return hook, stops


def _run_with_debug(src: str):
    """Parse, scope, evaluate with a recording debug hook.

    Returns (bodies, echo_lines, stops).
    """
    hook, stops = _make_recorder()
    echo_lines = []
    nodes = getASTfromString(src, include_comments=False)
    root_scope = build_scopes(nodes)
    ev = Evaluator(
        echo_fn=lambda msg: echo_lines.append(msg),
        debug_hook=hook,
    )
    bodies, _ = ev.evaluate(nodes, root_scope)
    return bodies, echo_lines, stops


def _stmt_stops(stops):
    """Filter to statement-level (non-expr_level) stops."""
    return [s for s in stops if not s["expr_level"]]


def _lines(stops):
    """Extract just line numbers from a stops list."""
    return [s["line"] for s in stops]


# ---------------------------------------------------------------------------
# Basic statement stops
# ---------------------------------------------------------------------------

class TestStatementStops:
    def test_sequential_assignments(self):
        _, _, stops = _run_with_debug("a = 1;\nb = 2;\nc = 3;\n")
        lines = _lines(_stmt_stops(stops))
        assert lines == [1, 2, 3]

    def test_assignment_depths_are_zero(self):
        _, _, stops = _run_with_debug("a = 1;\nb = 2;\n")
        for s in _stmt_stops(stops):
            assert s["depth"] == 0

    def test_echo_is_statement_stop(self):
        _, _, stops = _run_with_debug("echo(42);\n")
        stmt = _stmt_stops(stops)
        assert len(stmt) == 1
        assert stmt[0]["line"] == 1


# ---------------------------------------------------------------------------
# Ternary conditionals
# ---------------------------------------------------------------------------

class TestTernaryStops:
    def test_ternary_has_statement_level_stop(self):
        """Ternary condition should produce a statement-level debug stop."""
        _, _, stops = _run_with_debug("a = true ? 1 : 2;\n")
        # Line 1: assignment stop + ternary stop (both statement-level)
        stmt = _stmt_stops(stops)
        stmt_on_line1 = [s for s in stmt if s["line"] == 1]
        # At least 2: the assignment itself and the ternary condition
        assert len(stmt_on_line1) >= 2

    def test_ternary_branch_is_expr_level(self):
        """The chosen ternary branch should be expr_level."""
        _, _, stops = _run_with_debug("a = true ? 1 : 2;\n")
        expr_stops = [s for s in stops if s["expr_level"] and s["line"] == 1]
        assert len(expr_stops) >= 1


# ---------------------------------------------------------------------------
# User-defined function call-site stops
# ---------------------------------------------------------------------------

class TestFunctionCallSiteStops:
    def test_function_call_site_stop(self):
        """Calling a user function should produce a statement-level stop
        at the call site (in caller's context) before entering the function."""
        src = """\
function double(x) = x * 2;
a = double(5);
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # Line 2 should have: assignment stop + call-site stop + function body stop
        line2_stops = [s for s in stmt if s["line"] == 2]
        assert len(line2_stops) >= 2  # assignment + call-site

    def test_function_call_increases_depth(self):
        """Inside a user function, depth should be > 0."""
        src = """\
function double(x) = x * 2;
a = double(5);
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        all_depths = [s["depth"] for s in stmt]
        # Should have depth 0 (assignment, call-site) and depth 1 (function body on line 1)
        assert 0 in all_depths
        assert 1 in all_depths

    def test_no_call_site_stop_for_builtin(self):
        """Built-in functions should NOT produce a call-site debug stop."""
        src = "a = len([1,2,3]);\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # Only one statement-level stop: the assignment
        assert len(stmt) == 1
        assert stmt[0]["line"] == 1

    def test_function_call_in_list(self):
        """Function calls inside list literals should get call-site stops."""
        src = """\
function f(x) = x + 1;
a = [f(1), f(2), f(3)];
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        line2_stops = [s for s in stmt if s["line"] == 2]
        # assignment + 3 call-site stops + 3 function body stops = 7
        assert len(line2_stops) >= 4  # at least assignment + 3 call-sites

    def test_function_literal_call_site_stop(self):
        """Calling a function literal should produce a call-site stop."""
        src = """\
f = function(x) x * 2;
a = f(5);
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        line2_stops = [s for s in stmt if s["line"] == 2]
        assert len(line2_stops) >= 2  # assignment + call-site


# ---------------------------------------------------------------------------
# Let assignments
# ---------------------------------------------------------------------------

class TestLetStops:
    def test_modular_let_stops_at_assignments(self):
        """ModularLet should stop at each assignment, not the let() node."""
        src = """\
let(a = 1, b = 2)
  echo(a + b);
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        lines = _lines(stmt)
        # Should stop at a=1 (line 1), b=2 (line 1), echo (line 2)
        assert 2 in lines  # echo stop

    def test_let_expr_stops_at_assignments(self):
        """let() in expression context should stop at assignments."""
        src = """\
a = let(x = 10, y = 20) x + y;
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # Should have assignment stop + let assignment stops
        assert len(stmt) >= 2  # at least: a= assignment + x=10 let assignment


# ---------------------------------------------------------------------------
# For loop iteration stops
# ---------------------------------------------------------------------------

class TestForLoopStops:
    def test_modular_for_per_iteration_stop(self):
        """ModularFor should fire a debug stop at each iteration."""
        src = """\
for(i = [1:3])
  echo(i);
"""
        _, _, stops = _run_with_debug(src)
        # echo fires once per iteration (3 times), so at least 3 statement stops
        stmt = _stmt_stops(stops)
        echo_stops = [s for s in stmt if s["line"] == 2]
        assert len(echo_stops) == 3

    def test_modular_for_iteration_stops_are_expr_level(self):
        """ModularFor iteration stops should be expr_level (body statements
        already get their own statement-level stops from _eval_statement)."""
        src = """\
for(i = [1:3])
  echo(i);
"""
        _, _, stops = _run_with_debug(src)
        expr_stops = [s for s in stops if s["expr_level"]]
        assert len(expr_stops) >= 3


# ---------------------------------------------------------------------------
# Modular if/else
# ---------------------------------------------------------------------------

class TestModularIfStops:
    def test_if_statement_stop(self):
        """ModularIf should fire a statement-level stop at the if node."""
        src = """\
if (true)
  echo("yes");
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        lines = _lines(stmt)
        assert 1 in lines  # if node
        assert 2 in lines  # echo

    def test_if_branch_has_expr_level_stop(self):
        """The chosen branch of an if should have an expr_level stop."""
        src = """\
if (true)
  echo("yes");
"""
        _, _, stops = _run_with_debug(src)
        expr_stops = [s for s in stops if s["expr_level"]]
        assert len(expr_stops) >= 1


# ---------------------------------------------------------------------------
# List comprehension if stops
# ---------------------------------------------------------------------------

class TestListCompIfStops:
    def test_list_comp_if_is_statement_level(self):
        """List comp if() should produce a statement-level debug stop."""
        src = """\
a = [
    for (i = [0:4])
        if (i <= 2)
            i * 10
];
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        if_stops = [s for s in stmt if s["line"] == 3]
        # 5 iterations, each hitting the if on line 3
        assert len(if_stops) == 5

    def test_list_comp_if_else_is_statement_level(self):
        """List comp if/else should produce a statement-level debug stop."""
        src = """\
a = [
    for (i = [0:2])
        if (i == 1)
            99
        else
            i
];
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        if_stops = [s for s in stmt if s["line"] == 3]
        assert len(if_stops) == 3


# ---------------------------------------------------------------------------
# Expression-level echo and assert stops
# ---------------------------------------------------------------------------

class TestExprEchoAssertStops:
    def test_expr_echo_has_statement_level_stop(self):
        """echo() in expression context should produce a statement-level stop."""
        src = "a = echo(\"hi\") 42;\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # assignment + echo stop
        assert len(stmt) >= 2

    def test_expr_assert_has_statement_level_stop(self):
        """assert() in expression context should produce a statement-level stop."""
        src = "a = assert(true) 42;\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # assignment + assert stop
        assert len(stmt) >= 2

    def test_modular_echo_has_statement_level_stop(self):
        """Modular echo() should produce a statement-level stop."""
        src = "echo(\"hello\");\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        assert len(stmt) == 1
        assert stmt[0]["line"] == 1

    def test_modular_assert_has_statement_level_stop(self):
        """Modular assert() should produce a statement-level stop."""
        src = "assert(true);\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        assert len(stmt) == 1
        assert stmt[0]["line"] == 1


# ---------------------------------------------------------------------------
# List comprehension stops
# ---------------------------------------------------------------------------

class TestListCompStops:
    def test_list_comp_for_iterations_are_statement_level(self):
        """List comp for should produce statement-level stops per iteration."""
        src = "a = [for(i=[1:3]) i];\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # assignment + 3 iteration stops
        assert len(stmt) >= 4

    def test_list_comp_for_with_function_call_alternates(self):
        """Step Over through a list comp for with function calls should
        alternate between the for line and the call line each iteration."""
        src = """\
function fx(x) = x*2;
x = [
    for (i = [0:1:4])
        fx(i+1)
];
"""
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # After the assignment (line 2), we should see iterations:
        # for (line 3) then fx() call-site (line 4), repeated 5 times
        after_assign = [s for s in stmt if s["line"] >= 3]
        for_stops = [s for s in after_assign if s["line"] == 3]
        call_stops = [s for s in after_assign if s["line"] == 4]
        assert len(for_stops) == 5
        assert len(call_stops) >= 5  # call-site + function body stops

    def test_list_comp_let_is_statement_level(self):
        """List comp let assignments should be statement-level stops."""
        src = "a = [let(x=1) x];\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # assignment + let assignment
        assert len(stmt) >= 2


# ---------------------------------------------------------------------------
# Error break
# ---------------------------------------------------------------------------

class TestErrorBreak:
    def test_error_break_fires(self):
        """error_break_fn should be called on evaluation errors."""
        breaks = []

        def error_break(line, msg, all_frame_locals, call_stack, *, origin=None):
            breaks.append({
                "line": line,
                "msg": msg,
                "frame_count": len(all_frame_locals),
            })

        hook, stops = _make_recorder()
        nodes = getASTfromString("assert(false);\n", include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator(
            echo_fn=lambda msg: None,
            debug_hook=hook,
            error_break_fn=error_break,
        )
        with pytest.raises(EvalError):
            ev.evaluate(nodes, root_scope)
        assert len(breaks) == 1
        assert breaks[0]["line"] == 1

    def test_error_break_has_frame_locals(self):
        """Error break should provide frame locals for inspection."""
        breaks = []

        def error_break(line, msg, all_frame_locals, call_stack, *, origin=None):
            breaks.append({"locals": all_frame_locals, "stack": call_stack})

        hook, _ = _make_recorder()
        src = """\
module bad() { assert(false); }
bad();
"""
        nodes = getASTfromString(src, include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator(
            echo_fn=lambda msg: None,
            debug_hook=hook,
            error_break_fn=error_break,
        )
        with pytest.raises(EvalError):
            ev.evaluate(nodes, root_scope)
        assert len(breaks) == 1
        # Should have frame locals and a non-empty call stack
        assert len(breaks[0]["locals"]) >= 1
        assert len(breaks[0]["stack"]) >= 1


# ---------------------------------------------------------------------------
# Forced stops (breakpoint())
# ---------------------------------------------------------------------------

class TestForcedStops:
    def test_breakpoint_is_forced(self):
        """breakpoint() should produce a forced debug stop."""
        src = """\
a = 1;
breakpoint();
b = 2;
"""
        _, _, stops = _run_with_debug(src)
        forced = [s for s in stops if s["forced"]]
        assert len(forced) == 1
        assert forced[0]["line"] == 2
