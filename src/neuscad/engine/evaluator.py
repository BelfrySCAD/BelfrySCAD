"""
AST evaluator: walks the openscad_parser AST and produces Manifold geometry.
Returns (manifold_body, id_to_node, colored_meshes) or raises EvalError.
"""
from __future__ import annotations
import math
from typing import Any, Optional
from dataclasses import dataclass, field

import manifold3d as m3d
import numpy as np

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
    ModularEcho, ModularAssert,
    ModularModifierShowOnly, ModularModifierHighlight,
    ModularModifierBackground, ModularModifierDisable,
    ModuleDeclaration, FunctionDeclaration, ParameterDeclaration,
    VectorElement,
    LetOp, EchoOp, AssertOp,
    FunctionLiteral,
)


class EvalError(Exception):
    pass


@dataclass
class ColoredBody:
    """A Manifold body paired with an optional RGBA color."""
    body: m3d.Manifold
    color: Optional[tuple[float, float, float, float]] = None  # RGBA 0-1


@dataclass
class EvalContext:
    """Mutable evaluation state threaded through recursive calls."""
    # Lexical scope (from build_scopes)
    scope: Any
    # Dynamic variables ($fn, $fa, $fs, $t, etc.) — call-chain inherited
    dyn: dict[str, Any] = field(default_factory=lambda: {"$fn": 0, "$fa": 12.0, "$fs": 2.0})
    # Optional color propagated from parent color() call
    color: Optional[tuple[float, float, float, float]] = None
    # Children passed into a module call (for children() built-in)
    children_bodies: list[ColoredBody] = field(default_factory=list)

    def child_ctx(self, scope=None, dyn=None, color=None, children_bodies=None):
        return EvalContext(
            scope=scope if scope is not None else self.scope,
            dyn=dyn if dyn is not None else dict(self.dyn),
            color=color if color is not None else self.color,
            children_bodies=children_bodies if children_bodies is not None else [],
        )


