"""
AST evaluator: walks the openscad_parser AST and produces Manifold geometry.
Returns (manifold_body, id_to_node, colored_meshes) or raises EvalError.
"""
from __future__ import annotations
import math
import random
from typing import Any, Optional
from dataclasses import dataclass, field

import manifold3d as m3d
import numpy as np
from PySide6.QtGui import QColor

from openscad_parser.ast import to_openscad
from openscad_parser.ast.nodes import (
    ASTNode, Assignment, Identifier,
    NumberLiteral, BooleanLiteral, StringLiteral, UndefinedLiteral,
    CommentedExpr,
    ListComprehension, ListCompFor, ListCompCFor, ListCompIf, ListCompIfElse, ListCompLet, ListCompEach,
    PositionalArgument, NamedArgument,
    AdditionOp, SubtractionOp, MultiplicationOp, DivisionOp, ModuloOp, ExponentOp,
    UnaryMinusOp,
    LogicalAndOp, LogicalOrOp, LogicalNotOp,
    EqualityOp, InequalityOp, GreaterThanOp, GreaterThanOrEqualOp, LessThanOp, LessThanOrEqualOp,
    TernaryOp,
    PrimaryCall, PrimaryIndex, PrimaryMember,
    RangeLiteral,
    ModularCall, ModularIf, ModularIfElse, ModularFor, ModularLet,
    ModularEcho, ModularAssert, ModularIntersectionFor,
    ModularModifierShowOnly, ModularModifierHighlight,
    ModularModifierBackground, ModularModifierDisable,
    ModuleDeclaration, FunctionDeclaration, ParameterDeclaration,
    VectorElement,
    LetOp, EchoOp, AssertOp,
    FunctionLiteral,
)


class EvalError(Exception):
    pass


def _scale(scalar, value):
    """Recursively multiply `scalar` into `value`, OpenSCAD-style.

    `value` may be a (possibly nested) list, e.g. a matrix — each element is
    scaled in turn so `2 * [[1,2],[3,4]]` returns `[[2,4],[6,8]]`.
    """
    if isinstance(value, list):
        return [_scale(scalar, v) for v in value]
    if isinstance(scalar, bool) or isinstance(value, bool):
        return None
    try:
        return scalar * value
    except TypeError:
        return None


def _div_scale(value, divisor):
    """Recursively divide `value` (a number or nested list) by `divisor`, OpenSCAD-style.

    `[1,2,3] / 2` -> `[0.5, 1, 1.5]`; nested lists (matrices) recurse like `_scale()`.
    Division by zero follows IEEE 754 (`inf`/`-inf`/`nan`) element-wise.
    """
    if isinstance(value, list):
        return [_div_scale(v, divisor) for v in value]
    if isinstance(value, bool):
        return None
    try:
        if divisor == 0:
            return float('nan') if value == 0 else math.copysign(float('inf'), value)
        return value / divisor
    except TypeError:
        return None


def _vec_add(a, b):
    """OpenSCAD `+` between two values, recursing element-wise into nested lists.

    `[1,2,3] + [4,5,6]` -> `[5,7,9]`; matrices (lists of vectors) recurse so
    `[[0,0,0,0]] + [[1,1,1,1]]` -> `[[1,1,1,1]]` rather than concatenating
    each row's elements.
    """
    if isinstance(a, list) and isinstance(b, list):
        return [_vec_add(x, y) for x, y in zip(a, b)]
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    if isinstance(a, str) or isinstance(b, str):
        # OpenSCAD has no `+` for strings (unlike Python's `str.__add__`).
        return None
    try:
        return a + b
    except TypeError:
        return None


def _point_seg_dist(p, a, b):
    """Euclidean distance from 2D point `p` to segment `a`-`b`."""
    ab = b - a
    denom = np.dot(ab, ab)
    t = np.dot(p - a, ab) / denom if denom else 0.0
    t = max(0.0, min(1.0, t))
    return float(np.linalg.norm(p - (a + t * ab)))


def _point_in_poly_evenodd(p, edges):
    """Even-odd ray-casting point-in-polygon test against a flat list of (a, b) edges."""
    x, y = p
    inside = False
    for a, b in edges:
        x1, y1 = a
        x2, y2 = b
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def _vec_sub(a, b):
    """OpenSCAD `-` between two values, recursing element-wise into nested lists.

    See `_vec_add()` — matrices (lists of vectors) recurse row-by-row.
    """
    if isinstance(a, list) and isinstance(b, list):
        return [_vec_sub(x, y) for x, y in zip(a, b)]
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    try:
        return a - b
    except TypeError:
        return None


def _osc_type_name(v) -> str:
    """OpenSCAD's name for `v`'s type, as used in 'undefined operation (...)' warnings."""
    if v is None:
        return "undefined"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "vector"
    if isinstance(v, OscObject):
        return "object"
    return "undefined"


def _object_arg_type_name(v) -> str:
    """Type name as used in `object()`'s own argument-validation warnings
    (`<number>`, `<string>`, `<list>`, ... `<undef>`) — distinct spelling from
    `_osc_type_name()`'s `undefined`/`vector`."""
    if v is None:
        return "undef"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, OscRange):
        return "range"
    if isinstance(v, OscObject):
        return "object"
    if isinstance(v, (FunctionDeclaration, FunctionLiteral)):
        return "function"
    return "undef"


def _osc_equal(a, b) -> bool:
    """OpenSCAD `==`: unlike Python, `bool` is not interchangeable with `int`/`float`,
    so `1 == true` and `0 == false` are `false`. Recurses into lists element-wise.

    `object()` equality is deep AND order-sensitive: two objects with the same
    keys/values in a different order are NOT equal, matching real OpenSCAD."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_osc_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, OscObject) and isinstance(b, OscObject):
        pairs_a, pairs_b = list(a.items()), list(b.items())
        return len(pairs_a) == len(pairs_b) and all(
            ka == kb and _osc_equal(va, vb)
            for (ka, va), (kb, vb) in zip(pairs_a, pairs_b)
        )
    return a == b


def _osc_comparable(a, b) -> bool:
    """Whether `<`/`>`/`<=`/`>=` are defined between `a` and `b` in OpenSCAD.

    Ordering is only defined between two values of the *same* type:
    number-number (int/float mix ok), string-string, vector-vector, or
    bool-bool. Any other pairing (e.g. `true > 0`, `"a" < 1`, `[1,2] < 5`)
    is an "undefined operation".
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return True
    if isinstance(a, str) and isinstance(b, str):
        return True
    if isinstance(a, list) and isinstance(b, list):
        return True
    return False


def _format_number(v: float) -> str:
    """Format a number the way OpenSCAD's `echo()`/`str()` do.

    Differs from Python's `f"{v:g}"` in two ways:
    - exponents drop their leading zero (`1e+08` -> `1e+8`, `1e-07` -> `1e-7`)
    - small numbers stay in fixed notation one digit further than `%g`
      (`1e-5` -> `0.00001`, where `%g` would give `1e-05`); fixed notation
      covers exponents in `[-5, 5]`, vs. `%g`'s `[-4, 5]`.
    Both still show at most 6 significant digits, and `-0.0` -> `"0"`.
    """
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    if v == 0:
        return "0"

    neg = v < 0
    av = abs(v)
    exp = math.floor(math.log10(av))
    mantissa = round(av / (10 ** exp), 5)
    if mantissa >= 10:
        mantissa /= 10
        exp += 1

    if -5 <= exp <= 5:
        decimals = max(0, 5 - exp)
        s = f"{av:.{decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
    else:
        m = f"{mantissa:.5f}".rstrip("0").rstrip(".")
        s = f"{m}e{'+' if exp >= 0 else '-'}{abs(exp)}"
    return ("-" + s) if neg else s


def _matmul(a, b):
    """OpenSCAD `*` between two lists: vector/matrix (dot/matrix) product.

    - vector * vector -> scalar (dot product)
    - matrix * vector -> vector (matrix-vector product)
    - vector * matrix -> vector (vector-matrix product)
    - matrix * matrix -> matrix (matrix product)
    Returns `None` (undef) on dimension mismatches or non-numeric entries.
    """
    a_is_mat = bool(a) and isinstance(a[0], list)
    b_is_mat = bool(b) and isinstance(b[0], list)
    try:
        if not a_is_mat and not b_is_mat:
            if len(a) != len(b):
                return None
            return sum(x * y for x, y in zip(a, b))
        if a_is_mat and not b_is_mat:
            if any(len(row) != len(b) for row in a):
                return None
            return [sum(x * y for x, y in zip(row, b)) for row in a]
        if not a_is_mat and b_is_mat:
            if len(a) != len(b):
                return None
            cols = len(b[0])
            if any(len(row) != cols for row in b):
                return None
            return [sum(a[i] * b[i][j] for i in range(len(a))) for j in range(cols)]
        # matrix * matrix
        if not a or not b or len(a[0]) != len(b):
            return None
        cols = len(b[0])
        if any(len(row) != cols for row in b) or any(len(row) != len(a[0]) for row in a):
            return None
        return [[sum(arow[k] * b[k][j] for k in range(len(b))) for j in range(cols)] for arow in a]
    except TypeError:
        return None


class OscRange:
    """Lazy OpenSCAD range value — echoes as [start : step : end], iterable, indexable."""
    __slots__ = ("start", "step", "end")

    def __init__(self, start: float, step: float, end: float):
        self.start = start
        self.step = step
        self.end = end

    def __iter__(self):
        if self.step == 0:
            return
        v = self.start
        if self.step > 0:
            while v <= self.end + 1e-10:
                yield v
                v += self.step
        else:
            while v >= self.end - 1e-10:
                yield v
                v += self.step

    def __getitem__(self, idx: int):
        # OpenSCAD indexes a range as its 3 components, not its iterated values:
        # `[2:3:11][0]` -> 2 (start), `[1]` -> 3 (step), `[2]` -> 11 (end).
        return (self.start, self.step, self.end)[idx] if 0 <= idx <= 2 else None

    def __repr__(self):
        return f"OscRange({self.start}, {self.step}, {self.end})"


class OscObject:
    """OpenSCAD `object()` value — an ordered string-keyed map."""
    __slots__ = ("data",)

    def __init__(self, data: dict):
        self.data = data

    def __iter__(self):
        return iter(self.data)  # keys, in insertion order

    def __len__(self):
        return len(self.data)

    def get(self, key):
        return self.data.get(key)  # missing key -> None (undef)

    def items(self):
        return self.data.items()

    def __repr__(self):
        return f"OscObject({self.data!r})"


@dataclass
class ColoredBody:
    """A Manifold body (3D) or CrossSection (2D) paired with an optional RGBA color."""
    body: Optional[m3d.Manifold] = None
    color: Optional[tuple[float, float, float, float]] = None  # RGBA 0-1
    section: Optional[m3d.CrossSection] = None  # set for 2D primitives


