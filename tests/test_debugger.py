"""
Tests for debugger stepping behavior.

Each test evaluates OpenSCAD source with a recording debug hook,
then asserts the expected sequence of debug stops (line, depth,
expr_level, origin).
"""
import pytest
from openscad_lalr_parser import getASTfromString, build_scopes

from belfryscad.engine.evaluator import Evaluator, EvalContext, EvalError
from belfryscad.window.debugger import _generate_partial_render


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
# Geometry statement stops (ModularCall/modifiers/intersection_for) —
# regression coverage for a bug where _eval_statement's CSG-tree wrapper
# (added in CSG tree Phase 2) intercepts every _TREE_NODE_TYPES node before
# _eval_statement_impl ever runs, but never called _check_debug itself —
# meaning no geometry-producing statement paused the debugger at all, from
# Phase 2 step 1 onward. No existing test caught this since every prior
# test in this file only ever used assignments/control-flow, never a
# geometry statement.
# ---------------------------------------------------------------------------

class TestGeometryStatementStops:
    def test_single_primitive_is_statement_stop(self):
        _, _, stops = _run_with_debug("cube(1);\n")
        stmt = _stmt_stops(stops)
        assert len(stmt) == 1
        assert stmt[0]["line"] == 1

    def test_mixed_assignments_and_geometry_all_stop(self):
        # Assignments run before geometry in the same scope (OpenSCAD
        # semantics), but every statement should still get its own stop.
        _, _, stops = _run_with_debug("a = 1;\ncube(1);\nsphere(1);\nb = 2;\n")
        lines = sorted(_lines(_stmt_stops(stops)))
        assert lines == [1, 2, 3, 4]

    def test_modifier_and_wrapped_child_both_stop(self):
        _, _, stops = _run_with_debug("#cube(1);\n")
        stmt = _stmt_stops(stops)
        assert len(stmt) == 2
        assert all(s["line"] == 1 for s in stmt)

    def test_boolean_op_and_each_child_stop(self):
        _, _, stops = _run_with_debug("union() { cube(1); sphere(1); }\n")
        stmt = _stmt_stops(stops)
        assert len(stmt) == 3

    def test_module_call_stops_at_call_site_and_inside_body(self):
        _, _, stops = _run_with_debug("module foo() { cube(1); }\nfoo();\n")
        stmt = _stmt_stops(stops)
        by_line_depth = sorted((s["line"], s["depth"]) for s in stmt)
        assert by_line_depth == [(1, 1), (2, 0)]


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
# C-style for loop stops
# ---------------------------------------------------------------------------

class TestCStyleForStops:
    """C-style for loop (ListCompCFor) should stop at init assignments,
    the condition, body entry, and incr assignments."""

    # Source with each CFor part on its own line for line-number assertions:
    #   line 1  a = [
    #   line 2      for (             ← CFor node / body-entry stop
    #   line 3          i = 0;        ← init assignment
    #   line 4          i < 2;        ← condition
    #   line 5          i = i + 1     ← incr assignment
    #   line 6      ) i               ← body expression
    #   line 7  ];
    _SRC = "a = [\n    for (\n        i = 0;\n        i < 2;\n        i = i + 1\n    ) i\n];\n"

    def test_init_assignments_are_statement_stops(self):
        """Each init assignment should produce one statement-level stop before the loop."""
        _, _, stops = _run_with_debug(self._SRC)
        init_stops = [s for s in _stmt_stops(stops) if s["line"] == 3]
        assert len(init_stops) == 1  # fires exactly once, before the loop

    def test_condition_is_expr_level(self):
        """Condition should get an expr-level stop each time it is checked,
        including the final false-check that terminates the loop."""
        _, _, stops = _run_with_debug(self._SRC)
        cond_stops = [s for s in stops if s["line"] == 4 and s["expr_level"]]
        # 2 iterations → 2 true checks + 1 false check = 3
        assert len(cond_stops) == 3

    def test_body_entry_fires_per_iteration(self):
        """Each loop body entry should produce a statement-level stop at the for node."""
        _, _, stops = _run_with_debug(self._SRC)
        body_stops = [s for s in _stmt_stops(stops) if s["line"] == 2]
        assert len(body_stops) == 2  # one per iteration

    def test_incr_assignments_are_statement_stops(self):
        """Each incr assignment should produce a statement-level stop per iteration."""
        _, _, stops = _run_with_debug(self._SRC)
        incr_stops = [s for s in _stmt_stops(stops) if s["line"] == 5]
        assert len(incr_stops) == 2  # one per iteration

    def test_multiple_inits_and_incrs(self):
        """Multiple inits and incrs each get their own stops."""
        src = "a = [for (i = 0, j = 0; i < 2; i = i + 1, j = j + 2) i + j];\n"
        _, _, stops = _run_with_debug(src)
        stmt = _stmt_stops(stops)
        # outer assign(1) + 2 inits(2) + 2 body(2) + 2*2 incr(4) = 9 minimum
        assert len(stmt) >= 9

    def test_stop_order(self):
        """Stops should follow init → (cond → body → incr)* → cond(false) order."""
        _, _, stops = _run_with_debug(self._SRC)
        # Build a simplified sequence: (line, expr_level)
        seq = [(s["line"], s["expr_level"]) for s in stops
               if s["line"] in (2, 3, 4, 5)]
        assert seq == [
            (3, False),  # init i=0
            (4, True),   # condition (true, iter 1)
            (2, False),  # body entry (iter 1)
            (5, False),  # incr i=i+1 (iter 1)
            (4, True),   # condition (true, iter 2)
            (2, False),  # body entry (iter 2)
            (5, False),  # incr i=i+1 (iter 2)
            (4, True),   # condition (false, loop ends)
        ]