class Evaluator:
    def __init__(self, echo_fn=None):
        self.id_to_node: dict[int, ASTNode] = {}
        self._errors: list[str] = []
        self._echo_fn = echo_fn or (lambda msg: print(msg))
        self._call_stack: list[str] = []  # function name frames

    def error(self, msg: str):
        if self._call_stack:
            lines = []
            for fname, pos in reversed(self._call_stack):
                if pos is not None:
                    lines.append(f"  in {fname}() at {pos.origin}:{pos.line}")
                else:
                    lines.append(f"  in {fname}()")
            full = f"{msg}\nTraceback:\n" + "\n".join(lines)
        else:
            full = msg
        self._errors.append(full)
        raise EvalError(full)

    def _fmt_val(self, v) -> str:
        if v is None:
            return "undef"
        if isinstance(v, bool):
            return "true" if v else "false"
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

    def evaluate(self, nodes: list[ASTNode], root_scope) -> tuple[list[ColoredBody], dict[int, ASTNode]]:
        """Walk top-level AST nodes and return (geometry, id_to_node mapping)."""
        self._call_stack.clear()
        ctx = EvalContext(scope=root_scope)
        result = []
        for node in nodes:
            bodies = self._eval_statement(node, ctx)
            result.extend(bodies)
        return result, self.id_to_node

    # ------------------------------------------------------------------
    # Statement dispatch
    # ------------------------------------------------------------------

    def _eval_statement(self, node: ASTNode, ctx: EvalContext) -> list[ColoredBody]:
        if isinstance(node, Assignment):
            # $-prefixed assignments update the dynamic context so child calls inherit them
            if node.name.name.startswith("$"):
                ctx.dyn[node.name.name] = self._eval_expr(node.expr, ctx)
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
        if isinstance(node, ModularLet):
            return self._eval_let_block(node, ctx)
        if isinstance(node, ModularEcho):
            self._do_echo(node.arguments, ctx)
            return []
        if isinstance(node, ModularAssert):
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
        for child in children:
            result.extend(self._eval_statement(child, ctx))
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

        child_ctx = ctx.child_ctx(
            scope=child_scope,
            children_bodies=caller_bodies,
        )
        # Bind all args; $-prefixed go into dyn directly, others as __let_
        for k, v in args.items():
            if k.startswith("$"):
                child_ctx.dyn[k] = v
            else:
                child_ctx.dyn[f"__let_{k}"] = v
        # Apply defaults for missing params
        self._apply_defaults(params, child_ctx, ctx)

        module_body = getattr(decl, 'children', None) or getattr(decl, 'body', None) or []
        bodies = self._eval_children(module_body, child_ctx)
        if not bodies:
            return None
        return self._combine(bodies)

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
        if name == "echo":
            self._do_echo(node.arguments, ctx)
            return None
        if name == "assert":
            return None
        if name == "children":
            return self._builtin_children(args, ctx)
        # Unknown — skip silently
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

        if name == "translate":
            v = self._get_arg(args, 0, "v", [0, 0, 0])
            v = self._to_vec3(v)
            body.body = body.body.translate(v)
        elif name == "rotate":
            a = self._get_arg(args, 0, "a", 0)
            v = self._get_arg(args, 1, "v", None)
            body.body = self._apply_rotate(body.body, a, v)
        elif name == "scale":
            v = self._get_arg(args, 0, "v", [1, 1, 1])
            if isinstance(v, (int, float)):
                v = [v, v, v]
            v = [float(x) for x in v]
            body.body = body.body.scale(v)
        elif name == "mirror":
            v = self._get_arg(args, 0, "v", [1, 0, 0])
            v = self._to_vec3(v)
            body.body = body.body.mirror(v)
        elif name == "resize":
            newsize = self._get_arg(args, 0, "newsize", [0, 0, 0])
            newsize = [float(x) for x in newsize]
            bb = body.body.bounding_box()  # (xmin,ymin,zmin,xmax,ymax,zmax)
            sx = newsize[0] / (bb[3] - bb[0]) if newsize[0] != 0 and (bb[3]-bb[0]) != 0 else 1
            sy = newsize[1] / (bb[4] - bb[1]) if newsize[1] != 0 and (bb[4]-bb[1]) != 0 else 1
            sz = newsize[2] / (bb[5] - bb[2]) if newsize[2] != 0 and (bb[5]-bb[2]) != 0 else 1
            body.body = body.body.scale([sx, sy, sz])
        elif name == "multmatrix":
            m = self._get_arg(args, 0, "m", None)
            if m is not None:
                mat = self._to_matrix4x3(m)
                body.body = body.body.transform(mat)

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

        result = children[0].body
        for child in children[1:]:
            if op == "union":
                result = result + child.body
            elif op == "difference":
                result = result - child.body
            elif op == "intersection":
                result = result ^ child.body

        return ColoredBody(body=result, color=children[0].color)

    def _builtin_hull(self, node: ModularCall, ctx: EvalContext) -> Optional[ColoredBody]:
        children = self._eval_children(node.children, ctx)
        if not children:
            return None
        bodies = [c.body for c in children]
        result = m3d.Manifold.batch_hull(bodies)
        return ColoredBody(body=result, color=children[0].color)

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

    # --- for loops ---

    def _eval_for(self, node: ModularFor, ctx: EvalContext) -> list[ColoredBody]:
        # Collect (name, [values]) for each assignment
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                values = []
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

    # --- let ---

    def _eval_let_block(self, node: ModularLet, ctx: EvalContext) -> list[ColoredBody]:
        return self._eval_children(node.body if hasattr(node, 'body') else [], ctx)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _combine(self, bodies: list[ColoredBody]) -> ColoredBody:
        if len(bodies) == 1:
            return bodies[0]
        composed = m3d.Manifold.compose([b.body for b in bodies])
        return ColoredBody(body=composed, color=bodies[0].color)

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
            return a + b
        if isinstance(node, SubtractionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, list) and isinstance(b, list):
                return [x - y for x, y in zip(a, b)]
            return a - b
        if isinstance(node, MultiplicationOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if isinstance(a, list) and isinstance(b, (int, float)):
                return [x * b for x in a]
            if isinstance(b, list) and isinstance(a, (int, float)):
                return [a * x for x in b]
            return a * b
        if isinstance(node, DivisionOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            if b == 0:
                return None
            return a / b
        if isinstance(node, ModuloOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return a % b
        if isinstance(node, ExponentOp):
            a, b = self._eval_expr(node.left, ctx), self._eval_expr(node.right, ctx)
            return a ** b
        if isinstance(node, UnaryMinusOp):
            v = self._eval_expr(node.expr, ctx)
            if isinstance(v, list):
                return [-x for x in v]
            return -v
        if isinstance(node, LogicalAndOp):
            return bool(self._eval_expr(node.left, ctx)) and bool(self._eval_expr(node.right, ctx))
        if isinstance(node, LogicalOrOp):
            return bool(self._eval_expr(node.left, ctx)) or bool(self._eval_expr(node.right, ctx))
        if isinstance(node, LogicalNotOp):
            return not bool(self._eval_expr(node.expr, ctx))
        if isinstance(node, EqualityOp):
            return self._eval_expr(node.left, ctx) == self._eval_expr(node.right, ctx)
        if isinstance(node, InequalityOp):
            return self._eval_expr(node.left, ctx) != self._eval_expr(node.right, ctx)
        if isinstance(node, GreaterThanOp):
            return self._eval_expr(node.left, ctx) > self._eval_expr(node.right, ctx)
        if isinstance(node, GreaterThanOrEqualOp):
            return self._eval_expr(node.left, ctx) >= self._eval_expr(node.right, ctx)
        if isinstance(node, LessThanOp):
            return self._eval_expr(node.left, ctx) < self._eval_expr(node.right, ctx)
        if isinstance(node, LessThanOrEqualOp):
            return self._eval_expr(node.left, ctx) <= self._eval_expr(node.right, ctx)
        if isinstance(node, TernaryOp):
            cond = self._eval_expr(node.condition, ctx)
            return self._eval_expr(node.true_expr, ctx) if cond else self._eval_expr(node.false_expr, ctx)
        if isinstance(node, PrimaryCall):
            return self._eval_function_call(node, ctx)
        if isinstance(node, PrimaryIndex):
            obj = self._eval_expr(node.left, ctx)
            idx = self._eval_expr(node.index, ctx)
            if isinstance(obj, (list, str)) and isinstance(idx, (int, float)):
                try:
                    return obj[int(idx)]
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
            return self._eval_expr(node.expr, child_ctx)
        if isinstance(node, EchoOp):
            self._do_echo(node.arguments, ctx)
            return self._eval_expr(node.body, ctx)
        if isinstance(node, AssertOp):
            return self._eval_expr(node.body, ctx)
        if isinstance(node, FunctionLiteral):
            return node  # lambda — store for later call
        # Unknown — return None
        return None

    def _eval_identifier(self, node: Identifier, ctx: EvalContext) -> Any:
        name = node.name
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
                    result.extend(self._eval_list_comp_body(elem.body, ctx))
            elif isinstance(elem, ListCompIfElse):
                branch = elem.if_body if self._eval_expr(elem.condition, ctx) else elem.else_body
                result.extend(self._eval_list_comp_body(branch, ctx))
            elif isinstance(elem, ListCompLet):
                pass  # TODO: let in list comp
            elif isinstance(elem, ListCompEach):
                v = self._eval_expr(elem.body, ctx)
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, list):
                            result.extend(item)
                        else:
                            result.append(item)
            else:
                result.append(self._eval_expr(elem, ctx))
        return result

    def _eval_list_comp_body(self, body, ctx: EvalContext) -> list:
        if isinstance(body, ListComprehension):
            return self._eval_list_comp(body, ctx)
        if isinstance(body, ListCompIf):
            if self._eval_expr(body.condition, ctx):
                return self._eval_list_comp_body(body.true_expr, ctx)
            return []
        v = self._eval_expr(body, ctx)
        return [v] if v is not None else []

    def _eval_listcomp_for(self, node: ListCompFor, ctx: EvalContext) -> list:
        var_seqs: list[tuple[str, list]] = []
        for assign in node.assignments:
            name = assign.name.name
            values = self._eval_expr(assign.expr, ctx)
            if values is None:
                values = []
            elif not isinstance(values, list):
                values = [values]
            var_seqs.append((name, values))

        result = []
        for combo in self._cartesian(var_seqs):
            loop_ctx = ctx.child_ctx()
            for vname, val in combo:
                loop_ctx.dyn[f"__let_{vname}"] = val
            result.extend(self._eval_list_comp_body(node.body, loop_ctx))
        return result

    def _eval_range(self, node: RangeLiteral, ctx: EvalContext) -> list:
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

        if increment is None or increment == 0:
            return []
        start = float(start) if start is not None else 0.0
        stop = float(stop) if stop is not None else 0.0
        increment = float(increment)

        result = []
        v = start
        if increment > 0:
            while v <= stop + 1e-10:
                result.append(v)
                v += increment
        else:
            while v >= stop - 1e-10:
                result.append(v)
                v += increment
        return result

    def _eval_function_call(self, node: PrimaryCall, ctx: EvalContext) -> Any:
        func_node = self._eval_expr(node.left, ctx)
        name = node.left.name if isinstance(node.left, Identifier) else None
        args = self._resolve_args(node.arguments, ctx)

        # Built-in math functions
        math_fns = {
            "abs": abs, "ceil": math.ceil, "floor": math.floor,
            "round": round, "sqrt": math.sqrt, "ln": math.log,
            "log": math.log10, "exp": math.exp,
            "sin": lambda x: math.sin(math.radians(x)),
            "cos": lambda x: math.cos(math.radians(x)),
            "tan": lambda x: math.tan(math.radians(x)),
            "asin": lambda x: math.degrees(math.asin(x)),
            "acos": lambda x: math.degrees(math.acos(x)),
            "atan": lambda x: math.degrees(math.atan(x)),
            "atan2": lambda y, x: math.degrees(math.atan2(y, x)),
            "max": max, "min": min,
            "pow": pow,
            "norm": lambda v: math.sqrt(sum(x*x for x in v)),
            "cross": lambda a, b: [
                a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]
            ],
            "concat": lambda *args: sum((list(a) if isinstance(a, list) else [a] for a in args), []),
            "len": len,
            "str": lambda *a: "".join(x if isinstance(x, str) else self._fmt_val(x) for x in a),
            "chr": chr,
            "ord": ord,
            "is_undef": lambda x: x is None,
            "is_num": lambda x: isinstance(x, (int, float)),
            "is_bool": lambda x: isinstance(x, bool),
            "is_string": lambda x: isinstance(x, str),
            "is_list": lambda x: isinstance(x, list),
        }
        if name and name in math_fns:
            positional = [args[k] for k in sorted(k for k in args if isinstance(k, int))]
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
                pos = getattr(node, 'position', None)
                loc = f" at {pos.origin}:{pos.line}" if pos else ""
                self.error(f"undefined function '{name}'{loc}")

        return None

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
        child_ctx = ctx.child_ctx(scope=decl.scope if hasattr(decl, 'scope') and decl.scope else ctx.scope)
        for k, v in bound.items():
            child_ctx.dyn[f"__let_{k}"] = v
        self._apply_defaults(params, child_ctx, ctx)
        pos = getattr(call_node, 'position', None)
        self._call_stack.append((name, pos))
        try:
            return self._eval_expr(decl.expr, child_ctx)
        finally:
            self._call_stack.pop()
