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

from openscad_parser.ast import to_openscad
from openscad_parser.ast.nodes import (
    ASTNode, Assignment, Identifier,
    NumberLiteral, BooleanLiteral, StringLiteral, UndefinedLiteral,
    ListComprehension, ListCompFor, ListCompIf, ListCompIfElse, ListCompLet, ListCompEach,
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
        items = list(self)
        return items[idx] if 0 <= idx < len(items) else None

    def __repr__(self):
        return f"OscRange({self.start}, {self.step}, {self.end})"


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

        cmd, mods = self._debug_hook(int(line), self._last_locals, list(self._call_stack), all_frame_locals, forced=forced, expr_level=expr_level)
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
            return f"[{v.start:g} : {v.step:g} : {v.end:g}]"
        if isinstance(v, float):
            return f"{v:g}"
        if isinstance(v, list):
            return "[" + ", ".join(self._fmt_val(x) for x in v) + "]"
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
                if key in ctx.dyn:
                    first_pos = ctx.dyn_positions.get(key)
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
                return self._eval_children(node.true_branch, ctx)
            return []
        if isinstance(node, ModularIfElse):
            cond = self._eval_expr(node.condition, ctx)
            if cond:
                return self._eval_children(node.true_branch, ctx)
            return self._eval_children(node.false_branch, ctx)
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

    def _eval_user_module(self, decl: ModuleDeclaration, call: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        # Bind parameters
        child_scope = decl.scope if hasattr(decl, 'scope') and decl.scope else ctx.scope
        params = decl.parameters if hasattr(decl, 'parameters') else []
        args = self._bind_args(params, call.arguments, ctx)

        # Evaluate children in caller's ctx so they become available via children()
        caller_bodies = self._eval_children(call.children, ctx)

        child_ctx = ctx.call_ctx(
            scope=child_scope,
            children_bodies=caller_bodies,
        )
        child_ctx.dyn["$children"] = len(caller_bodies)
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
        if name == "render":
            # render() is a display hint; just pass through children
            children = self._eval_children(node.children, ctx)
            return self._combine(children) if children else None
        if name in ("text", "surface", "import"):
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
        segs = self._fn(ctx)
        body = m3d.Manifold.sphere(float(r), circular_segments=segs)
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
        table = {
            "red": (1,0,0), "green": (0,0.502,0), "blue": (0,0,1),
            "white": (1,1,1), "black": (0,0,0), "yellow": (1,1,0),
            "cyan": (0,1,1), "magenta": (1,0,1), "orange": (1,0.647,0),
            "purple": (0.502,0,0.502), "gray": (0.502,0.502,0.502),
            "grey": (0.502,0.502,0.502),
        }
        rgb = table.get(name.lower(), (1, 1, 1))
        if name.startswith("#"):
            h = name.lstrip("#")
            if len(h) == 6:
                rgb = (int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255)
            elif len(h) == 3:
                rgb = (int(h[0],16)/15, int(h[1],16)/15, int(h[2],16)/15)
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
        if points is None or faces is None:
            self.error("polyhedron: 'points' and 'faces' are required", node)
            return None
        try:
            verts = np.array([[float(c) for c in p] for p in points], dtype=np.float32)
            # Triangulate faces (fan triangulation for convex polygons)
            tris = []
            for face in faces:
                face = list(face)
                for i in range(1, len(face) - 1):
                    tris.append([face[0], face[i], face[i + 1]])
            tri_arr = np.array(tris, dtype=np.uint32)
            mesh = m3d.Mesh(vert_properties=verts, tri_verts=tri_arr)
            body = m3d.Manifold(mesh)
            return self._tag(body, node, ctx)
        except Exception as e:
            self.error(f"polyhedron: {e}", node)
            return None

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
            if isinstance(a, list) and isinstance(b, list):
                return [x + y for x, y in zip(a, b)]
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            try:
                return a + b
            except TypeError:
                return None
        if isinstance(node, SubtractionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, list) and isinstance(b, list):
                return [x - y for x, y in zip(a, b)]
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            try:
                return a - b
            except TypeError:
                return None
        if isinstance(node, MultiplicationOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, list) and isinstance(b, (int, float)) and not isinstance(b, bool):
                return [x * b for x in a]
            if isinstance(b, list) and isinstance(a, (int, float)) and not isinstance(a, bool):
                return [a * x for x in b]
            if isinstance(a, bool) or isinstance(b, bool):
                return None
            try:
                return a * b
            except TypeError:
                return None
        if isinstance(node, DivisionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                return None
            if isinstance(a, bool) or isinstance(b, bool):
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
            return a == b
        if isinstance(node, InequalityOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return a != b
        if isinstance(node, GreaterThanOp):
            try:
                return self._eval_expr(node.left, ctx) > self._eval_expr(node.right, ctx)
            except TypeError:
                return None
        if isinstance(node, GreaterThanOrEqualOp):
            try:
                return self._eval_expr(node.left, ctx) >= self._eval_expr(node.right, ctx)
            except TypeError:
                return None
        if isinstance(node, LessThanOp):
            try:
                return self._eval_expr(node.left, ctx) < self._eval_expr(node.right, ctx)
            except TypeError:
                return None
        if isinstance(node, LessThanOrEqualOp):
            try:
                return self._eval_expr(node.left, ctx) <= self._eval_expr(node.right, ctx)
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
            return None
        if isinstance(node, PrimaryMember):
            obj = self._eval_expr(node.left, ctx)
            member = node.member.name if hasattr(node.member, 'name') else str(node.member)
            if isinstance(obj, (list, tuple)):
                swizzle = {"x": 0, "y": 1, "z": 2, "w": 3}
                if member in swizzle and swizzle[member] < len(obj):
                    return obj[swizzle[member]]
            return None
        if isinstance(node, LetOp):
            child_ctx = ctx.child_ctx()
            for assign in node.assignments:
                v = self._eval_expr(assign.expr, ctx)
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

    def _eval_identifier(self, node: Identifier, ctx: EvalContext) -> Any:
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
            # Fall back to function namespace — allows is_function(f) and passing functions as values
            fn_decl = ctx.scope.lookup_function(name)
            if fn_decl is not None:
                return fn_decl
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
            elif isinstance(elem, ListCompIf):
                if self._eval_expr(elem.condition, ctx):
                    result.extend(self._eval_list_comp_body(elem.true_expr, ctx))
            elif isinstance(elem, ListCompIfElse):
                branch = elem.true_expr if self._eval_expr(elem.condition, ctx) else elem.false_expr
                result.extend(self._eval_list_comp_body(branch, ctx))
            elif isinstance(elem, ListCompLet):
                let_ctx = ctx.child_ctx()
                for assign in elem.assignments:
                    let_ctx.dyn[f"__let_{assign.name.name}"] = self._eval_expr(assign.expr, ctx)
                    self._check_debug(assign, let_ctx, expr_level=True)
                result.extend(self._eval_list_comp_body(elem.body, let_ctx))
            elif isinstance(elem, ListCompEach):
                v = self._eval_expr(elem.body, ctx)
                if isinstance(v, list):
                    result.extend(v)
                elif v is not None:
                    result.append(v)
            else:
                result.append(self._eval_expr(elem, ctx))
        return result

    def _eval_list_comp_body(self, body, ctx: EvalContext) -> list:
        if isinstance(body, ListComprehension):
            # Bracketed body is a single element — wrap so caller's extend adds it as one item.
            return [self._eval_list_comp(body, ctx)]
        if isinstance(body, ListCompFor):
            return self._eval_listcomp_for(body, ctx)
        if isinstance(body, ListCompLet):
            let_ctx = ctx.child_ctx()
            for assign in body.assignments:
                let_ctx.dyn[f"__let_{assign.name.name}"] = self._eval_expr(assign.expr, ctx)
                self._check_debug(assign, let_ctx, expr_level=True)
            return self._eval_list_comp_body(body.body, let_ctx)
        if isinstance(body, ListCompIf):
            if self._eval_expr(body.condition, ctx):
                return self._eval_list_comp_body(body.true_expr, ctx)
            return []
        if isinstance(body, ListCompIfElse):
            branch = body.true_expr if self._eval_expr(body.condition, ctx) else body.false_expr
            return self._eval_list_comp_body(branch, ctx)
        if isinstance(body, ListCompEach):
            v = self._eval_expr(body.body, ctx)
            if isinstance(v, list):
                return v
            return [v] if v is not None else []
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
            elif not isinstance(values, list):
                values = [values]
            var_seqs.append((name, values))

        result = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx()
            for vname, val in combo:
                loop_ctx.dyn[f"__let_{vname}"] = val
            self._check_debug(node, loop_ctx, expr_level=True)
            if isinstance(node.body, ListComprehension):
                # Bracketed inner comprehension — yields one list element per iteration.
                result.append(self._eval_list_comp(node.body, loop_ctx))
            else:
                result.extend(self._eval_list_comp_body(node.body, loop_ctx))
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
        func_node = self._eval_expr(node.left, ctx)
        name = node.left.name if isinstance(node.left, Identifier) else None
        args = self._resolve_args(node.arguments, ctx)

        # Built-in math functions
        math_fns = {
            "abs": abs, "sign": lambda x: (1 if x > 0 else -1 if x < 0 else 0),
            "ceil": math.ceil, "floor": math.floor,
            "round": round,
            "sqrt": lambda x: float('nan') if x < 0 else math.sqrt(x),
            "ln": lambda x: float('-inf') if x == 0 else (float('nan') if x < 0 else math.log(x)),
            "log": lambda x: float('-inf') if x == 0 else (float('nan') if x < 0 else math.log10(x)),
            "exp": math.exp,
            "sin": lambda x: math.sin(math.radians(x)),
            "cos": lambda x: math.cos(math.radians(x)),
            "tan": lambda x: math.tan(math.radians(x)),
            "asin": lambda x: float('nan') if abs(x) > 1 else math.degrees(math.asin(x)),
            "acos": lambda x: float('nan') if abs(x) > 1 else math.degrees(math.acos(x)),
            "atan": lambda x: math.degrees(math.atan(x)),
            "atan2": lambda y, x: math.degrees(math.atan2(y, x)),
            "max": max, "min": min,
            "pow": lambda a, b: float('nan') if a < 0 and not float(b).is_integer() else pow(a, b),
            "norm": lambda v: math.sqrt(sum(x*x for x in v)),
            "cross": lambda a, b: [
                a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]
            ],
            "rands": self._builtin_rands,
            "concat": lambda *args: sum((list(a) if isinstance(a, list) else [a] for a in args), []),
            "len": lambda x: len(x) if isinstance(x, (list, str)) else None,
            "str": lambda *a: "".join(x if isinstance(x, str) else self._fmt_val(x) for x in a),
            "chr": lambda x: chr(int(x)),
            "ord": lambda s: ord(s) if isinstance(s, str) and len(s) == 1 else None,
            "is_undef": lambda x: x is None,
            "is_num": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "is_bool": lambda x: isinstance(x, bool),
            "is_string": lambda x: isinstance(x, str),
            "is_list": lambda x: isinstance(x, list),
            "is_function": lambda x: isinstance(x, (FunctionDeclaration, FunctionLiteral)),
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
            if func_node is None:
                self.error(f"undefined function '{name}'", node)

        return None

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
            return 0
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

    def _apply_defaults(self, params, child_ctx: EvalContext, caller_ctx: EvalContext):
        """Bind default values for any params not already set in child_ctx.dyn."""
        for param in params:
            pname = param.name.name if hasattr(param, 'name') else None
            if pname and f"__let_{pname}" not in child_ctx.dyn:
                default = getattr(param, 'default', None)
                if default is not None:
                    child_ctx.dyn[f"__let_{pname}"] = self._eval_expr(default, caller_ctx)

    def _eval_user_function(self, name: str, decl: FunctionDeclaration, arguments, ctx: EvalContext, call_node=None) -> Any:
        params = decl.parameters if hasattr(decl, 'parameters') else []
        bound = self._bind_args(params, arguments, ctx)
        child_ctx = ctx.call_ctx(scope=decl.scope if hasattr(decl, 'scope') and decl.scope else ctx.scope)
        for k, v in bound.items():
            child_ctx.dyn[f"__let_{k}"] = v
        self._apply_defaults(params, child_ctx, ctx)
        pos = getattr(call_node, 'position', None)
        self._call_stack.append(("function", name, pos))
        self._frame_ctxs.append(child_ctx)
        try:
            self._check_debug(decl.expr, child_ctx)
            return self._eval_expr(decl.expr, child_ctx)
        finally:
            self._call_stack.pop()
            self._frame_ctxs.pop()