# ---------------------------------------------------------------------------
# Normal for() loop variable-binding stops
# ---------------------------------------------------------------------------

class TestForAssignmentStops:
    """for() loops should stop at each variable binding, in nested order,
    before each body evaluation."""

    # Listcomp multi-variable source:
    #   line 1  x = [
    #   line 2      for(            ← ListCompFor node
    #   line 3          i=[0:2],    ← i binding (3 values: 0,1,2)
    #   line 4          j=[0:1]     ← j binding (2 values: 0,1)
    #   line 5      )
    #   line 6      i*3+j           ← body expression (expr_level stop)
    #   line 7  ];
    _LC_SRC = "x = [\nfor(\ni=[0:2],\nj=[0:1]\n)\ni*3+j\n];\n"

    # Modular multi-variable source:
    #   line 1  for(                ← ModularFor node
    #   line 2      i=[0:2],        ← i binding (3 values: 0,1,2)
    #   line 3      j=[0:1]         ← j binding (2 values: 0,1)
    #   line 4  )
    #   line 5  echo(i*3+j);        ← body first stmt (expr_level body-entry + stmt echo)
    _MOD_SRC = "for(\ni=[0:2],\nj=[0:1]\n)\necho(i*3+j);\n"

    def test_listcomp_assignment_stops_fire(self):
        """Each variable binding in a listcomp for should produce a statement-level stop."""
        _, _, stops = _run_with_debug(self._LC_SRC)
        i_stops = [s for s in _stmt_stops(stops) if s["line"] == 3]
        j_stops = [s for s in _stmt_stops(stops) if s["line"] == 4]
        # i iterates over [0,1,2] → 3 stops; j iterates [0,1] per i value → 6 stops
        assert len(i_stops) == 3
        assert len(j_stops) == 6

    def test_listcomp_assignment_stop_order(self):
        """Outer variable advances only after inner variable exhausts its range."""
        _, _, stops = _run_with_debug(self._LC_SRC)
        seq = [(s["line"], s["expr_level"]) for s in stops if s["line"] in (3, 4, 6)]
        assert seq == [
            (3, False),              # i=0
            (4, False), (6, True),   # j=0, body
            (4, False), (6, True),   # j=1, body
            (3, False),              # i=1
            (4, False), (6, True),
            (4, False), (6, True),
            (3, False),              # i=2
            (4, False), (6, True),
            (4, False), (6, True),
        ]

    def test_modular_assignment_stops_fire(self):
        """Each variable binding in a modular for should produce a statement-level stop."""
        _, _, stops = _run_with_debug(self._MOD_SRC)
        i_stops = [s for s in _stmt_stops(stops) if s["line"] == 2]
        j_stops = [s for s in _stmt_stops(stops) if s["line"] == 3]
        assert len(i_stops) == 3
        assert len(j_stops) == 6

    def test_modular_assignment_stop_order(self):
        """Outer variable (i) advances only after inner variable (j) exhausts its range."""
        _, _, stops = _run_with_debug(self._MOD_SRC)
        seq = [(s["line"], s["expr_level"]) for s in stops if s["line"] in (2, 3)]
        assert seq == [
            (2, False), (3, False), (3, False),  # i=0, j=0, j=1
            (2, False), (3, False), (3, False),  # i=1, j=0, j=1
            (2, False), (3, False), (3, False),  # i=2, j=0, j=1
        ]


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