@dataclass
class EvalContext:
    """Mutable evaluation state threaded through recursive calls."""
    # Lexical scope (from build_scopes)
    scope: Any
    # Dynamic variables ($fn, $fa, $fs, $t, etc.) — call-chain inherited
    dyn: dict[str, Any] = field(default_factory=lambda: {"$fn": 0, "$fa": 12.0, "$fs": 2.0})
    # Positions of assignments stored in dyn (for double-assignment warnings)
    dyn_positions: dict[str, Any] = field(default_factory=dict)
    # Optional color propagated from parent color() call
    color: Optional[tuple[float, float, float, float]] = None
    # Children passed into a module call (for children() built-in)
    children_bodies: list[ColoredBody] = field(default_factory=list)

    def child_ctx(self, scope=None, dyn=None, color=None, children_bodies=None):
        return EvalContext(
            scope=scope if scope is not None else self.scope,
            dyn=dyn if dyn is not None else dict(self.dyn),
            dyn_positions={} if dyn is None else self.dyn_positions,
            color=color if color is not None else self.color,
            children_bodies=children_bodies if children_bodies is not None else [],
        )

    def call_ctx(self, scope=None, color=None, children_bodies=None):
        """Child context for a module/function call: inherits only $-prefixed
        dynamic bindings, not __let_* variable bindings from the caller's scope."""
        dyn = {k: v for k, v in self.dyn.items() if k.startswith('$')}
        return EvalContext(
            scope=scope if scope is not None else self.scope,
            dyn=dyn,
            dyn_positions={},
            color=color if color is not None else self.color,
            children_bodies=children_bodies if children_bodies is not None else [],
        )


class Evaluator:
    def __init__(self, echo_fn=None, debug_hook=None, error_break_fn=None):
        self.id_to_node: dict[int, ASTNode] = {}
        self._errors: list[str] = []
        self._echo_fn = echo_fn or (lambda msg: print(msg))
        # Module frames: ("module", name, call_pos, decl_pos); function frames: ("function", name, call_pos)
        self._call_stack: list = []
        # EvalContext for each live call-stack frame (parallel to _call_stack)
        self._frame_ctxs: list = []
        self._debug_hook = debug_hook  # callable(line, locals_dict, call_stack, all_frame_locals) -> (cmd, mods)
        self._error_break_fn = error_break_fn  # callable(line, msg, all_frame_locals, call_stack); returns, then EvalError raised
        self._last_locals: dict = {}
        self._last_all_frame_locals: list = []
        self._root_ctx: EvalContext | None = None
        self._expr_depth: int = 0  # nesting depth inside listcomp for/if/each/listcomp bodies

    def _check_debug(self, node: ASTNode, ctx: EvalContext, forced: bool = False, expr_level: bool = False):
        if self._debug_hook is None:
            return
        pos = getattr(node, 'position', None)
        line = getattr(pos, 'line', None) if pos else None
        if line is None:
            return

        # local_scope: all eagerly-assigned vars in the current frame's dyn
        # outer_scope: global vars from the root context (shown when inside a call)
        # dyn_names:   subset of local_scope that live in dyn (user-modifiable)
        local_scope: dict = {}
        dyn_names: set = set()

        for k, v in ctx.dyn.items():
            if k.startswith('__let_'):
                name = k[6:]
                local_scope[name] = v
                dyn_names.add(name)
            elif k.startswith('$'):
                local_scope[k] = v

        outer_scope: dict = {}
        if self._call_stack and self._root_ctx is not None:
            for k, v in self._root_ctx.dyn.items():
                if k.startswith('__let_'):
                    name = k[6:]
                    if name not in local_scope:
                        outer_scope[name] = v

        current_frame = {"local_scope": local_scope, "outer_scope": outer_scope, "dyn_names": dyn_names}
        all_frame_locals = [current_frame]
        for frame_ctx in reversed(self._frame_ctxs[:-1]):
            p_local: dict = {}
            p_dyn: set = set()
            for k, v in frame_ctx.dyn.items():
                if k.startswith('__let_'):
                    name = k[6:]
                    p_local[name] = v
                    p_dyn.add(name)
                elif k.startswith('$'):
                    p_local[k] = v
            all_frame_locals.append({"local_scope": p_local, "outer_scope": {}, "dyn_names": p_dyn})

        # When inside a call, append a <toplevel> frame whose locals are the global (outer) vars.
        if self._call_stack:
            toplevel_frame = {
                "local_scope": dict(outer_scope),
                "outer_scope": {},
                "dyn_names": set(),
            }
            all_frame_locals.append(toplevel_frame)

        self._last_locals = {n: v for n, v in local_scope.items() if n in dyn_names}
        self._last_all_frame_locals = all_frame_locals

        cmd, mods = self._debug_hook(int(line), self._last_locals, list(self._call_stack), all_frame_locals, forced=forced, expr_level=expr_level, expr_depth=self._expr_depth)
        for k, v in mods.items():
            ctx.dyn[f'__let_{k}'] = v
        if cmd == "stop":
            raise EvalError("Debugging stopped.")

    @staticmethod
    def _loc(pos) -> str:
        if pos is None:
            return ""
        return f" in file {pos.origin}, line {pos.line}"

    def _trace_lines(self, node=None, innermost_frame: str | None = None) -> list[str]:
        """Build TRACE lines matching OpenSCAD's error/warning format."""
        lines = []
        node_pos = getattr(node, 'position', None) if node is not None else None
        if innermost_frame:
            lines.append(f"TRACE: called by '{innermost_frame}'{self._loc(node_pos)}")
        for entry in reversed(self._call_stack):
            kind = entry[0]
            fname = entry[1]
            call_pos = entry[2]
            if kind == "module":
                decl_pos = entry[3] if len(entry) > 3 else None
                lines.append(f"TRACE: call of '{fname}()'{self._loc(decl_pos)}")
                lines.append(f"TRACE: called by '{fname}'{self._loc(call_pos)}")
            else:
                lines.append(f"TRACE: called by '{fname}'{self._loc(call_pos)}")
        return lines

    def error(self, msg: str, node=None, innermost_frame: str | None = None):
        pos = getattr(node, 'position', None) if node is not None else None
        header = f"ERROR: {msg}{self._loc(pos)}"
        lines = [header] + self._trace_lines(node, innermost_frame)
        full = "\n".join(lines)
        self._errors.append(full)
        if self._error_break_fn is not None:
            line = getattr(pos, 'line', 0) if pos else 0
            self._error_break_fn(int(line), header, self._last_all_frame_locals, list(self._call_stack))
        raise EvalError(full)

    def _fmt_val(self, v) -> str:
        if v is None:
            return "undef"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, OscRange):
            return f"[{_format_number(v.start)} : {_format_number(v.step)} : {_format_number(v.end)}]"
        if isinstance(v, float):
            return _format_number(v)
        if isinstance(v, list):
            return "[" + ", ".join(self._fmt_val(x) for x in v) + "]"
        if isinstance(v, OscObject):
            if len(v) == 0:
                return "{ }"
            return "{ " + "".join(f"{k} = {self._fmt_val(val)}; " for k, val in v.items()) + "}"
        if isinstance(v, str):
            return f'"{v}"'
        return str(v)

    def _do_echo(self, arguments, ctx: "EvalContext"):
        parts = []
        for arg in arguments:
            val = self._eval_expr(arg.expr, ctx)
            if isinstance(arg, NamedArgument):
                parts.append(f"{arg.name.name} = {self._fmt_val(val)}")
            else:
                parts.append(self._fmt_val(val))
        self._echo_fn("ECHO: " + ", ".join(parts))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def evaluate(self, nodes: list[ASTNode], root_scope, viewport_params: dict | None = None) -> tuple[list[ColoredBody], dict[int, ASTNode]]:
        """Walk top-level AST nodes and return (geometry, id_to_node mapping)."""
        self._call_stack.clear()
        self._frame_ctxs.clear()
        ctx = EvalContext(scope=root_scope)
        if viewport_params:
            ctx.dyn.update(viewport_params)
        self._root_ctx = ctx
        result = []
        # OpenSCAD executes all assignments before geometry in each scope.
        assignments = [n for n in nodes if isinstance(n, Assignment)]
        others = [n for n in nodes if not isinstance(n, Assignment)]
        for node in assignments + others:
            bodies = self._eval_statement(node, ctx)
            result.extend(bodies)
        return result, self.id_to_node

    # ------------------------------------------------------------------
    # Statement dispatch
    # ------------------------------------------------------------------

    def _eval_statement(self, node: ASTNode, ctx: EvalContext) -> list[ColoredBody]:
        if not isinstance(node, (ModuleDeclaration, FunctionDeclaration)):
            self._check_debug(node, ctx)
        if isinstance(node, Assignment):
            name = node.name.name
            if name.startswith("$"):
                ctx.dyn[name] = self._eval_expr(node.expr, ctx)
            else:
                key = f'__let_{name}'
                pos = getattr(node, 'position', None)
                # Only warn on a genuine double-assignment within this scope.
                # A parameter binding (from _bind_args/_apply_defaults) also
                # sets ctx.dyn[key] but has no dyn_positions entry — a body
                # assignment shadowing a parameter (e.g. `anchor =
                # default(anchor, CENTER);`) is normal and not a warning.
                if key in ctx.dyn_positions:
                    first_pos = ctx.dyn_positions[key]
                    first_line = getattr(first_pos, 'line', '?') if first_pos else '?'
                    self._echo_fn(
                        f"WARNING: {name} was assigned on line {first_line}"
                        f" but was overwritten{self._loc(pos)}"
                    )
                ctx.dyn[key] = self._eval_expr(node.expr, ctx)
                ctx.dyn_positions[key] = pos
            return []
        if isinstance(node, ModularCall):
            body = self._eval_modular_call(node, ctx)
            return [body] if body is not None else []
        if isinstance(node, ModularIf):
            cond = self._eval_expr(node.condition, ctx)
            if cond:
                branch = node.true_branch
                self._check_debug(branch[0] if branch else node, ctx, expr_level=True)
                return self._eval_children(branch, ctx)
            return []
        if isinstance(node, ModularIfElse):
            cond = self._eval_expr(node.condition, ctx)
            branch = node.true_branch if cond else node.false_branch
            self._check_debug(branch[0] if branch else node, ctx, expr_level=True)
            return self._eval_children(branch, ctx)
        if isinstance(node, ModularFor):
            return self._eval_for(node, ctx)
        if isinstance(node, ModularIntersectionFor):
            return self._eval_intersection_for(node, ctx)
        if isinstance(node, ModularLet):
            return self._eval_let_block(node, ctx)
        if isinstance(node, ModularEcho):
            self._do_echo(node.arguments, ctx)
            return []
        if isinstance(node, ModularAssert):
            args = self._resolve_args(node.arguments, ctx)
            cond = self._get_arg(args, 0, "condition", True)
            if not cond:
                raw = node.arguments
                cond_text = to_openscad([raw[0].expr]).strip() if raw else "false"
                msg = self._get_arg(args, 1, "message", None)
                err = f"Assertion '{cond_text}' failed" + (f': "{msg}"' if msg is not None else "")
                self.error(err, node, innermost_frame="assert")
            return []
        if isinstance(node, (ModularModifierShowOnly, ModularModifierHighlight)):
            return self._eval_statement(node.child, ctx)
        if isinstance(node, (ModularModifierBackground, ModularModifierDisable)):
            return []
        if isinstance(node, (ModuleDeclaration, FunctionDeclaration)):
            return []
        return []

    def _eval_children(self, children, ctx: EvalContext) -> list[ColoredBody]:
        result = []
        # OpenSCAD executes all assignments before geometry in each scope.
        assignments = [c for c in children if isinstance(c, Assignment)]
        others = [c for c in children if not isinstance(c, Assignment)]
        for child in assignments + others:
            # Use the node's own scope from build_scopes when available so that
            # each node evaluates in its correct lexical scope. Share ctx.dyn
            # (not a copy) so that eager assignments in one sibling are visible
            # to subsequent siblings in the same block.
            if hasattr(child, 'scope') and child.scope is not None:
                child_ctx = EvalContext(
                    scope=child.scope,
                    dyn=ctx.dyn,
                    dyn_positions=ctx.dyn_positions,
                    color=ctx.color,
                    children_bodies=ctx.children_bodies,
                )
            else:
                child_ctx = ctx
            result.extend(self._eval_statement(child, child_ctx))
        return result

    # ------------------------------------------------------------------
    # Module call dispatch
    # ------------------------------------------------------------------

    def _eval_modular_call(self, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        name = node.name.name
        user_mod = ctx.scope.lookup_module(name)
        if user_mod is not None:
            return self._eval_user_module(user_mod, node, ctx)
        return self._eval_builtin(name, node, ctx)

    @staticmethod
    def _pos_contains(outer, inner) -> bool:
        """True if `inner`'s source span is strictly contained within `outer`'s.

        Used to detect "`inner` is declared lexically inside `outer`'s body"
        (e.g. a nested `module`/`function`). Identical spans (a declaration
        calling itself — direct recursion) are NOT considered contained.
        """
        if outer is None or inner is None:
            return False
        if outer.origin != inner.origin:
            return False
        if (outer.start_offset, outer.end_offset) == (inner.start_offset, inner.end_offset):
            return False
        return outer.start_offset <= inner.start_offset and inner.end_offset <= outer.end_offset

    def _call_ctx_for(self, decl, ctx: EvalContext, scope=None, children_bodies=None) -> EvalContext:
        """Build the child context for a module/function call.

        A declaration nested inside the body of a currently-executing
        module/function (e.g. BOSL2's `cuboid()` defines a local `module
        corner_shape() {...}` that references `cuboid`'s local `edges`
        variable) is a closure over that call's locals, so it inherits
        `ctx.dyn` (including `__let_*` bindings) via `child_ctx`. A
        top-level declaration, or a declaration calling itself
        (recursion), only inherits `$`-prefixed dynamic vars, per
        `call_ctx` — otherwise a recursive call would see its own
        in-progress local variables as if they were its caller's.
        """
        decl_pos = getattr(decl, 'position', None)
        nested = any(self._pos_contains(frame[-1], decl_pos) for frame in self._call_stack)
        if nested:
            return ctx.child_ctx(scope=scope, children_bodies=children_bodies)
        return ctx.call_ctx(scope=scope, children_bodies=children_bodies)

    def _eval_user_module(self, decl: ModuleDeclaration, call: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        # Bind parameters
        child_scope = decl.scope if hasattr(decl, 'scope') and decl.scope else ctx.scope
        params = decl.parameters if hasattr(decl, 'parameters') else []
        args = self._bind_args(params, call.arguments, ctx)

        # Evaluate children in caller's ctx so they become available via children()
        caller_bodies = self._eval_children(call.children, ctx)

        child_ctx = self._call_ctx_for(
            decl, ctx,
            scope=child_scope,
            children_bodies=caller_bodies,
        )
        # $children is the number of module-instantiation children passed in
        # `{}`, not the number of geometries they produced — e.g. `children()`
        # counts as one child even if the caller passed it none to forward.
        child_ctx.dyn["$children"] = len([
            c for c in call.children
            if not isinstance(c, (Assignment, ModuleDeclaration, FunctionDeclaration))
        ])
        # Bind all args; $-prefixed go into dyn directly, others as __let_
        for k, v in args.items():
            if k.startswith("$"):
                child_ctx.dyn[k] = v
            else:
                child_ctx.dyn[f"__let_{k}"] = v
        # Apply defaults for missing params
        self._apply_defaults(params, child_ctx, ctx)

        name = call.name.name
        call_pos = getattr(call, 'position', None)
        decl_pos = getattr(decl, 'position', None)
        self._call_stack.append(("module", name, call_pos, decl_pos))
        self._frame_ctxs.append(child_ctx)
        try:
            module_body = getattr(decl, 'children', None) or getattr(decl, 'body', None) or []
            bodies = self._eval_children(module_body, child_ctx)
            if not bodies:
                return None
            return self._combine(bodies)
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()

    def _bind_args(self, params, arguments, ctx: EvalContext) -> dict[str, Any]:
        result = {}
        positional_idx = 0
        for arg in arguments:
            if isinstance(arg, NamedArgument):
                result[arg.name.name] = self._eval_expr(arg.expr, ctx)
            elif isinstance(arg, PositionalArgument):
                if positional_idx < len(params):
                    param = params[positional_idx]
                    pname = param.name.name if hasattr(param, 'name') else str(positional_idx)
                    result[pname] = self._eval_expr(arg.expr, ctx)
                positional_idx += 1
        return result

    # ------------------------------------------------------------------
    # Built-in modules
    # ------------------------------------------------------------------

    def _eval_builtin(self, name: str, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        args = self._resolve_args(node.arguments, ctx)
        # $-prefixed named args (e.g. $fn=32) override the dynamic context for this call
        dyn_overrides = {k: v for k, v in args.items() if isinstance(k, str) and k.startswith("$")}
        if dyn_overrides:
            ctx = ctx.child_ctx(dyn={**ctx.dyn, **dyn_overrides})

        if name == "cube":
            return self._builtin_cube(args, node, ctx)
        if name == "sphere":
            return self._builtin_sphere(args, node, ctx)
        if name == "cylinder":
            return self._builtin_cylinder(args, node, ctx)
        if name in ("translate", "rotate", "scale", "mirror", "resize", "multmatrix"):
            return self._builtin_transform(name, args, node, ctx)
        if name == "color":
            return self._builtin_color(args, node, ctx)
        if name == "union":
            return self._builtin_csg("union", node, ctx)
        if name == "difference":
            return self._builtin_csg("difference", node, ctx)
        if name == "intersection":
            return self._builtin_csg("intersection", node, ctx)
        if name == "hull":
            return self._builtin_hull(node, ctx)
        if name == "minkowski":
            return self._builtin_minkowski(node, ctx)
        if name == "polyhedron":
            return self._builtin_polyhedron(args, node, ctx)
        if name in ("circle", "square", "polygon"):
            return self._builtin_2d(name, args, node, ctx)
        if name == "offset":
            return self._builtin_offset(args, node, ctx)
        if name == "projection":
            return self._builtin_projection(args, node, ctx)
        if name == "linear_extrude":
            return self._builtin_linear_extrude(args, node, ctx)
        if name == "rotate_extrude":
            return self._builtin_rotate_extrude(args, node, ctx)
        if name == "roof":
            return self._builtin_roof(args, node, ctx)
        if name == "render":
            # render() is a display hint; just pass through children
            children = self._eval_children(node.children, ctx)
            return self._combine(children) if children else None
        if name == "surface":
            return self._builtin_surface(args, node, ctx)
        if name in ("text", "import"):
            self._echo_fn(f"WARNING: {name}() is not yet implemented")
            return None
        if name == "echo":
            self._do_echo(node.arguments, ctx)
            return None
        if name == "assert":
            return None
        if name == "children":
            return self._builtin_children(args, ctx)
        if name == "breakpoint":
            return self._builtin_breakpoint(args, node, ctx)
        # Unknown module — warn with call stack, matching OpenSCAD's WARNING format
        pos = getattr(node, 'position', None)
        warn = f"WARNING: Ignoring unknown module '{name}'{self._loc(pos)}"
        trace = self._trace_lines(node)
        self._echo_fn("\n".join([warn] + trace))
        return None

    def _resolve_args(self, arguments, ctx: EvalContext) -> dict:
        """Resolve call arguments into a dict: positional args keyed 0, 1, ... and named by name."""
        result = {}
        pos = 0
        for arg in arguments:
            if isinstance(arg, PositionalArgument):
                result[pos] = self._eval_expr(arg.expr, ctx)
                pos += 1
            elif isinstance(arg, NamedArgument):
                result[arg.name.name] = self._eval_expr(arg.expr, ctx)
        return result

    def _get_arg(self, args: dict, pos: int, name: str, default=None):
        if name in args:
            return args[name]
        if pos in args:
            return args[pos]
        return default

    # --- primitives ---

    def _tag(self, body: m3d.Manifold, node: ASTNode, ctx: EvalContext) -> ColoredBody:
        for orig_id in body.to_mesh().run_original_id:
            self.id_to_node[int(orig_id)] = node
        return ColoredBody(body=body, color=ctx.color)

    def _fn(self, ctx: EvalContext) -> int:
        fn = ctx.dyn.get("$fn", 0)
        fa = ctx.dyn.get("$fa", 12.0)
        fs = ctx.dyn.get("$fs", 2.0)
        if fn > 0:
            return max(3, int(fn))
        # approximate from $fa/$fs: use OpenSCAD formula later, default 16
        return 16

    def _builtin_cube(self, args: dict, node: ModularCall, ctx: EvalContext) -> ColoredBody:
        size = self._get_arg(args, 0, "size", 1.0)
        center = bool(self._get_arg(args, 1, "center", False))
        if isinstance(size, (int, float)):
            size = [size, size, size]
        size = [float(s) for s in size]
        body = m3d.Manifold.cube(size, center)
        return self._tag(body, node, ctx)

    def _builtin_sphere(self, args: dict, node: ModularCall, ctx: EvalContext) -> ColoredBody:
        r = self._get_arg(args, 0, "r", None)
        d = self._get_arg(args, None, "d", None)
        if d is not None:
            r = d / 2
        if r is None:
            r = 1.0
        r = float(r)
        n = self._fn(ctx)  # longitude segments
        stacks = max(2, int(math.ceil(n / 2)))  # number of latitude rings (no single-point poles)

        # OpenSCAD-compatible sphere: polygon caps at top/bottom (no triangulated poles),
        # quad belts between rings. Rings evenly spaced excluding the actual poles.
        step = math.pi / stacks  # latitude step in radians
        verts = []
        rings = []  # rings[i] = list of vertex indices

        for s in range(stacks):
            lat = -math.pi / 2 + (s + 0.5) * step
            ring_r = r * math.cos(lat)
            z = r * math.sin(lat)
            ring = []
            for seg in range(n):
                angle = 2 * math.pi * seg / n
                ring.append(len(verts))
                verts.append([ring_r * math.cos(angle), ring_r * math.sin(angle), z])
            rings.append(ring)

        tris = []

        # Bottom polygon cap: fan with reversed winding → outward normal points down
        bot = rings[0]
        for i in range(1, n - 1):
            tris.append([bot[0], bot[i + 1], bot[i]])

        # Quad belts between adjacent rings
        for s in range(stacks - 1):
            lo, hi = rings[s], rings[s + 1]
            for seg in range(n):
                a, b = lo[seg], lo[(seg + 1) % n]
                c, d_ = hi[seg], hi[(seg + 1) % n]
                tris.append([a, b, d_])
                tris.append([a, d_, c])

        # Top polygon cap: forward-winding fan → outward normal points up
        top = rings[-1]
        for i in range(1, n - 1):
            tris.append([top[0], top[i], top[i + 1]])

        verts_arr = np.array(verts, dtype=np.float32)
        tris_arr = np.array(tris, dtype=np.uint32)
        mesh = m3d.Mesh(vert_properties=verts_arr, tri_verts=tris_arr)
        body = m3d.Manifold(mesh)
        return self._tag(body, node, ctx)

    def _builtin_cylinder(self, args: dict, node: ModularCall, ctx: EvalContext) -> ColoredBody:
        h = float(self._get_arg(args, 0, "h", 1.0))
        r = self._get_arg(args, 1, "r", None)
        r1 = self._get_arg(args, None, "r1", None)
        r2 = self._get_arg(args, None, "r2", None)
        d = self._get_arg(args, None, "d", None)
        d1 = self._get_arg(args, None, "d1", None)
        d2 = self._get_arg(args, None, "d2", None)
        center = bool(self._get_arg(args, None, "center", False))
        segs = self._fn(ctx)

        if d is not None and r is None:
            r = d / 2
        if d1 is not None and r1 is None:
            r1 = d1 / 2
        if d2 is not None and r2 is None:
            r2 = d2 / 2
        if r is not None:
            r1 = r2 = float(r)
        if r1 is None:
            r1 = 1.0
        if r2 is None:
            r2 = r1

        body = m3d.Manifold.cylinder(h, float(r1), float(r2), circular_segments=segs, center=center)
        return self._tag(body, node, ctx)

    # --- transforms ---

    def _builtin_transform(self, name: str, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        if not children:
            return None
        body = self._combine(children)

        if body.section is not None:
            body.section = self._apply_transform_2d(name, args, body.section)
        else:
            body.body = self._apply_transform_3d(name, args, body.body)

        return body

    def _apply_transform_2d(self, name: str, args: dict, cs: "m3d.CrossSection") -> "m3d.CrossSection":
        if name == "translate":
            v = self._get_arg(args, 0, "v", [0, 0])
            cs = cs.translate([float(v[0]), float(v[1])])
        elif name == "rotate":
            a = self._get_arg(args, 0, "a", 0)
            # 2D rotation: scalar angle (Z), or [x,y,z] list → use Z component
            if isinstance(a, list):
                angle = float(a[2]) if len(a) > 2 else 0.0
            else:
                angle = float(a)
            cs = cs.rotate(angle)
        elif name == "scale":
            v = self._get_arg(args, 0, "v", [1, 1])
            if isinstance(v, (int, float)):
                v = [float(v), float(v)]
            cs = cs.scale([float(v[0]), float(v[1])])
        elif name == "mirror":
            v = self._get_arg(args, 0, "v", [1, 0])
            cs = cs.mirror([float(v[0]), float(v[1])])
        return cs

    def _apply_transform_3d(self, name: str, args: dict, body: "m3d.Manifold") -> "m3d.Manifold":
        if name == "translate":
            v = self._get_arg(args, 0, "v", [0, 0, 0])
            v = self._to_vec3(v)
            body = body.translate(v)
        elif name == "rotate":
            a = self._get_arg(args, 0, "a", 0)
            v = self._get_arg(args, 1, "v", None)
            body = self._apply_rotate(body, a, v)
        elif name == "scale":
            v = self._get_arg(args, 0, "v", [1, 1, 1])
            if isinstance(v, (int, float)):
                v = [v, v, v]
            v = [float(x) for x in v]
            body = body.scale(v)
        elif name == "mirror":
            v = self._get_arg(args, 0, "v", [1, 0, 0])
            v = self._to_vec3(v)
            body = body.mirror(v)
        elif name == "resize":
            newsize = self._get_arg(args, 0, "newsize", [0, 0, 0])
            newsize = [float(x) for x in newsize]
            bb = body.bounding_box()  # (xmin,ymin,zmin,xmax,ymax,zmax)
            sx = newsize[0] / (bb[3] - bb[0]) if newsize[0] != 0 and (bb[3]-bb[0]) != 0 else 1
            sy = newsize[1] / (bb[4] - bb[1]) if newsize[1] != 0 and (bb[4]-bb[1]) != 0 else 1
            sz = newsize[2] / (bb[5] - bb[2]) if newsize[2] != 0 and (bb[5]-bb[2]) != 0 else 1
            body = body.scale([sx, sy, sz])
        elif name == "multmatrix":
            m = self._get_arg(args, 0, "m", None)
            if m is not None:
                mat = self._to_matrix4x3(m)
                body = body.transform(mat)
        return body

    def _apply_rotate(self, body: m3d.Manifold, a, v) -> m3d.Manifold:
        if isinstance(a, (list, tuple)):
            # rotate([x,y,z]) — Euler angles in degrees, applied Z then Y then X
            ax, ay, az = float(a[0]), float(a[1]), float(a[2]) if len(a) > 2 else 0.0
            body = body.rotate([ax, ay, az])
            return body
        else:
            # rotate(a, v) — angle around axis
            angle = float(a)
            if v is None:
                v = [0, 0, 1]
            v = self._to_vec3(v)
            # Rodrigues rotation via matrix
            mat = self._axis_angle_matrix(v, math.radians(angle))
            body = body.transform(mat)
            return body

    def _axis_angle_matrix(self, axis, angle_rad: float) -> list:
        ax, ay, az = axis
        length = math.sqrt(ax*ax + ay*ay + az*az)
        if length == 0:
            return [[1,0,0,0],[0,1,0,0],[0,0,1,0]]
        ax, ay, az = ax/length, ay/length, az/length
        c = math.cos(angle_rad)
        s = math.sin(angle_rad)
        t = 1 - c
        return [
            [t*ax*ax+c,    t*ax*ay-s*az, t*ax*az+s*ay, 0],
            [t*ax*ay+s*az, t*ay*ay+c,    t*ay*az-s*ax, 0],
            [t*ax*az-s*ay, t*ay*az+s*ax, t*az*az+c,    0],
        ]

    def _to_vec3(self, v) -> list[float]:
        if isinstance(v, (int, float)):
            return [float(v), 0.0, 0.0]
        result = [float(x) for x in v]
        while len(result) < 3:
            result.append(0.0)
        return result[:3]

    def _to_matrix4x3(self, m) -> list:
        """Convert 4x4 or 4x3 matrix to manifold's 4x3 row-major transform."""
        rows = []
        for row in m[:3]:
            r = [float(x) for x in row]
            while len(r) < 4:
                r.append(0.0)
            rows.append(r[:4])
        return rows

    # --- color ---

    def _builtin_color(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        c = self._get_arg(args, 0, "c", [1, 1, 1, 1])
        alpha = float(self._get_arg(args, 1, "alpha", 1.0))
        if isinstance(c, str):
            rgba = self._css_color(c, alpha)
        elif isinstance(c, (list, tuple)):
            rgba = tuple(float(x) for x in c) + (alpha,) if len(c) == 3 else tuple(float(x) for x in c[:4])
        else:
            rgba = (1.0, 1.0, 1.0, 1.0)

        child_ctx = ctx.child_ctx(color=rgba)
        children = self._eval_children(node.children, child_ctx)
        if not children:
            return None
        result = self._combine(children)
        result.color = rgba
        return result

    def _css_color(self, name: str, alpha: float = 1.0) -> tuple:
        if name.startswith("#"):
            h = name.lstrip("#")
            if len(h) == 6:
                rgb = (int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255)
            elif len(h) == 3:
                rgb = (int(h[0],16)/15, int(h[1],16)/15, int(h[2],16)/15)
            else:
                rgb = (1, 1, 1)
            return rgb + (alpha,)

        color = QColor(name)
        rgb = color.getRgbF()[:3] if color.isValid() else (1, 1, 1)
        return rgb + (alpha,)

    # --- CSG ---

    def _builtin_csg(self, op: str, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        if not children:
            return None
        if len(children) == 1:
            return children[0]

        bodies_3d = [c for c in children if c.body is not None]
        sections_2d = [c for c in children if c.section is not None]

        if bodies_3d:
            result = bodies_3d[0].body
            for c in bodies_3d[1:]:
                if op == "union":
                    result = result + c.body
                elif op == "difference":
                    result = result - c.body
                elif op == "intersection":
                    result = result ^ c.body
            return ColoredBody(body=result, color=bodies_3d[0].color)

        if sections_2d:
            result = sections_2d[0].section
            for c in sections_2d[1:]:
                if op == "union":
                    result = result + c.section
                elif op == "difference":
                    result = result - c.section
                elif op == "intersection":
                    result = result ^ c.section
            return ColoredBody(section=result, color=sections_2d[0].color)

        return None

    def _builtin_hull(self, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        if not children:
            return None
        bodies_3d = [c.body for c in children if c.body is not None]
        if bodies_3d:
            result = m3d.Manifold.batch_hull(bodies_3d)
            return ColoredBody(body=result, color=children[0].color)
        sections = [c.section for c in children if c.section is not None]
        if sections:
            result = m3d.CrossSection.batch_hull(sections)
            return ColoredBody(section=result, color=children[0].color)
        return None

    def _builtin_polyhedron(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        points = self._get_arg(args, 0, "points", None)
        faces = self._get_arg(args, 1, "faces", None)
        if faces is None:
            faces = self._get_arg(args, 1, "triangles", None)  # legacy alias
        if points is None or faces is None:
            self.error("polyhedron: 'points' and 'faces' are required", node)
            return None
        if not isinstance(points, list) or not isinstance(faces, list):
            self.error("polyhedron: 'points' and 'faces' must be lists", node)
            return None
        for i, p in enumerate(points):
            if not isinstance(p, list) or len(p) != 3 or any(c is None for c in p):
                self.error(f"polyhedron: point[{i}] is not a valid [x,y,z] coordinate", node)
                return None
        try:
            verts = np.array([[float(c) for c in p] for p in points], dtype=np.float32)
            # Fan-triangulate faces, reversing winding to convert OpenSCAD's
            # CW-from-outside convention to Manifold's CCW-from-outside convention.
            tris = []
            for face in faces:
                face = list(face)
                for i in range(1, len(face) - 1):
                    tris.append([face[0], face[i + 1], face[i]])
            tri_arr = np.array(tris, dtype=np.uint32)
            mesh = m3d.Mesh(vert_properties=verts, tri_verts=tri_arr)
            body = m3d.Manifold(mesh)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"polyhedron: {e}", node)
            return None

    def _builtin_surface(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        file_arg = self._get_arg(args, 0, "file", None)
        center = bool(self._get_arg(args, None, "center", False))
        invert = bool(self._get_arg(args, None, "invert", False))

        if file_arg is None:
            self.error("surface: 'file' parameter is required", node)
            return None

        # Resolve path relative to the source file
        base_dir = None
        pos = getattr(node, 'position', None)
        if pos and getattr(pos, 'origin', None):
            import os as _os
            base_dir = _os.path.dirname(pos.origin)
        if base_dir:
            import os as _os
            file_path = _os.path.join(base_dir, str(file_arg)) if not _os.path.isabs(str(file_arg)) else str(file_arg)
        else:
            file_path = str(file_arg)

        try:
            heights = self._surface_load(file_path, invert)
        except Exception as e:
            self.error(f"surface: {e}", node)
            return None

        if heights is None or len(heights) == 0 or len(heights[0]) == 0:
            self.error("surface: empty height data", node)
            return None

        rows = len(heights)
        cols = len(heights[0])

        x_off = -(cols - 1) / 2.0 if center else 0.0
        y_off = -(rows - 1) / 2.0 if center else 0.0

        # Build vertex grid: (cols) * (rows) top vertices + same for bottom (z=0)
        # top verts: index = row * cols + col
        # bottom verts: index = rows*cols + row * cols + col
        n = rows * cols
        verts = []
        for r in range(rows):
            for c in range(cols):
                verts.append([c + x_off, r + y_off, float(heights[r][c])])
        for r in range(rows):
            for c in range(cols):
                verts.append([c + x_off, r + y_off, 0.0])

        tris = []

        def top(r, c):
            return r * cols + c

        def bot(r, c):
            return n + r * cols + c

        # Top surface (CCW from above = outward upward normal)
        for r in range(rows - 1):
            for c in range(cols - 1):
                tl, tr, bl, br = top(r+1, c), top(r+1, c+1), top(r, c), top(r, c+1)
                tris.append([tl, bl, br])
                tris.append([tl, br, tr])

        # Bottom face (CCW from below = outward downward normal)
        for r in range(rows - 1):
            for c in range(cols - 1):
                tl, tr, bl, br = bot(r+1, c), bot(r+1, c+1), bot(r, c), bot(r, c+1)
                tris.append([tl, tr, br])
                tris.append([tl, br, bl])

        # Side walls (outward normals: front=-Y, back=+Y, left=-X, right=+X)
        for c in range(cols - 1):  # front (r=0, outward=-Y)
            tris.append([top(0, c), bot(0, c), bot(0, c+1)])
            tris.append([top(0, c), bot(0, c+1), top(0, c+1)])
        for c in range(cols - 1):  # back (r=rows-1, outward=+Y)
            tris.append([top(rows-1, c), top(rows-1, c+1), bot(rows-1, c+1)])
            tris.append([top(rows-1, c), bot(rows-1, c+1), bot(rows-1, c)])
        for r in range(rows - 1):  # left (c=0, outward=-X)
            tris.append([top(r, 0), top(r+1, 0), bot(r+1, 0)])
            tris.append([top(r, 0), bot(r+1, 0), bot(r, 0)])
        for r in range(rows - 1):  # right (c=cols-1, outward=+X)
            tris.append([top(r, cols-1), bot(r+1, cols-1), top(r+1, cols-1)])
            tris.append([top(r, cols-1), bot(r, cols-1), bot(r+1, cols-1)])

        try:
            verts_arr = np.array(verts, dtype=np.float32)
            tris_arr = np.array(tris, dtype=np.uint32)
            mesh = m3d.Mesh(vert_properties=verts_arr, tri_verts=tris_arr)
            body = m3d.Manifold(mesh)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"surface: mesh construction failed: {e}", node)
            return None

    def _surface_load(self, file_path: str, invert: bool):
        """Load height data from a .dat text file or a PNG image."""
        import os as _os
        ext = _os.path.splitext(file_path)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
            return self._surface_load_image(file_path, invert)
        return self._surface_load_dat(file_path)

    def _surface_load_dat(self, file_path: str):
        heights = []
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                heights.append([float(v) for v in line.split()])
        heights.reverse()  # first row in file = highest Y (OpenSCAD convention)
        return heights

    def _surface_load_image(self, file_path: str, invert: bool):
        try:
            from PIL import Image
        except ImportError:
            raise RuntimeError("Pillow is required for image-based surface() — install it with: uv add Pillow")
        img = Image.open(file_path).convert("RGB")
        w, h = img.size
        pixels = img.load()
        heights = []
        for row in range(h - 1, -1, -1):  # bottom row of image = Y=0
            r_vals = []
            for col in range(w):
                r, g, b = pixels[col, row]
                gray = 0.2126 * r + 0.7152 * g + 0.0722 * b  # linear luminance
                val = (255.0 - gray) / 255.0 * 100.0 if invert else gray / 255.0 * 100.0
                r_vals.append(val)
            heights.append(r_vals)
        return heights

    def _builtin_offset(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        r = self._get_arg(args, None, "r", None)
        delta = self._get_arg(args, None, "delta", None)
        chamfer = bool(self._get_arg(args, None, "chamfer", False))
        segs = self._fn(ctx)
        if r is not None:
            result = cs.offset(float(r), m3d.JoinType.Round, circular_segments=segs)
        elif delta is not None:
            jt = m3d.JoinType.Miter if chamfer else m3d.JoinType.Square
            result = cs.offset(float(delta), jt)
        else:
            return children[0] if children else None
        return ColoredBody(section=result, color=ctx.color)

    def _builtin_projection(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        bodies_3d = [c for c in children if c.body is not None]
        if not bodies_3d:
            return None
        combined = self._combine(bodies_3d).body
        cut = bool(self._get_arg(args, None, "cut", False))
        try:
            if cut:
                cs = combined.slice(0.0)
            else:
                raw = combined.project()
                # project() may produce self-intersecting polygons; re-fill to clean up
                polys = raw.to_polygons()
                cs = m3d.CrossSection(polys, m3d.FillRule.Positive) if polys else raw
            return ColoredBody(section=cs, color=bodies_3d[0].color)
        except Exception as e:
            self.error(f"projection: {e}", node)
            return None

    def _builtin_2d(self, name: str, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        segs = self._fn(ctx)
        try:
            if name == "circle":
                r = self._get_arg(args, 0, "r", None)
                d = self._get_arg(args, None, "d", None)
                if d is not None:
                    r = d / 2
                if r is None:
                    r = 1.0
                cs = m3d.CrossSection.circle(float(r), segs)
            elif name == "square":
                size = self._get_arg(args, 0, "size", 1.0)
                center = bool(self._get_arg(args, 1, "center", False))
                if isinstance(size, (int, float)):
                    size = [size, size]
                cs = m3d.CrossSection.square([float(size[0]), float(size[1])], center)
            elif name == "polygon":
                points = self._get_arg(args, 0, "points", None)
                paths = self._get_arg(args, 1, "paths", None)
                if points is None:
                    self.error("polygon: 'points' is required", node)
                    return None
                pts = [[float(p[0]), float(p[1])] for p in points]
                if paths is None:
                    contour = np.array(pts, dtype=np.float64)
                    cs = m3d.CrossSection([contour])
                else:
                    contours = [np.array([pts[int(i)] for i in path], dtype=np.float64) for path in paths]
                    cs = m3d.CrossSection(contours, m3d.FillRule.EvenOdd)
            else:
                return None
            return ColoredBody(section=cs, color=ctx.color)
        except Exception as e:
            self.error(f"{name}: {e}", node)
            return None

    def _builtin_linear_extrude(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        height = float(self._get_arg(args, 0, "height", 1.0))
        center = bool(self._get_arg(args, None, "center", False))
        twist = float(self._get_arg(args, None, "twist", 0.0))
        slices = int(self._get_arg(args, None, "slices", 0))
        scale = self._get_arg(args, None, "scale", None)
        if scale is None:
            scale_top = (1.0, 1.0)
        elif isinstance(scale, (int, float)):
            scale_top = (float(scale), float(scale))
        else:
            scale_top = (float(scale[0]), float(scale[1]))
        try:
            body = m3d.Manifold.extrude(cs, height, slices, twist, scale_top)
            if center:
                body = body.translate([0, 0, -height / 2])
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"linear_extrude: {e}", node)
            return None

    def _builtin_rotate_extrude(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        angle = float(self._get_arg(args, 0, "angle", 360.0))
        segs = self._fn(ctx)
        try:
            body = cs.revolve(segs, angle)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"rotate_extrude: {e}", node)
            return None

    def _builtin_roof(self, args: dict, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        cs = self._to_cross_section(children)
        if cs is None:
            return None
        method = self._get_arg(args, None, "method", "voronoi")
        if method not in ("voronoi", "straight"):
            self._echo_fn(f"WARNING: Unknown roof method '{method}'. Using 'voronoi'.")
            method = "voronoi"
        try:
            polys = cs.to_polygons()
            if not polys:
                return None
            edges = []
            for poly in polys:
                n = len(poly)
                for i in range(n):
                    edges.append((np.asarray(poly[i], dtype=np.float64), np.asarray(poly[(i + 1) % n], dtype=np.float64)))

            minx, miny, maxx, maxy = cs.bounds()
            width, height = maxx - minx, maxy - miny
            z_max = min(width, height) / 2 * 1.02

            edge_length = max(width, height, z_max) / 10
            eps = edge_length / 2

            def sdf(x, y, z):
                p = np.array([x, y])
                d = min(_point_seg_dist(p, a, b) for a, b in edges)
                inside = _point_in_poly_evenodd(p, edges)
                d2 = d if inside else -d
                return d2 - z

            bounds = [minx - eps, miny - eps, 0.0, maxx + eps, maxy + eps, z_max + eps]
            body = m3d.Manifold.level_set(sdf, bounds, edge_length)
            if body.is_empty():
                return None
            body = body.simplify(edge_length * 0.05)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"roof: {e}", node)
            return None

    def _builtin_minkowski(self, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        bodies_3d = [c for c in children if c.body is not None]
        if not bodies_3d:
            return None
        if len(bodies_3d) == 1:
            return bodies_3d[0]
        try:
            result = bodies_3d[0].body
            for c in bodies_3d[1:]:
                result = result.minkowski_sum(c.body)
            return ColoredBody(body=result, color=bodies_3d[0].color)
        except Exception as e:
            self.error(f"minkowski: {e}", node)
            return None

    def _builtin_children(self, args: dict, ctx: EvalContext) -> Optional[ColoredBody]:
        if not ctx.children_bodies:
            return None
        idx = self._get_arg(args, 0, "index", None)
        if idx is not None:
            idx = int(idx)
            if 0 <= idx < len(ctx.children_bodies):
                return ctx.children_bodies[idx]
            return None
        return self._combine(ctx.children_bodies)

    def _builtin_breakpoint(self, args: dict, node, ctx: EvalContext):
        cond = self._get_arg(args, 0, "condition", default=None)
        if cond is not None and not cond:
            return None
        self._check_debug(node, ctx, forced=True)
        return None

    # --- for loops ---

    def _eval_for(self, node: ModularFor, ctx: EvalContext) -> list[ColoredBody]:
        # The parser puts body-level assignments into node.assignments alongside the actual
        # loop variables. Skip any assignment that also appears as a body node — those are
        # per-iteration let-like definitions, not loop variables.
        body_ids = {id(b) for b in node.body}
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            if id(assign) in body_ids:
                continue
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                values = []
            elif isinstance(values, OscRange):
                values = list(values)
            elif isinstance(values, OscObject):
                values = list(values)  # iterate over keys
            elif not isinstance(values, list):
                values = [values]
            var_seqs.append((name, values))

        result = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx()
            for vname, val in combo:
                loop_ctx.dyn[f"__let_{vname}"] = val
            result.extend(self._eval_children(node.body, loop_ctx))
        return result

    def _cartesian(self, var_seqs: list[tuple[str, list]]):
        """Yield [(name, value), ...] tuples for all combinations."""
        if not var_seqs:
            yield []
            return
        name, values = var_seqs[0]
        rest = var_seqs[1:]
        for v in values:
            for tail in self._cartesian(rest):
                yield [(name, v)] + tail

    def _eval_intersection_for(self, node: ModularIntersectionFor, ctx: EvalContext) -> list[ColoredBody]:
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                return []
            if isinstance(values, OscRange):
                values = list(values)
            elif isinstance(values, OscObject):
                values = list(values)  # iterate over keys
            elif not isinstance(values, list):
                values = [values]
            var_seqs.append((name, values))

        body_node = node.body if isinstance(node.body, list) else [node.body]
        iterations = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx()
            for vname, val in combo:
                loop_ctx.dyn[f"__let_{vname}"] = val
            children = self._eval_children(body_node, loop_ctx)
            if children:
                iterations.append(self._combine(children))

        if not iterations:
            return []
        # Intersect all iteration results
        bodies_3d = [c for c in iterations if c.body is not None]
        if bodies_3d:
            result = bodies_3d[0].body
            for c in bodies_3d[1:]:
                result = result ^ c.body  # intersection
            return [ColoredBody(body=result, color=bodies_3d[0].color)]
        # 2D intersection
        sections = [c.section for c in iterations if c.section is not None]
        if sections:
            result = sections[0]
            for s in sections[1:]:
                result = result ^ s
            return [ColoredBody(section=result, color=iterations[0].color)]
        return []

    # --- let ---

    def _eval_let_block(self, node: ModularLet, ctx: EvalContext) -> list[ColoredBody]:
        child_ctx = ctx.child_ctx()
        for assign in node.assignments:
            v = self._eval_expr(assign.expr, ctx)
            child_ctx.dyn[f"__let_{assign.name.name}"] = v
        body = getattr(node, 'children', None) or getattr(node, 'body', None) or []
        return self._eval_children(body, child_ctx)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _combine(self, bodies: list[ColoredBody]) -> ColoredBody:
        bodies_3d = [b for b in bodies if b.body is not None]
        if bodies_3d:
            if len(bodies_3d) == 1:
                return bodies_3d[0]
            composed = m3d.Manifold.compose([b.body for b in bodies_3d])
            return ColoredBody(body=composed, color=bodies_3d[0].color)
        # Pure 2D — union all cross sections
        sections = [b.section for b in bodies if b.section is not None]
        if not sections:
            return ColoredBody(body=m3d.Manifold())
        cs = sections[0]
        for s in sections[1:]:
            cs = cs + s
        return ColoredBody(section=cs, color=bodies[0].color)

    def _to_cross_section(self, children: list[ColoredBody]) -> Optional[m3d.CrossSection]:
        """Union all 2D children into a single CrossSection. Returns None if no 2D children."""
        sections = [c.section for c in children if c.section is not None]
        if not sections:
            return None
        cs = sections[0]
        for s in sections[1:]:
            cs = cs + s
        return cs

    # ------------------------------------------------------------------
    # Expression evaluator
    # ------------------------------------------------------------------

    def _eval_expr(self, node: ASTNode, ctx: EvalContext) -> Any:
        if isinstance(node, CommentedExpr):
            return self._eval_expr(node.expr, ctx)
        if isinstance(node, NumberLiteral):
            return node.val
        if isinstance(node, BooleanLiteral):
            return node.val
        if isinstance(node, StringLiteral):
            return node.val
        if isinstance(node, UndefinedLiteral):
            return None
        if isinstance(node, Identifier):
            return self._eval_identifier(node, ctx)
        if isinstance(node, ListComprehension):
            return self._eval_list_comp(node, ctx)
        if isinstance(node, RangeLiteral):
            return self._eval_range(node, ctx)
        if isinstance(node, AdditionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return _vec_add(a, b)
        if isinstance(node, SubtractionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return _vec_sub(a, b)
        if isinstance(node, MultiplicationOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, list) and isinstance(b, list):
                return _matmul(a, b)
            if isinstance(a, list) and isinstance(b, (int, float)) and not isinstance(b, bool):
                return [_scale(b, x) for x in a]
            if isinstance(b, list) and isinstance(a, (int, float)) and not isinstance(a, bool):
                return [_scale(a, x) for x in b]
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            try:
                return a * b
            except TypeError:
                return None
        if isinstance(node, DivisionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            if isinstance(a, list) and isinstance(b, (int, float)):
                return _div_scale(a, b)
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                return None
            if b == 0:
                if a == 0:
                    return float('nan')
                return math.copysign(float('inf'), a)
            return a / b
        if isinstance(node, ModuloOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            try:
                return a % b
            except (TypeError, ZeroDivisionError):
                return None
        if isinstance(node, ExponentOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            try:
                result = a ** b
                return float('nan') if isinstance(result, complex) else result
            except (TypeError, ZeroDivisionError):
                return None
        if isinstance(node, UnaryMinusOp):
            v = self._eval_expr(node.expr, ctx)
            if isinstance(v, list):
                return [-x for x in v]
            if isinstance(v, bool):
                return None
            try:
                return -v
            except TypeError:
                return None
        if isinstance(node, LogicalAndOp):
            return bool(self._eval_expr(node.left, ctx)) and bool(self._eval_expr(node.right, ctx))
        if isinstance(node, LogicalOrOp):
            return bool(self._eval_expr(node.left, ctx)) or bool(self._eval_expr(node.right, ctx))
        if isinstance(node, LogicalNotOp):
            return not bool(self._eval_expr(node.expr, ctx))
        if isinstance(node, EqualityOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return _osc_equal(a, b)
        if isinstance(node, InequalityOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return not _osc_equal(a, b)
        if isinstance(node, (GreaterThanOp, GreaterThanOrEqualOp, LessThanOp, LessThanOrEqualOp)):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            symbol = {
                GreaterThanOp: ">", GreaterThanOrEqualOp: ">=",
                LessThanOp: "<", LessThanOrEqualOp: "<=",
            }[type(node)]
            if not _osc_comparable(a, b):
                pos = getattr(node, 'position', None)
                self._echo_fn(f"WARNING: undefined operation ({_osc_type_name(a)} {symbol} {_osc_type_name(b)}){self._loc(pos)}")
                return None
            try:
                if isinstance(node, GreaterThanOp):
                    return a > b
                if isinstance(node, GreaterThanOrEqualOp):
                    return a >= b
                if isinstance(node, LessThanOp):
                    return a < b
                return a <= b
            except TypeError:
                return None
        if isinstance(node, TernaryOp):
            self._check_debug(node, ctx, expr_level=True)
            cond = self._eval_expr(node.condition, ctx)
            branch = node.true_expr if cond else node.false_expr
            self._check_debug(branch, ctx, expr_level=True)
            return self._eval_expr(branch, ctx)
        if isinstance(node, PrimaryCall):
            return self._eval_function_call(node, ctx)
        if isinstance(node, PrimaryIndex):
            obj = self._eval_expr(node.left, ctx)
            idx = self._eval_expr(node.index, ctx)
            if isinstance(obj, OscRange) and isinstance(idx, (int, float)):
                return obj[int(idx)]
            if isinstance(obj, (list, str)) and isinstance(idx, (int, float)):
                i = int(idx)
                if i < 0:
                    return None  # OpenSCAD does not support negative indexing
                try:
                    return obj[i]
                except IndexError:
                    return None
            if isinstance(obj, OscObject) and isinstance(idx, str):
                return obj.get(idx)
            return None
        if isinstance(node, PrimaryMember):
            obj = self._eval_expr(node.left, ctx)
            member = node.member.name if hasattr(node.member, 'name') else str(node.member)
            if isinstance(obj, (list, tuple)):
                swizzle = {"x": 0, "y": 1, "z": 2, "w": 3}
                if member in swizzle and swizzle[member] < len(obj):
                    return obj[swizzle[member]]
            if isinstance(obj, OscObject):
                return obj.get(member)
            return None
        if isinstance(node, LetOp):
            child_ctx = ctx.child_ctx()
            for assign in node.assignments:
                # Evaluate in child_ctx (not ctx) so each binding can see
                # earlier bindings in the same let(), e.g. let(a=1, b=a+1).
                v = self._eval_expr(assign.expr, child_ctx)
                child_ctx.dyn[f"__let_{assign.name.name}"] = v
                self._check_debug(assign, child_ctx, expr_level=True)
            return self._eval_expr(node.body, child_ctx)
        if isinstance(node, EchoOp):
            self._do_echo(node.arguments, ctx)
            return self._eval_expr(node.body, ctx)
        if isinstance(node, AssertOp):
            raw = node.arguments
            condition = self._eval_expr(raw[0].expr, ctx) if raw else True
            if not condition:
                cond_text = to_openscad([raw[0].expr]).strip() if raw else "false"
                msg = self._eval_expr(raw[1].expr, ctx) if len(raw) > 1 else None
                err = f"Assertion '{cond_text}' failed" + (f': "{msg}"' if msg is not None else "")
                self.error(err, node, innermost_frame="assert")
            return self._eval_expr(node.body, ctx)
        if isinstance(node, FunctionLiteral):
            return node  # lambda — store for later call
        # Unknown — return None
        return None

    _CONSTANTS = {"PI": math.pi}

    def _eval_identifier(self, node: Identifier, ctx: EvalContext, warn_if_undef: bool = True) -> Any:
        name = node.name
        # Built-in constants
        if name in self._CONSTANTS:
            return self._CONSTANTS[name]
        # Dynamic variable ($fn etc.)
        if name.startswith("$") and name in ctx.dyn:
            return ctx.dyn[name]
        # Let-bound
        let_key = f"__let_{name}"
        if let_key in ctx.dyn:
            return ctx.dyn[let_key]
        # Lexical variable
        decl = ctx.scope.lookup_variable(name)
        if decl is None:
            # Real OpenSCAD: variables and functions live in separate namespaces,
            # so a bare reference to a `function f(x) = ...` declaration is an
            # "unknown variable" -> undef (matches is_function(f) == false there).
            # Warn the same way for any unresolved identifier, matching real
            # OpenSCAD's "Ignoring unknown variable" (no TRACE lines). Suppressed
            # when probing for a function-value (see _eval_function_call), which
            # emits its own "Ignoring unknown function" warning instead.
            if warn_if_undef:
                pos = getattr(node, 'position', None)
                self._echo_fn(f"WARNING: Ignoring unknown variable '{name}'{self._loc(pos)}")
            return None
        if isinstance(decl, ParameterDeclaration):
            # Params are bound via __let_ above; reaching here means no value was provided and no default
            return None
        return self._eval_expr(decl.expr, ctx)

    def _eval_list_comp(self, node: ListComprehension, ctx: EvalContext) -> list:
        result = []
        for elem in node.elements:
            if isinstance(elem, ListCompFor):
                result.extend(self._eval_listcomp_for(elem, ctx))
            elif isinstance(elem, ListCompCFor):
                result.extend(self._eval_listcomp_cfor(elem, ctx))
            elif isinstance(elem, ListCompIf):
                self._check_debug(elem, ctx, expr_level=True)
                if self._eval_expr(elem.condition, ctx):
                    self._expr_depth += 1
                    self._check_debug(elem.true_expr, ctx, expr_level=True)
                    result.extend(self._eval_list_comp_body(elem.true_expr, ctx))
                    self._expr_depth -= 1
            elif isinstance(elem, ListCompIfElse):
                self._check_debug(elem, ctx, expr_level=True)
                branch = elem.true_expr if self._eval_expr(elem.condition, ctx) else elem.false_expr
                self._expr_depth += 1
                self._check_debug(branch, ctx, expr_level=True)
                result.extend(self._eval_list_comp_body(branch, ctx))
                self._expr_depth -= 1
            elif isinstance(elem, ListCompLet):
                let_ctx = ctx.child_ctx()
                for assign in elem.assignments:
                    let_ctx.dyn[f"__let_{assign.name.name}"] = self._eval_expr(assign.expr, ctx)
                    self._check_debug(assign, let_ctx, expr_level=True)
                result.extend(self._eval_list_comp_body(elem.body, let_ctx))
            elif isinstance(elem, ListCompEach):
                self._expr_depth += 1
                self._check_debug(elem, ctx, expr_level=True)
                v = self._eval_expr(elem.body, ctx)
                self._expr_depth -= 1
                if isinstance(v, list):
                    result.extend(v)
                elif v is not None:
                    result.append(v)
            else:
                self._check_debug(elem, ctx, expr_level=True)
                result.append(self._eval_expr(elem, ctx))
        return result

    def _eval_list_comp_body(self, body, ctx: EvalContext) -> list:
        if isinstance(body, ListComprehension):
            # Bracketed body is a single element — wrap so caller's extend adds it as one item.
            self._expr_depth += 1
            result = [self._eval_list_comp(body, ctx)]
            self._expr_depth -= 1
            return result
        if isinstance(body, ListCompFor):
            return self._eval_listcomp_for(body, ctx)
        if isinstance(body, ListCompCFor):
            return self._eval_listcomp_cfor(body, ctx)
        if isinstance(body, ListCompLet):
            let_ctx = ctx.child_ctx()
            for assign in body.assignments:
                let_ctx.dyn[f"__let_{assign.name.name}"] = self._eval_expr(assign.expr, ctx)
                self._check_debug(assign, let_ctx, expr_level=True)
            return self._eval_list_comp_body(body.body, let_ctx)
        if isinstance(body, ListCompIf):
            self._check_debug(body, ctx, expr_level=True)
            if self._eval_expr(body.condition, ctx):
                self._expr_depth += 1
                self._check_debug(body.true_expr, ctx, expr_level=True)
                result = self._eval_list_comp_body(body.true_expr, ctx)
                self._expr_depth -= 1
                return result
            return []
        if isinstance(body, ListCompIfElse):
            self._check_debug(body, ctx, expr_level=True)
            branch = body.true_expr if self._eval_expr(body.condition, ctx) else body.false_expr
            self._expr_depth += 1
            self._check_debug(branch, ctx, expr_level=True)
            result = self._eval_list_comp_body(branch, ctx)
            self._expr_depth -= 1
            return result
        if isinstance(body, ListCompEach):
            self._expr_depth += 1
            self._check_debug(body, ctx, expr_level=True)
            v = self._eval_expr(body.body, ctx)
            self._expr_depth -= 1
            if isinstance(v, list):
                return v
            return [v] if v is not None else []
        self._check_debug(body, ctx, expr_level=True)
        v = self._eval_expr(body, ctx)
        return [v] if v is not None else []

    def _eval_listcomp_for(self, node: ListCompFor, ctx: EvalContext) -> list:
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                values = []
            elif isinstance(values, OscRange):
                values = list(values)
            elif isinstance(values, OscObject):
                values = list(values)  # iterate over keys
            elif not isinstance(values, list):
                values = [values]
            var_seqs.append((name, values))

        result = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx()
            for vname, val in combo:
                loop_ctx.dyn[f"__let_{vname}"] = val
            self._expr_depth += 1
            self._check_debug(node, loop_ctx, expr_level=True)
            if isinstance(node.body, ListComprehension):
                # Bracketed inner comprehension — yields one list element per iteration.
                result.append(self._eval_list_comp(node.body, loop_ctx))
            else:
                result.extend(self._eval_list_comp_body(node.body, loop_ctx))
            self._expr_depth -= 1
        return result

    # OpenSCAD has no native loop-iteration cap; this just guards against a
    # runaway condition/increment (e.g. `i = i+1` typo'd as `i = i-1`) hanging
    # the evaluator forever.
    _MAX_CFOR_ITERATIONS = 1_000_000

    def _eval_listcomp_cfor(self, node: ListCompCFor, ctx: EvalContext) -> list:
        loop_ctx = ctx.child_ctx()
        for assign in node.inits:
            loop_ctx.dyn[f"__let_{assign.name.name}"] = self._eval_expr(assign.expr, loop_ctx)

        result = []
        iterations = 0
        while self._eval_expr(node.condition, loop_ctx):
            iterations += 1
            if iterations > self._MAX_CFOR_ITERATIONS:
                self.error("C-style for loop exceeded maximum iteration count", node)
            self._expr_depth += 1
            self._check_debug(node, loop_ctx, expr_level=True)
            if isinstance(node.body, ListComprehension):
                result.append(self._eval_list_comp(node.body, loop_ctx))
            else:
                result.extend(self._eval_list_comp_body(node.body, loop_ctx))
            self._expr_depth -= 1
            for assign in node.incrs:
                loop_ctx.dyn[f"__let_{assign.name.name}"] = self._eval_expr(assign.expr, loop_ctx)
        return result

    def _eval_range(self, node: RangeLiteral, ctx: EvalContext) -> OscRange:
        start = self._eval_expr(node.start, ctx)

        # Detect 2-part [start:end] vs 3-part [start:increment:end]:
        # The parser stores 3-part [A:B:C] as start=A, end=B, step=C
        # where B is the OpenSCAD increment and C is the OpenSCAD end.
        # For 2-part, step.position == range.position (synthetic default=1).
        is_3part = (node.step.position != node.position)
        if is_3part:
            increment = self._eval_expr(node.end, ctx)   # middle value = increment
            stop = self._eval_expr(node.step, ctx)        # last value = end
        else:
            stop = self._eval_expr(node.end, ctx)
            increment = self._eval_expr(node.step, ctx)   # default 1.0

        start = float(start) if start is not None else 0.0
        stop = float(stop) if stop is not None else 0.0
        increment = float(increment) if increment is not None else 1.0
        return OscRange(start, increment, stop)

    def _eval_function_call(self, node: PrimaryCall, ctx: EvalContext) -> Any:
        name = node.left.name if isinstance(node.left, Identifier) else None
        args = self._resolve_args(node.arguments, ctx)

        if name == "object":
            return self._builtin_object(args, node)

        # Built-in math functions
        math_fns = {
            "abs": abs, "sign": lambda x: (1 if x > 0 else -1 if x < 0 else 0),
            # math.ceil()/math.floor() raise on nan/inf (Python wants a finite
            # result to convert to int); OpenSCAD passes nan/inf through unchanged.
            "ceil": lambda x: x if (math.isnan(x) or math.isinf(x)) else math.ceil(x),
            "floor": lambda x: x if (math.isnan(x) or math.isinf(x)) else math.floor(x),
            # OpenSCAD rounds half away from zero (round(2.5)==3, round(-2.5)==-3),
            # unlike Python's round-half-to-even (round(2.5)==2).
            "round": lambda x: x if (math.isnan(x) or math.isinf(x))
                else (math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)),
            "sqrt": lambda x: float('nan') if x < 0 else math.sqrt(x),
            "ln": lambda x: float('-inf') if x == 0 else (float('nan') if x < 0 else math.log(x)),
            "log": lambda x: float('-inf') if x == 0 else (float('nan') if x < 0 else math.log10(x)),
            "exp": math.exp,
            "sin": self._builtin_sin,
            "cos": self._builtin_cos,
            "tan": self._builtin_tan,
            "asin": lambda x: float('nan') if abs(x) > 1 else math.degrees(math.asin(x)),
            "acos": lambda x: float('nan') if abs(x) > 1 else math.degrees(math.acos(x)),
            "atan": lambda x: math.degrees(math.atan(x)),
            "atan2": lambda y, x: math.degrees(math.atan2(y, x)),
            "max": self._builtin_max, "min": self._builtin_min,
            "pow": self._builtin_pow,
            "norm": lambda v: math.sqrt(sum(x*x for x in v)),
            "cross": self._builtin_cross,
            "rands": self._builtin_rands,
            "concat": lambda *args: sum((list(a) if isinstance(a, list) else [a] for a in args), []),
            "len": lambda x: len(x) if isinstance(x, (list, str, OscObject)) else None,
            "str": lambda *a: "".join(x if isinstance(x, str) else self._fmt_val(x) for x in a),
            # chr() accepts either a single code point or a vector of them.
            "chr": lambda x: "".join(chr(int(c)) for c in x) if isinstance(x, list) else chr(int(x)),
            # ord() of a multi-character string returns the code of its first character.
            "ord": lambda s: ord(s[0]) if isinstance(s, str) and len(s) >= 1 else None,
            "is_undef": lambda x: x is None,
            # nan fails is_num() in real OpenSCAD (inf/-inf pass).
            "is_num": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and not math.isnan(x),
            "is_bool": lambda x: isinstance(x, bool),
            "is_string": lambda x: isinstance(x, str),
            "is_list": lambda x: isinstance(x, list),
            "is_function": lambda x: isinstance(x, (FunctionDeclaration, FunctionLiteral)),
            "is_object": lambda x: isinstance(x, OscObject),
            "search": self._builtin_search,
            "lookup": self._builtin_lookup,
            "version": lambda: [2025, 1, 1],
            "version_num": lambda: 20250101,
            "parent_module": lambda idx=0: None,
        }
        if name and name in math_fns:
            positional = [args[k] for k in sorted(k for k in args if isinstance(k, int))]
            if not positional:
                # OpenSCAD: named args fall back to positional order for built-ins
                positional = [args[k] for k in args if isinstance(k, str)]
            try:
                return math_fns[name](*positional)
            except Exception:
                return None

        # User-defined function
        if name:
            decl = ctx.scope.lookup_function(name)
            if decl is not None:
                return self._eval_user_function(name, decl, node.arguments, ctx, node)

        # Function value, e.g. `g = function(x) x*2; g(3)`. For a plain
        # identifier, look up the variable directly (suppressing the
        # "unknown variable" warning) — an unresolved name here means this
        # is an unknown *function*, warned about below instead.
        if isinstance(node.left, Identifier):
            func_node = self._eval_identifier(node.left, ctx, warn_if_undef=False)
        else:
            func_node = self._eval_expr(node.left, ctx)
        if isinstance(func_node, FunctionLiteral):
            return self._eval_function_literal(func_node, node.arguments, ctx, node, name=name)

        if name and func_node is None:
            # Unknown function — warn and evaluate to undef, matching real
            # OpenSCAD's "Ignoring unknown function" behavior (no TRACE,
            # execution continues), rather than aborting the whole render.
            pos = getattr(node, 'position', None)
            self._echo_fn(f"WARNING: Ignoring unknown function '{name}'{self._loc(pos)}")

        return None

    def _builtin_minmax(self, op, args):
        """Shared logic for OpenSCAD's `min`/`max`.

        A single vector argument returns `op` of its elements; multiple
        arguments must all be scalars (mixing in a vector is `undef`, like
        real OpenSCAD); a single scalar argument returns itself.
        """
        if len(args) == 1:
            v = args[0]
            return op(v) if isinstance(v, list) else v
        if any(isinstance(a, list) for a in args):
            return None
        return op(args)

    def _builtin_max(self, *args):
        return self._builtin_minmax(max, args)

    def _builtin_min(self, *args):
        return self._builtin_minmax(min, args)

    def _builtin_pow(self, a, b):
        if a < 0 and not float(b).is_integer():
            return float('nan')
        if a == 0 and b < 0:
            # 0 ** negative is +inf in OpenSCAD; Python's pow()/math.pow() raise.
            return float('inf')
        return pow(a, b)

    # At exact multiples of 90 degrees, sin/cos/tan use exact table values
    # instead of math.sin/cos/tan(radians(x)), which accumulate floating-point
    # noise (e.g. cos(90) -> 6.12e-17, tan(90) -> 1.63e+16) — matching real
    # OpenSCAD's degree-based trig, which special-cases these angles.
    _SIN_90 = (0.0, 1.0, 0.0, -1.0)
    _COS_90 = (1.0, 0.0, -1.0, 0.0)
    _TAN_90 = (0.0, math.inf, 0.0, -math.inf)

    def _deg_trig(self, x, table, fallback):
        if math.isnan(x) or math.isinf(x):
            return float('nan')
        n = x / 90.0
        rn = round(n)
        if rn == n:
            return table[int(rn) % 4]
        return fallback(math.radians(x))

    def _builtin_sin(self, x):
        return self._deg_trig(x, self._SIN_90, math.sin)

    def _builtin_cos(self, x):
        return self._deg_trig(x, self._COS_90, math.cos)

    def _builtin_tan(self, x):
        return self._deg_trig(x, self._TAN_90, math.tan)

    def _builtin_cross(self, a, b):
        if len(a) == 2 and len(b) == 2:
            return a[0]*b[1] - a[1]*b[0]
        return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]

    def _builtin_rands(self, minval, maxval, n, seed=None):
        if seed is not None:
            random.seed(int(seed))
        return [random.uniform(float(minval), float(maxval)) for _ in range(int(n))]

    def _builtin_search(self, match, vector, num_returns=1, index_col=0):
        """OpenSCAD search(): find positions of match value(s) in vector.

        Strings are treated as character arrays — each character is searched
        independently, mirroring OpenSCAD semantics.
        """
        num_returns = int(num_returns)
        col = int(index_col)

        def _find_all(val):
            results = []
            for i, item in enumerate(vector):
                # A vector match value (e.g. searching for a coordinate like
                # [0,0,1]) is compared directly against each whole element,
                # not column-indexed — index_col only applies to scalar matches.
                if isinstance(val, list):
                    target = item
                else:
                    target = item[col] if isinstance(item, list) else item
                if target == val:
                    results.append(i)
            return results

        def _result_for(val):
            """Result for one element in a list/string match context."""
            matches = _find_all(val)
            if num_returns == 1:
                return matches[0] if matches else []
            elif num_returns == 0:
                return matches
            else:
                return matches[:num_returns]

        if isinstance(match, str):
            # String → character array: search for each char independently.
            # With num_returns=1: not-found chars are dropped (not included as []).
            # With num_returns=0: all chars included, not-found → [].
            results = []
            for c in match:
                r = _result_for(c)
                if num_returns != 1 or r != []:
                    results.append(r)
            return results
        elif isinstance(match, list):
            return [_result_for(m) for m in match]
        else:
            # Scalar number: always return a list of matching indices
            matches = _find_all(match)
            if num_returns == 1:
                return matches[:1]      # [idx] or []
            elif num_returns == 0:
                return matches
            else:
                return matches[:num_returns]

    def _builtin_lookup(self, key, table):
        """Linear interpolation lookup in a [[key, value], ...] table."""
        if not table:
            return None
        pairs = sorted(table, key=lambda p: p[0])
        if key <= pairs[0][0]:
            return pairs[0][1]
        if key >= pairs[-1][0]:
            return pairs[-1][1]
        for i in range(len(pairs) - 1):
            k0, v0 = pairs[i]
            k1, v1 = pairs[i + 1]
            if k0 <= key <= k1:
                t = (key - k0) / (k1 - k0)
                return v0 + t * (v1 - v0)
        return 0

    def _builtin_object(self, args: dict, node) -> Optional[OscObject]:
        """`object(a=1, b=2, ...)` — an ordered string-keyed map.

        Positional arguments merge an existing `OscObject`'s entries, or a
        list of `[key, value]` pairs, into the result (in their own order);
        named arguments set/override entries in call order. Any other
        positional argument type is invalid and the whole call is `undef`.
        """
        result: dict = {}
        for key, val in args.items():
            if isinstance(key, str):
                result[key] = val
                continue
            if isinstance(val, OscObject):
                for k, v in val.items():
                    result[k] = v
            elif isinstance(val, list):
                for entry in val:
                    if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str):
                        result[entry[0]] = entry[1]
                    else:
                        self._echo_fn(
                            f"WARNING: object(Argument {key}) malformed [key,value] entry in "
                            f"unnamed list argument{self._loc(getattr(node, 'position', None))}"
                        )
                        return None
            else:
                tname = _object_arg_type_name(val)
                self._echo_fn(
                    f"WARNING: object(Argument {key} <{tname}>) An unnamed argument must be "
                    f"either <object> or <list>, it is <{tname}>. "
                    f"{self._loc(getattr(node, 'position', None))}"
                )
                return None
        return OscObject(result)

    def _apply_defaults(self, params, child_ctx: EvalContext, caller_ctx: EvalContext):
        """Bind default values for any params not already set in child_ctx.dyn.

        Every declared parameter gets a `__let_*` entry — `undef` (`None`) if
        it has no default and the caller didn't supply one — so
        `_eval_identifier`'s eager `ctx.dyn` check always wins for parameter
        names. Without this, a body statement that shadows a parameter name
        (e.g. BOSL2's `chamfer = approx(chamfer,0) ? undef : chamfer;`) would
        hit the hoisted Assignment via `scope.lookup_variable` when
        evaluating its own right-hand side, recursing forever.
        """
        for param in params:
            pname = param.name.name if hasattr(param, 'name') else None
            if pname and f"__let_{pname}" not in child_ctx.dyn:
                default = getattr(param, 'default', None)
                if default is not None:
                    child_ctx.dyn[f"__let_{pname}"] = self._eval_expr(default, caller_ctx)
                else:
                    child_ctx.dyn[f"__let_{pname}"] = None

    def _eval_user_function(self, name: str, decl: FunctionDeclaration, arguments, ctx: EvalContext, call_node=None) -> Any:
        params = decl.parameters if hasattr(decl, 'parameters') else []
        bound = self._bind_args(params, arguments, ctx)
        fn_scope = decl.scope if hasattr(decl, 'scope') and decl.scope else ctx.scope
        child_ctx = self._call_ctx_for(decl, ctx, scope=fn_scope)
        for k, v in bound.items():
            child_ctx.dyn[f"__let_{k}"] = v
        self._apply_defaults(params, child_ctx, ctx)
        pos = getattr(call_node, 'position', None)
        self._call_stack.append(("function", name, pos, getattr(decl, 'position', None)))
        self._frame_ctxs.append(child_ctx)
        try:
            self._check_debug(decl.expr, child_ctx)
            return self._eval_expr(decl.expr, child_ctx)
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()

    def _eval_function_literal(self, func_node: FunctionLiteral, arguments, ctx: EvalContext, call_node=None, name: str | None = None) -> Any:
        """Call a `function (...) expr` value, e.g. `g = function(x) x*2; g(3)`.

        Closes over `func_node.scope` (where the literal was written), like
        `_eval_user_function()` does for named functions via `decl.scope`.
        """
        params = func_node.parameters
        bound = self._bind_args(params, arguments, ctx)
        fn_scope = func_node.scope if func_node.scope else ctx.scope
        child_ctx = self._call_ctx_for(func_node, ctx, scope=fn_scope)
        for k, v in bound.items():
            child_ctx.dyn[f"__let_{k}"] = v
        self._apply_defaults(params, child_ctx, ctx)
        pos = getattr(call_node, 'position', None)
        self._call_stack.append(("function", name or "<function>", pos, func_node.position))
        self._frame_ctxs.append(child_ctx)
        try:
            self._check_debug(func_node.body, child_ctx)
            return self._eval_expr(func_node.body, child_ctx)
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()