# ---------------------------------------------------------------------------
# Phase 3 — live partial-tree rendering during debugging.
#
# _generate_partial_render(ev) is the one piece of new logic testable
# without touching DebugSession/Qt at all (no test in this repo instantiates
# DebugSession or any Qt widget — the rest of Phase 3 is thin signal/UI
# plumbing verified manually). It's called from DebugSession's hook and
# _error_break right before every pause, so the viewport can show whatever's
# been resolved so far.
# ---------------------------------------------------------------------------

class TestGeneratePartialRender:
    def _resolve_only(self, src: str) -> Evaluator:
        """Build ev.csg_tree via the resolve pass only (no generate_tree()
        call yet) — same manual-resolve pattern used in
        TestCSGTreeStep6FinalCutover (test_evaluator.py)."""
        nodes = getASTfromString(src, include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator()
        ev._resolve_use_statements(nodes, root_scope)
        ev.csg_tree = []
        ev._tree_stack = [ev.csg_tree]
        ctx = EvalContext(scope=root_scope)
        ev._root_ctx = ctx
        for node in nodes:
            ev._eval_statement(node, ctx)
        return ev

    def test_success_renders_whatever_is_resolved_so_far(self):
        ev = self._resolve_only("cube(1); sphere(1);")
        bodies, error = _generate_partial_render(ev)
        assert error is None
        assert len(bodies) == 2

    def test_success_on_empty_tree(self):
        # The very first pause (break-on-first) happens before any
        # statement has resolved — csg_tree is empty at that point.
        ev = self._resolve_only("")
        bodies, error = _generate_partial_render(ev)
        assert error is None
        assert bodies == []

    def test_failure_is_caught_and_reported_not_raised(self):
        # A fake evaluator whose generate_tree() raises — deterministic way
        # to exercise the catch-and-report contract without depending on
        # what real generate_fns happen to raise on (Manifold tends to
        # tolerate degenerate input rather than raising).
        class _FakeEvaluator:
            csg_tree: list = []
            _tree_stack: list = [[]]

            def generate_tree(self, tree):
                raise RuntimeError("boom")

        bodies, error = _generate_partial_render(_FakeEvaluator())
        assert bodies is None
        assert error == "boom"

    def test_picks_up_leaves_still_nested_in_an_unfinished_parent(self):
        # Regression: for a script whose whole geometry is one deeply-nested
        # top-level statement (e.g. difference(){union(){cube();sphere();}
        # cylinder();}), ev.csg_tree (the top-level list) stays completely
        # empty for the entire time spent stepping through cube()/sphere(),
        # since difference()'s own CSGNode isn't appended anywhere until
        # every child has finished resolving — including union(), which
        # itself doesn't finish until cube() *and* sphere() both have.
        # _generate_partial_render must still show cube() at this point by
        # looking at every level of ev._tree_stack, not just ev.csg_tree.
        src = "difference() {\n  union() {\n    cube(10);\n    sphere(10);\n  }\n  cylinder(h=10,d=10);\n}\n"
        nodes = getASTfromString(src, include_comments=False)
        root_scope = build_scopes(nodes)
        paused_at_sphere = {}

        def hook(line, depth, *, forced=False, expr_level=False, expr_depth=0, origin=None, get_frames=None):
            if line == 4 and not expr_level and not paused_at_sphere:
                # Paused right at sphere(10) — cube(10) has resolved (its
                # CSGNode sits in union()'s still-in-progress accumulator)
                # but neither union() nor difference() has finished yet.
                paused_at_sphere["bodies"], paused_at_sphere["error"] = _generate_partial_render(ev)
                assert ev.csg_tree == []  # confirms the scenario this test targets
            return ("continue", {})

        ev = Evaluator(debug_hook=hook)
        ev.evaluate(nodes, root_scope)

        assert paused_at_sphere["error"] is None
        assert len(paused_at_sphere["bodies"]) == 1
        assert paused_at_sphere["bodies"][0].body.volume() == pytest.approx(1000)  # cube(10)^3


# ---------------------------------------------------------------------------
# Step to Child — resumes execution until control reaches one of the paused
# call's own children()-forwarded statements, regardless of how much of the
# module's internal logic runs first; falls back to stopping when the call
# returns if it never calls children() at all (same safety net Step Out
# already has, so it can't hang).
#
# This exercises real DebugSession threading (unlike the rest of this file,
# which drives Evaluator's debug_hook directly) because the feature is
# specifically about the interplay between Evaluator._check_debug stashing
# _last_children_positions and DebugSession's hook reading it back at
# resume time — a pure-hook test can't see that half of the mechanism.
# Uses a bare QCoreApplication + a safety-timeout QTimer to pump the event
# loop, since queued cross-thread signals are never delivered without one.
# ---------------------------------------------------------------------------

def _run_debug_session(path: str, on_pause_line, timeout: float = 15.0) -> tuple[list[int], int]:
    """Start a real DebugSession on the file at `path`. `on_pause_line(line)`
    is called at each pause and must return a resume command string (e.g.
    "continue", "step_to_child"). Returns (paused_lines, finished_body_count).

    Deliberately avoids a nested QCoreApplication.exec() — a manual
    processEvents()-and-sleep poll loop instead, driven by a plain
    wall-clock deadline. A prior QTimer-triggered-app.quit() version of this
    helper was flaky specifically on Linux CI (never locally, never on the
    PR that introduced it, and inconsistently across which of the 4 tests
    in this class failed from run to run) — symptomatic of a platform-
    specific re-entrant-event-loop quirk rather than a logic bug in the
    feature itself. Polling avoids relying on nested QCoreApplication.exec()
    semantics at all.
    """
    import sys
    import time as _time
    from PySide6.QtCore import QCoreApplication
    from openscad_lalr_parser import getASTfromFile
    from belfryscad.window.debugger import DebugSession

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    nodes = getASTfromFile(path, include_comments=False)
    root_scope = build_scopes(nodes)

    session = DebugSession()
    paused_lines: list[int] = []
    result = {"count": None}

    def on_paused(origin, line, frames, stk, pbodies, perr):
        paused_lines.append(line)
        session.resume(on_pause_line(line))

    def on_finished(bodies, id2node):
        result["count"] = len(bodies)

    def on_errored(msg):
        result["count"] = -1

    session.paused.connect(on_paused)
    session.finished.connect(on_finished)
    session.errored.connect(on_errored)
    session.start(nodes, root_scope, breakpoints={}, current_file=path)

    deadline = _time.monotonic() + timeout
    while result["count"] is None and _time.monotonic() < deadline:
        app.processEvents()
        _time.sleep(0.001)

    session.paused.disconnect()
    session.finished.disconnect()
    session.errored.disconnect()
    # If the deadline was hit (session still running/paused), stop it so its
    # worker thread doesn't linger blocked into the next test — and either
    # way, join so the next test starts with a clean slate rather than
    # racing a still-finishing thread for the GIL.
    if session.is_running():
        session.stop()
    if session._thread is not None:
        session._thread.join(timeout=5.0)
    return paused_lines, result["count"]


class TestLastChildrenPositions:
    """Evaluator._check_debug stashes self._last_children_positions right
    before calling the debug hook — the (origin, line) pairs of a paused
    ModularCall's own top-level children, which DebugSession reads at
    resume time for Step to Child. Tested directly against Evaluator here
    (no DebugSession/Qt needed), matching this file's usual convention."""

    def test_populated_at_a_call_with_children(self):
        seen = {}

        def hook(line, depth, *, forced=False, expr_level=False,
                 expr_depth=0, origin=None, get_frames=None):
            if line == 4 and not expr_level:
                seen["positions"] = ev._last_children_positions
            return ("continue", {})

        src = "module foo(bar) {\n    echo(bar);\n}\nfoo(1) {\n    cube(42);\n    sphere(13);\n}\n"
        nodes = getASTfromString(src, include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator(debug_hook=hook)
        ev.evaluate(nodes, root_scope)

        assert [line for _origin, line in seen["positions"]] == [5, 6]

    def test_none_for_a_call_with_no_children(self):
        seen = {}

        def hook(line, depth, *, forced=False, expr_level=False,
                 expr_depth=0, origin=None, get_frames=None):
            if line == 1 and not expr_level:
                seen["positions"] = ev._last_children_positions
            return ("continue", {})

        nodes = getASTfromString("cube(1);\n", include_comments=False)
        root_scope = build_scopes(nodes)
        ev = Evaluator(debug_hook=hook)
        ev.evaluate(nodes, root_scope)

        assert seen["positions"] is None


class TestStepToChild:
    def test_stops_at_child_reached_immediately(self, tmp_path):
        src = "module foo(bar) {\n    echo(bar);\n    children();\n}\nfoo(1) {\n    cube(42);\n    sphere(13);\n}\necho(\"Done\");\n"
        path = tmp_path / "step_to_child_immediate.scad"
        path.write_text(src)

        def on_pause(line):
            return "step_to_child" if line == 5 else "continue"

        paused_lines, count = _run_debug_session(str(path), on_pause)
        assert 6 in paused_lines  # cube(42);
        assert count == 2

    def test_reaches_child_after_internal_module_logic(self, tmp_path):
        # Same as above, but the module does several statements of its own
        # bookkeeping before ever calling children() — Step to Child must
        # skip past all of it regardless.
        src = (
            "module foo(bar) {\n"
            "    a = bar + 1;\n"
            "    b = a * 2;\n"
            "    echo(a, b);\n"
            "    children();\n"
            "}\n"
            "foo(1) {\n"
            "    cube(42);\n"
            "    sphere(13);\n"
            "}\n"
            "echo(\"Done\");\n"
        )
        path = tmp_path / "step_to_child_delayed.scad"
        path.write_text(src)

        def on_pause(line):
            return "step_to_child" if line == 7 else "continue"

        paused_lines, count = _run_debug_session(str(path), on_pause)
        assert 8 in paused_lines  # cube(42);
        assert count == 2

    def test_stops_at_whichever_child_children_call_reaches_first(self, tmp_path):
        # children(1) invoked before children(0) — Step to Child should land
        # on whichever child actually runs first (sphere), not the one
        # written first in the caller's { } block (cube).
        src = (
            "module reversed(bar) {\n"
            "    echo(bar);\n"
            "    children(1);\n"
            "    children(0);\n"
            "}\n"
            "reversed(1) {\n"
            "    cube(42);\n"
            "    sphere(13);\n"
            "}\n"
            "echo(\"Done\");\n"
        )
        path = tmp_path / "step_to_child_reversed.scad"
        path.write_text(src)

        def on_pause(line):
            return "step_to_child" if line == 6 else "continue"

        paused_lines, count = _run_debug_session(str(path), on_pause)
        assert 8 in paused_lines  # sphere(13) — children(1), reached first
        assert count == 2

    def test_falls_back_to_call_return_when_children_never_invoked(self, tmp_path):
        # The module ignores its children entirely. Step to Child must not
        # hang — same safety net Step Out already relies on.
        src = (
            "module ignores_children(bar) {\n"
            "    echo(bar);\n"
            "}\n"
            "ignores_children(1) {\n"
            "    cube(42);\n"
            "    sphere(13);\n"
            "}\n"
            "echo(\"Done\");\n"
        )
        path = tmp_path / "step_to_child_no_children.scad"
        path.write_text(src)

        def on_pause(line):
            return "step_to_child" if line == 4 else "continue"

        paused_lines, count = _run_debug_session(str(path), on_pause)
        assert count == 0  # neither cube nor sphere ever evaluated
