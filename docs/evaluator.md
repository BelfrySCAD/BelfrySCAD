# AST Evaluator Reference

The evaluator sits between openscad_lalr_parser and Manifold: a recursive AST walker producing Manifold geometry from a parsed AST.

## Scope processing

Call `build_scopes()` immediately after parsing to annotate every node with `.scope`. Three independent namespaces — variables, functions, modules — with automatic parent-chain lookup:

```python
scope.lookup_variable(name)  # returns the Assignment/ParameterDeclaration node
scope.lookup_function(name)  # returns the FunctionDeclaration node
scope.lookup_module(name)    # returns the ModuleDeclaration node or None (built-in)
```

Declarations are hoisted within their block (forward references work). Last-wins scoping is implemented by the library — later assignments in the same scope overwrite earlier ones.

## Architecture

Recursive AST walker with a type→method dispatch table (`_EXPR_DISPATCH`):

`_eval_expr(node, ctx)` does a single `dict.get(type(node))` to look up a handler method (e.g. `AdditionOp` → `_expr_add`), replacing the earlier `isinstance` chain. The dispatch table is a module-level dict built after the `Evaluator` class definition. Statement dispatch (`_eval_statement`) still uses `isinstance` since its node-type set is smaller.

1. `ModularCall`: look up via `scope.lookup_module(name)` — `None` → dispatch to built-ins table; found → recursively evaluate the module body in a new child scope
2. `Identifier` in an expression: `scope.lookup_variable(name)` then evaluate the bound value; if not found, the identifier is `undef` (matches real OpenSCAD — variables and functions/modules live in separate namespaces, so a bare reference to `function f(x) = ...` is an unknown variable, not a value, and `is_function(f)` is `false`)
3. Function call: if `name` resolves via `scope.lookup_function(name)`, evaluate args in caller's scope, body in new scope (`_eval_user_function`). Otherwise, if the callee expression evaluates to a `FunctionLiteral` (e.g. a variable holding `function (params) expr`), call it via `_eval_function_literal`, closing over the literal's own `.scope` — this is how function *values* (`g = function(x) x*2; g(3)`) are invoked
4. Default parameter values are evaluated in the **caller's** scope, not the callee's

## Assignment execution order

Within each scope (top-level, module body, `if`/`for` block), all `Assignment` nodes evaluate **before** any geometry statements, matching OpenSCAD's last-wins semantics. E.g. `a = 5; cube(a); a = 10;` produces a 10×10×10 cube — both assignments run before `cube(a)`. Applies recursively at every level processed by `evaluate()` and `_eval_children()`.

Assignments are **eager**: `_eval_statement` evaluates an `Assignment`'s expression immediately, storing it in `ctx.dyn` as `__let_{name}`. `_eval_identifier` checks `ctx.dyn` first, so the cached value serves later references in the same scope. Forward references (used before assigned in source order) fall back to `scope.lookup_variable()` and lazy evaluation.

A variable assigned twice in the same scope: the second overwrites the first and emits:
```
WARNING: a was assigned on line 1 but was overwritten in file foo.scad, line 3
```
matching OpenSCAD's exact format. `EvalContext.dyn_positions` tracks each `__let_*` entry's source position for this. The warning only fires when `dyn_positions` already has an entry for that name — a parameter binding (from `_bind_args`/`_apply_defaults`) sets `ctx.dyn` but not `dyn_positions`, so a body assignment that normalizes a parameter (e.g. `anchor = default(anchor, CENTER);`, BOSL2's standard pattern) does not spuriously warn.

Every declared parameter gets a `__let_*` entry from `_apply_defaults` — `undef` (`None`) if it has no default and the caller didn't supply one. Without this, a body statement shadowing a parameter name with a self-referential expression (e.g. BOSL2's `chamfer = approx(chamfer,0) ? undef : chamfer;`) would resolve `chamfer` via `scope.lookup_variable` to that same hoisted Assignment instead of the parameter, recursing forever.

`_eval_children` shares `ctx.dyn` (not a copy) across siblings so eager assignments are immediately visible to subsequent siblings.

`EvalContext` uses `__slots__` for fast attribute access and has two context-creation methods with different inheritance rules:

| Method | `__let_*` inherited | Use for |
|---|---|---|
| `child_ctx()` | Yes (full copy) | `for`/`let` iterations, `_eval_let_block`, list comprehension scopes — outer bindings must stay visible |
| `call_ctx()` | No (only `$*` dynamic vars) | Module/function calls — callee has its own variable scope; inheriting caller `__let_*` would trigger spurious double-assignment warnings |

Both methods accept `children_nodes` and `children_caller_ctx` to propagate deferred children through `for`/`let`/`intersection_for` blocks (so `children()` works inside loops).

`_call_ctx_for(decl, ctx, ...)` picks between the two for a module/function call: it walks `_call_stack` and uses `child_ctx()` (inherit `__let_*`) if `decl`'s source span is *strictly contained* within an already-active frame's declaration span — i.e. `decl` is a module/function declared lexically inside the body of a module/function currently being evaluated (a closure over that call's locals), otherwise `call_ctx()` (isolated). Direct recursion (a declaration's span containing itself) is excluded from "nested" so a recursive call doesn't inherit its own in-progress locals as if they were its caller's. This is what lets BOSL2's `cuboid()` — which reassigns its `edges` parameter (`edges = _edges(edges, ...)`) and then calls a nested `module corner_shape() { ... }` referencing `edges` — see the *reassigned* value instead of recursing forever back into `scope.lookup_variable("edges")` → the same reassignment's own RHS.

## Built-ins implemented

**3D Primitives** (→ `ColoredBody.body`): `cube`, `sphere`, `cylinder`, `polyhedron`. `polyhedron` deduplicates coincident vertices (within 1e-6 after rounding to 6 decimal places) and discards degenerate triangles before constructing a Manifold, since VNF meshes from libraries like BOSL2 often have coincident vertices at seams/poles that would otherwise cause `NotManifold` errors.

**2D Primitives** (→ `ColoredBody.section`): `circle`, `square`, `polygon`, `text`

`polygon()` uses `m3d.FillRule.EvenOdd` (matching OpenSCAD), which fills the interior regardless of contour winding direction. The default Manifold fill rule (`Positive`) would silently produce an empty `CrossSection` for clockwise-wound polygons — BOSL2's `teardrop2d()` returns CW polygons, which broke `onion()` and any shape that revolves a teardrop profile. `paths`-based polygons already used `EvenOdd`; this makes the no-paths case consistent.

**Extrusion** (2D → 3D): `linear_extrude`, `rotate_extrude`, `roof`

**Transforms** (3D and 2D): `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `resize`, `color`, `offset`

**Booleans** (3D or 2D, dispatched by child type): `union`, `difference`, `intersection`

`_resolve_csg`/`_generate_csg` (see "CSG tree" below) treat each **top-level child statement** separately. All bodies produced by a single statement are unioned together before the CSG operation is applied across statements. This matches OpenSCAD's implicit-union-within-scope rule: in `difference() { A; B; }`, if A evaluates to multiple bodies (e.g., a parent geometry plus an attached child returned by BOSL2's `attachable()`), all of A's bodies form the positive operand (unioned), not a chain of differences. Without this grouping, `difference()`'s flat body list would treat the 2nd body as a subtractor instead of part of the base.

Empty statements are handled with correct set semantics: if a child statement produces no solid geometry (e.g., a background-modifier `%` or a disabled `*` child, or an `attachable()` whose `_is_shown()` returns false due to tag filtering), `intersection()`'s combined result is empty (`∅ ∩ B = ∅`), and `difference()`'s result is empty when its first operand is empty (`∅ - B = ∅`). For `union()`, an empty contributor is simply skipped. This prevents clip geometry internal to BOSL2 modules like `half_of()` / `bottom_half()` from escaping as spurious output when tag filtering suppresses the object being clipped. This discard-vs-skip decision is made in `_generate_csg`, from each statement's actually-generated bodies — every statement is always resolved (no short-circuiting at the AST-walk stage), so a statement *after* one that ends up empty still runs its own side effects (e.g. `echo()`), even though its geometry contribution is ultimately discarded from the combined result.

**Topology**: `hull`, `minkowski`, `projection`

**Control / utility**: `for`, `intersection_for`, `let`, `if`/`else`, `echo`, `assert` (modular + expression forms), `render`, `children()`, `breakpoint()`

**Modular modifiers** — OpenSCAD's prefix operators applied to module calls. Each modifier tags the resulting `ColoredBody` list via the `role` field and/or filters the output:
- `*` (disable) — `ModularModifierDisable`: child produces no geometry; equivalent to commenting it out
- `!` (show-only) — `ModularModifierShowOnly`: tags children with `role="show_only"`; at top-level `evaluate()`, if any `show_only` bodies exist the result is filtered to only `show_only` + `highlight` bodies, suppressing all `normal` and `background` siblings
- `%` (background) — `ModularModifierBackground`: child geometry is evaluated and tagged `role="background"`; background bodies are excluded from CSG operations (`union`/`difference`/`intersection`/`hull`/`minkowski`) and passed through transforms separately; the renderer displays them as translucent ghosts and they are not selectable via ray-cast
- `#` (highlight) — `ModularModifierHighlight`: child geometry is evaluated normally and tagged `role="highlight"`; the renderer renders highlight bodies opaquely (like normal) plus a pink translucent overlay pass

`ColoredBody.role` field: `"normal"` (default) | `"highlight"` | `"background"` | `"show_only"`. Role is preserved through all transform, color, and module calls — `_generate_transform`, `_generate_color`, and `_eval_user_module` (via `generate_tree()`'s default child-concatenation) all return `list[ColoredBody]` without merging, so each body's role flows through the chain.

**Data**: `object`, `is_object`, `has_key`, `textmetrics`, `fontmetrics`

`has_key(obj, key)` — returns `true` if string `key` exists in `obj` (an `OscObject`); `undef` for non-object first argument. Experimental feature in real OpenSCAD (`--enable=object-function`).

`breakpoint()` — pauses the debugger at the call site. Optional first positional/keyword `condition`: skipped if falsy. No-op outside the debugger. Implemented via `_check_debug(node, ctx, forced=True)`, which passes `forced=True` to the debug hook to bypass the normal step/breakpoint-line check.

**Math functions**: `abs`, `sign`, `ceil`, `floor`, `round`, `sqrt`, `ln`, `log`, `exp`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `min`, `max`, `pow`, `norm`, `cross`, `rands`, `lookup`

**String / list functions**: `str`, `chr`, `ord`, `concat`, `len`, `search`

**Type checks**: `is_undef`, `is_bool`, `is_num`, `is_string`, `is_list`, `is_function`

Note: `is_range`, `is_nan`, and `is_finite` are **not** real OpenSCAD builtins despite the `is_*` naming convention — they're ordinary functions defined by BOSL2 (`utility.scad`). Calling them without BOSL2's `std.scad` included emits `WARNING: Ignoring unknown function '...'` and evaluates to `undef`, matching real OpenSCAD. Do not add them to `math_fns` — doing so would shadow BOSL2's own definitions.

`is_function(x)` is `isinstance(x, (FunctionDeclaration, FunctionLiteral))`. In practice only `FunctionLiteral` values (`g = function(x) ...`) ever reach it as a value — a `FunctionDeclaration` (`function f(x) = ...`) is never returned by identifier lookup (see Architecture #2), so `is_function(f)` for a named function `f` is `false`, matching real OpenSCAD.

`is_num(x)` is `false` for `nan` (`is_num(0/0)` → `false`), even though `nan` is a Python `float` — matching real OpenSCAD's quirk that `nan` fails `is_num()` while `inf`/`-inf` pass. `math_fns["is_num"]` explicitly excludes `math.isnan(x)`.

**Constants**: `PI`

**Other**: `version`, `version_num`, `parent_module`; **`$parent_modules`** (int, the number of parent module call-stack frames at the point a module's body is entered — 0 at top level, 1 inside one module, etc.)

**`surface(file, center=false, invert=false)`**: loads a heightmap from a `.dat` text file or PNG and builds a closed solid mesh. `.dat`: whitespace-separated number matrix; `#`-prefixed and blank lines ignored; first row = highest Y (OpenSCAD convention). PNG: linear luminance `Y = 0.2126R + 0.7152G + 0.0722B` scaled to 0–100; `invert=true` flips the mapping. `center=true` centers on X/Y; bottom face always at z=0. Requires Pillow for images.

**`import(file, convexity=10, layer=undef)`**: loads external files. Behaviour depends on context and file extension:
- **Module context** (geometry statement): `.stl`/`.obj`/`.off`/`.3mf` → 3D `ColoredBody`; `.svg` → 2D `CrossSection` (Y-axis flipped from SVG convention); `.dxf` → 2D `CrossSection` from closed LWPOLYLINE/POLYLINE entities (`layer` filters by DXF layer name; requires `ezdxf` — `pip install ezdxf`).
- **Expression context** (right-hand side of assignment): `.json` → parsed data (list/number/string/bool/null); `.stl`/`.obj`/`.off`/`.3mf` → VNF `[[verts], [faces]]` where each vert is `[x,y,z]` and each face is a list of vertex indices; `.dxf`/`.svg` → Region `[[[x,y],...],...]` (list of closed paths).
- Path resolved relative to the source `.scad` file (same as `surface()`). `convexity` is accepted and ignored (preview hint). Binary and ASCII STL are both supported. SVG: `<path>`, `<polygon>`, `<polyline>`, `<rect>`, `<circle>`, `<ellipse>` elements with `transform` stack (translate/scale/rotate/matrix); Bezier curves flattened to 32-segment polylines; `<defs>`/`<symbol>` skipped. 3MF parsed via stdlib `zipfile`+`ElementTree` (no lib3mf needed).

**Special variables**: `$fn`, `$fa`, `$fs` control mesh resolution. `$children` = the number of module-instantiation child *statements* in the `{}` block passed to this module call (`len(call.children)`, excluding `Assignment`/`ModuleDeclaration`/`FunctionDeclaration`), not the number of geometries they produce — e.g. `children()` counts as one child even when it forwards zero bodies, and `if (false) sphere();` still counts as one child. `$`-prefixed named args in any call (e.g. `sphere(r=2, $fn=64)`) merge into the dynamic context for that call and its children.

**Viewport special variables**: `$vpt` (= `camera.target` as `[x,y,z]`), `$vpr` (= `[((90-altitude)%360+360)%360, 0, ((azimuth-270)%360+360)%360]`), `$vpd` (= `camera.distance`) are injected into the root `EvalContext.dyn` at render/debug start, snapshotted in the main thread via `MainWindow._viewport_params(tab)` before the worker thread launches. `Evaluator.evaluate()` accepts `viewport_params: dict | None` and merges it into `ctx.dyn` before processing.

**`EvalContext.dyn_explicit`**: a `set[str]` tracking which `$`-prefixed names the *script itself* assigned via a `$var = ...;` statement (`_eval_statement_impl`'s `Assignment` branch adds to it alongside the `ctx.dyn[name] = ...` write), as opposed to names merely *present* in `dyn` because they were seeded — either from `_DEFAULT_DOLLAR` (`$fn`/`$fa`/`$fs`/`$t`/`$parent_modules`) or from `viewport_params` (the current camera's `$vpt`/`$vpr`/`$vpd`/`$vpf`, injected above). Threaded through `child_ctx`/`call_ctx` (copied, like `dyn` itself) and `let_child_ctx`/the sibling-assignment path in `_eval_children` (shared by reference, also like `dyn`) so it obeys the same scoping rules. `_build_frame_locals` exposes each frame's `dyn_explicit` set (for the debugger's `all_frame_locals`); `Evaluator.evaluate()`'s caller can also read `_root_ctx.dyn_explicit` directly. `MainWindow` uses this to distinguish "the script set `$vpt`, move the camera" from "`$vpt` merely reflects the camera's position at render/pause start" — without it, re-applying the viewport-seeded (not script-assigned) `$vp*` values back to the camera on every render or debugger pause would silently overwrite any manual orbit/pan/zoom the user did in between, since a seeded-but-unassigned value is indistinguishable from an explicit one by membership in `dyn` alone.

**Animation variable `$t`**: defaults to `0.0` in `EvalContext.dyn` (rest position). During playback, `MainWindow._viewport_params(tab)` includes `"$t": tab.animate_pane.current_t()`, where `current_t() = step / steps` for `step` in `0..steps-1` — range `[0, 1 - 1/steps)`, matching the [OpenSCAD animation spec](https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Animation) (the cycle never reaches `$t=1`, avoiding a hitch on `rotate([0,0,$t*360])`-style loops).

## originalID assignment

Each geometry-producing node (primitives and their transform/boolean ancestors) gets a unique Manifold `originalID` via `ReserveIDs`. The evaluator builds and returns the `originalID → AST node` lookup table alongside the mesh.

## CSG tree (Phase 2 of a planned multi-phase evaluator refactor — complete)

`evaluate()` is a genuine two-pass process. The AST walk (**resolve**) builds `self.csg_tree: list[CSGNode]` — an explicit, persistent tree mirroring the shape of the geometry-producing statements in the script — as plain data, with no Manifold calls at all. A separate walk over the completed tree, `generate_tree()` (**generate**), then does all the Manifold/CrossSection work and populates each node's `bodies`. `evaluate()` calls `generate_tree(self.csg_tree)` once, after the AST walk completes, and returns its result. `csg_tree` complements, not replaces, `id_to_node` (originalID → AST node, above): `id_to_node` is a fine-grained per-triangle reverse lookup for WYSIWYG ray-cast picking; `csg_tree` is a coarse per-statement tree for structural inspection (e.g. a future debugger tree view / partial-render preview, Phase 3 — not yet implemented). Reset at the start of every `evaluate()` call, alongside `_call_stack`/`_frame_ctxs`.

`CSGNode(kind, node, bodies, is_builtin, children, params)`: `kind` is a human-readable label — a `ModularCall`'s call name (`"cube"`, `"union"`, or a user module's own name), or `"highlight"`/`"background"`/`"show_only"`/`"intersection_for"` for the non-`ModularCall` node types below. `is_builtin` is only meaningful for `ModularCall` (`False` when the name resolves via `scope.lookup_module`); it's what disambiguates a user module that happens to shadow a builtin name (e.g. a user `module union() {...}`) from the real thing — `kind` alone is not a unique discriminator. `params` is the plain-data output of the node's resolve step (already-evaluated numbers/lists/strings/resolved color — no Manifold objects). `bodies` starts empty and is populated only by `generate_tree()`.

Only five AST node types get their own `CSGNode` (`Evaluator._TREE_NODE_TYPES`): `ModularCall` (covers every primitive/transform/boolean/`hull`/`minkowski`/`children()`/`render()`/`breakpoint()`/user-module call), the three tagging modifiers `ModularModifierHighlight`/`Background`/`ShowOnly` (`#`/`%`/`!`), and `ModularIntersectionFor`. Everything else `_eval_statement` handles (`Assignment`, `ModularFor`, `ModularIf`/`IfElse`, `ModularLet`, `ModularEcho`, `ModularAssert`) is transparent in the tree — no synthetic node — so e.g. `for (i=[0:2]) cube(i);` produces three sibling `cube` nodes at the enclosing level, not a wrapping "for" node. `ModularIntersectionFor` is the one exception that is *not* transparent: like `union`/`difference`/`intersection`, it combines its per-iteration children into a single result (via `^`), so its iterations nest under one `intersection_for` node exactly like `union()`'s children nest under a union node — treating it as transparent would make `flatten_csg_tree()` return the pre-intersection per-iteration bodies instead of the actual combined result. `ModularModifierDisable` (`*`) produces no `CSGNode` at all, matching its existing behavior of never evaluating its child.

**Resolve** (`Evaluator._RESOLVE_DISPATCH: dict[str, Callable]`, keyed by `kind`): every builtin geometry-producing kind (primitives, transforms, color, booleans, topology, extrusion, `surface`/`import`, and the control-ish builtins `render`/`children`/`breakpoint`/the three tag modifiers) has a `_resolve_X(node, ctx) -> dict` that parses arguments and recursively resolves children (via `_eval_children`/`_eval_statement`, called purely for the tree-building side effect — the return value is unused, since real bodies don't exist until generate) into a plain-data `params` dict. `_eval_statement` computes `(kind, is_builtin)` up front, looks up `resolve_fn` (falling back to `_resolve_fallback_call` — which reuses the ordinary `_eval_modular_call` dispatch for its side effect — for user-module calls and genuinely unknown module names), pushes/pops a `self._tree_stack` frame around the call (so a raised `EvalError` cleanly discards the in-progress node rather than corrupting a parent's accumulator), and always returns `[]`.

`_eval_statement` calls `self._check_debug(node, ctx)` itself (guarded by `self._debugging`) for every `_TREE_NODE_TYPES` node, right before dispatching to `resolve_fn` — `_eval_statement_impl` (which calls `_check_debug` for every other statement type, at its own top) is never reached for these nodes, since the `isinstance` check always routes them to this wrapper instead. This was missing from Phase 2 step 1 through step 6 (no test exercised a debug stop on a geometry statement), meaning no `ModularCall`/modifier/`intersection_for` statement ever paused the debugger during that window — fixed alongside Phase 3.

**Generate** (`Evaluator._GENERATE_DISPATCH`, same keys): `_generate_X(params, children, node) -> list[ColoredBody]` does the actual Manifold/CrossSection work, reading pre-resolved `params` and its own already-generated `children` (via `flatten_csg_tree(children)`, which reads each child's `.bodies`). `generate_tree(tree)` walks bottom-up — recursing into `node.children` *before* calling `node`'s own `generate_fn`, so `flatten_csg_tree` always sees populated data — and for any kind with no registered `generate_fn` (user-module calls, `render()`/`children()` passthroughs, unknown modules), defaults to concatenating the children's bodies, matching a module body's plain concatenation semantics. It always recomputes from scratch (no caching), matching the project's "full Manifold rebuild on every render trigger" convention — it can be called on any (possibly partial) tree at any point, which is what makes a future debugger able to render a live partial result at a breakpoint (Phase 3) by calling `generate_tree(self.csg_tree)` directly.

Since resolve never depends on any node's generated bodies, `union`/`difference`/`intersection` (`_resolve_csg`/`_generate_csg`) always resolve every top-level statement (no short-circuiting during the walk) — `_generate_csg` alone decides, from real generated bodies, whether an empty operand discards the whole result (`intersection`, or `difference` before any positive operand is established) or is just skipped (`union`, or a later `difference` operand). One consequence: a statement *after* one whose geometry ends up empty still has its own side effects (e.g. `echo()`) run during resolve, even though its geometry is ultimately discarded from the combined result — unlike a pre-Phase-2 short-circuit, which would have skipped evaluating it at all.

`flatten_csg_tree(tree)` concatenates each **top-level** node's `.bodies` (not recursing into `.children` — a parent's `.bodies` already is the fully-combined result). It reproduces `evaluate()`'s returned body list exactly for any script without a top-level `!`; `evaluate()`'s own show_only filter (see above) runs once, after `generate_tree()` returns, and is not itself represented by any tree node — so for scripts using top-level `!`, `evaluate()`'s result is `[b for b in flatten_csg_tree(tree) if b.role in ("show_only", "highlight")]`.

## 2D geometry

`ColoredBody` carries either a 3D `body: Manifold` or a 2D `section: CrossSection` (not both). 2D primitives (`circle`, `square`, `polygon`) return only `section`. `linear_extrude`/`rotate_extrude` consume 2D children via `_to_cross_section()` (unions all child sections) and return a 3D body. Booleans dispatch on whether children carry 3D bodies or 2D sections; `_combine()` handles mixed children — uses 3D bodies if any present, else unions sections.

`manifold3d.CrossSection` supports full 2D CSG: `+` (union), `-` (difference), `^` (intersection), `offset`, `hull`, `batch_hull`, `revolve`, `extrude`, and all 2D transforms. `CrossSection.to_polygons()` returns contours for polygon construction.

`_generate_transform` dispatches on child type per body: `_apply_transform_2d` handles `CrossSection` (via `cs.translate/rotate/scale/mirror`); `_apply_transform_3d` handles `Manifold`. `resize` and `multmatrix` are 3D-only — 2D children pass through unchanged. Each child `ColoredBody` (including background bodies) is transformed individually, preserving its `role`. Returns `list[ColoredBody]` rather than a single merged body. So `translate([4,0]) circle(r=1)` and similar 2D transform chains work, including as `hull()` inputs.

**Top-level 2D results** (e.g. `circle();` with no enclosing `linear_extrude`/`rotate_extrude`) are returned from `evaluate()` as `section`-only `ColoredBody`s, per the above — `evaluate()` itself stays pure. The renderer/exporter only handle Manifold meshes, so `to_renderable_bodies()` (called by `main_window.py` right after `evaluate()`, for both normal renders and debug-finish) converts any `section`-only entry into a thin `Manifold.extrude(section, _TOP_LEVEL_2D_HEIGHT)` (`1e-3`) — giving a flat-looking preview/export, similar to real OpenSCAD's flat 2D view.

## Color propagation

`color()` sets the current color in the evaluation context, cascading to all child geometry. The evaluator passes per-body color to the renderer alongside the mesh. `color()` affects viewport display, passed through to ModernGL.

## Error handling

Runtime errors raise `EvalError` and are reported to the console; last-valid geometry stays in the viewport.

Error format matches OpenSCAD exactly:
```
ERROR: Assertion 'false' failed: "message" in file foo.scad, line 5
TRACE: called by 'assert' in file foo.scad, line 5
TRACE: call of 'inner()' in file foo.scad, line 4
TRACE: called by 'inner' in file foo.scad, line 2
TRACE: call of 'outer()' in file foo.scad, line 1
TRACE: called by 'outer' in file foo.scad, line 7
```

Unknown modules emit `WARNING: Ignoring unknown module 'name' in file ..., line n` with the same TRACE lines, without raising.

Unknown functions emit `WARNING: Ignoring unknown function 'name' in file ..., line n` (no TRACE lines, even when called from inside a user-defined function/module) and evaluate to `undef`, without raising — matching real OpenSCAD. A call to an unimplemented/unrecognized builtin (e.g. `sort()`, which this OpenSCAD version doesn't have) is therefore non-fatal.

Unknown variables emit `WARNING: Ignoring unknown variable 'name' in file ..., line n` (no TRACE lines) and evaluate to `undef`, for *any* unresolved identifier — including a bare reference to a named function declaration (`function f(x) = ...; f` is an "unknown variable", since functions and variables live in separate namespaces). `_eval_identifier()` takes a `warn_if_undef` flag (default `True`); `_eval_function_call()` passes `False` when probing whether a plain-identifier callee is a variable holding a `FunctionLiteral`, so a genuinely unknown function (e.g. `sort(...)`) produces exactly one warning ("Ignoring unknown function"), not two.

`_call_stack` entries: both modules and functions are 4-tuples `("module"|"function", name, call_pos, decl_pos)` (call site + declaration start). `error(msg, node=None, innermost_frame=None)` takes the failing node and an optional innermost frame label (e.g. `"assert"`) for the first TRACE line. If `error_break_fn` is set (debug mode), `error()` calls it before raising `EvalError`, pausing the debugger at the error site.

`Evaluator.__init__` accepts an optional `return_hook(name, value, depth)` callable. When set, it is called by `_eval_user_function` and `_eval_function_literal` after the function body evaluates and before the call stack frame is popped. `depth` is `len(self._call_stack)` at the moment of return (i.e., including the returning function's own frame). `DebugSession` uses this to print return values to the console during step-out and step-over.

## Special variable scoping (`$variables`)

`$`-prefixed variables (`$fn`, `$fa`, `$fs`, `$t`, `$children`, etc.) use **dynamic scoping** — inherited down the **call chain**, not the lexical scope chain, unlike regular variables.

The evaluator maintains a separate dynamic binding context threaded through each module call. `$fn=32` on a module invocation propagates to all nested calls within it, regardless of lexical scope. `scope.lookup_variable()` must not be used for `$`-prefixed names.

`children()` uses **deferred evaluation** to support this: a module's children AST nodes and caller context are stored in `EvalContext.children_nodes` / `children_caller_ctx` and only evaluated when `children()` is called (`_eval_children_lazy`). At that point, current `$`-variables (both `$name` direct assignments and `__let_$name` for-loop/let bindings) are injected into the caller's context, so `$`-variables set in the module body are visible to children — e.g. `for ($idx = ...) { children(); }` makes `$idx` available in `sphere(d=$idx+1)`.

`_eval_children_lazy` also passes the caller's own `children_nodes` / `children_caller_ctx` through to the eval context, enabling nested `children()` forwarding chains (e.g. BOSL2's `attachable → multmatrix → _multmatrix → builtin multmatrix` where each layer forwards its caller's children via `children()`).

`children(N)` (integer index) evaluates only the Nth **child statement** — not the Nth output body. This distinction matters when tag-based filtering causes a child statement to produce 0 bodies: body-index lookup would then map `children(1)` to whatever the 2nd body happens to be, which is the wrong statement. `_builtin_children` filters `ctx.children_nodes` to non-`Assignment`/non-declaration nodes (matching the `$children` count), picks the Nth node, and calls `_eval_children` on just that node with the propagated `$`-variable context. This correctly implements BOSL2's `attachable()` two-children pattern where `children(0)` is the geometry block and `children(1)` is the user-supplied attachment block — the geometry block may produce 0 bodies when filtered by `$tags_shown`, but `children(1)` must still return the attachment children.

## `include` vs `use`

Exact OpenSCAD semantics:
- `include <file.scad>` — brings all declarations and top-level geometry into the current scope
- `use <file.scad>` — brings only the used file's own functions and modules into scope; its top-level geometry and variable assignments are not injected and its variable namespace stays isolated from the using file's (in both directions)

`_resolve_use_scopes(nodes, current_file, log_fn)` in `main_window.py` implements `use`, called once from both the render-worker and debug-session paths. For each top-level `UseStatement` in `current_file`, it recursively resolves the used file's own `use` statements first, then:

- Injects only the used file's *own* `ModuleDeclaration`/`FunctionDeclaration` nodes (not ones it transitively pulled in via its own `use`) — "nested use has no effect on the base file's environment".
- Builds `current_file`'s combined `root_scope` from its own nodes plus the injected declarations, so `current_file` can call them by name.
- Re-anchors each injected declaration's `.scope` (and its body's scope tree) back to the used file's own root scope — built from the used file's own nodes plus anything *it* injected via nested `use`. This lets the injected modules/functions resolve the used file's own globals (and any nested-`use` declarations) without exposing them to `current_file`, and vice versa.

Re-anchoring works because `ModuleDeclaration.build_scope`/`FunctionDeclaration.build_scope` are idempotent: calling `.build_scope(scope)` a second time just creates a fresh child scope and reassigns `.scope` on the node and its descendants, overwriting the (incorrect) scope assigned by `current_file`'s combined `build_scopes()` call.

`Evaluator._resolve_use_statements(nodes, root_scope)` is a lightweight fallback that runs at the start of `evaluate()`. It scans for any remaining `UseStatement` nodes in the AST, parses their targets, and injects their modules/functions into `root_scope`. When the full app path is used (`_resolve_use_scopes` already stripped `UseStatement` nodes and rebuilt the scope), this is a no-op. It exists so that standalone callers (profiling scripts, tests) that pass raw `getASTfromFile()` + `build_scopes()` output directly to `evaluate()` still get correct `use` resolution.

## Implementation quirks

- `UseStatement.filepath` is a `StringLiteral` AST node, not a plain string — use `.filepath.val`.
- "file not found" errors from library resolution (e.g. internal BOSL2 files already handled by the parser) are suppressed in the console.
- `sys.setrecursionlimit(10000)` is set in `main()` for BOSL2 compatibility. `RecursionError` around `build_scopes()`/`evaluate()` is treated as a runtime error (shows last-valid geometry).
- **Ranges** are an `OscRange(start, step, end)` object, not an expanded list. `echo([1:3])` prints `[1 : 1 : 3]`. Expanded to a list only when iterated (`for`, list comprehensions, `intersection_for`). **Strings** iterated in `for`/list-comprehension/`intersection_for` are exploded into individual single-character strings (`for (c = "abc")` → `c` takes values `"a"`, `"b"`, `"c"`). A zero-step range echoes as `[1 : 0 : 5]` and iterates to nothing. **Indexing** a range with `[0]`/`[1]`/`[2]` returns its `start`/`step`/`end` components (not iterated values) — e.g. `[2:3:11][0]` → `2`, `[1]` → `3`, `[2]` → `11`, matching real OpenSCAD. This is what BOSL2's `is_finite()`/`is_range()` inspect to detect range values.
- **C-style `for` in list comprehensions** — `[for (a=v[0], i=1; i<=len(v); a = cond?a+v[i]:a, i=i+1) a]` — parses as a `ListCompCFor` node (`inits`, `condition`, `incrs`, `body`), distinct from the assignment-style `ListCompFor`. `_eval_listcomp_cfor()` binds `inits` once into a child context, then loops while `condition` is true, evaluating `body` (via `_eval_list_comp_body`) and then `incrs` *sequentially* (each `incrs` assignment sees the previous ones' new values, matching source order) each iteration. Capped at `_MAX_CFOR_ITERATIONS` (1,000,000) to avoid hangs on a malformed `incrs`/`condition`. Used by BOSL2's `cumsum()`, `product()`, etc.
- **Boolean arithmetic** returns `undef` (`None`): `true + 1` → `undef`. The evaluator checks `type(a) is bool or type(b) is bool` before any arithmetic op.
- **Vector math fast paths**: `_vec_add`, `_vec_sub`, `_scale`, `_div_scale`, and `_negate_list` use `_is_flat_numeric()` to detect flat lists of `int`/`float` (no bools, None, or nested lists) and use direct list comprehensions for small lists, switching to numpy (`np.asarray` + vectorized ops + `.tolist()`) for lists >= `_NP_VEC_THRESHOLD` (128) elements. `_matmul` uses `np.dot` for all matrix operations (mat×vec, vec×mat, mat×mat) regardless of size; vector dot products use a manual loop below the threshold.
- **`+`/`-` between lists** recurse element-wise into nested lists (`_vec_add()`/`_vec_sub()`), like `_scale()`/`_div_scale()`: `[[0,0,0,0],[0,0,0,0]] + [[1,1,1,1],[2,2,2,2]]` → `[[1,1,1,1],[2,2,2,2]]`. (A naive `zip`+Python-`+` would *concatenate* each row instead — `[0,0,0,0,1,1,1,1]` — which silently corrupted BOSL2's `_edges()`/`sum()` on edge-set matrices.)
- **Scalar × matrix/vector** multiplication recurses into nested lists (`_scale()`): `2 * [[1,2],[3,4]]` → `[[2,4],[6,8]]`, not just flat vectors.
- **List × list** multiplication (`_matmul()`) implements OpenSCAD's vector/matrix algebra: vector·vector → scalar dot product (`[1,2,3]*[4,5,6]` → `32`), matrix·vector and vector·matrix → vector, matrix·matrix → matrix. Dimension mismatches return `undef`.
- **List / scalar** division recurses into nested lists (`_div_scale()`), mirroring `_scale()`: `[2,4,6]/2` → `[1,2,3]`. `scalar/list` and `list/list` are `undef`.
- **`let(a=expr1, b=expr2, ...)`** bindings are sequential: each `exprN` is evaluated with the *previous* bindings in the same `let` already visible, so `let(a=1, b=a+1) b` → `2`. (Two bindings with the *same* name in one `let` are a separate, unhandled edge case — real OpenSCAD keeps the first and warns "Ignoring duplicate variable assignment".)
- **Division by zero** returns IEEE 754 values: `1/0` → `inf`, `-1/0` → `-inf`, `0/0` → `nan`. Math domain errors follow suit: `sqrt(-1)` → `nan`, `ln(0)` → `-inf`, `asin(2)` → `nan`. `pow(0, -1)` → `inf` likewise (`_builtin_pow()` special-cases `0 ** negative`, since Python's `pow()`/`math.pow()` raise instead of returning `inf`).
- **`sin`/`cos`/`tan`** (`_deg_trig()`) special-case exact multiples of 90 degrees to return exact table values (`0`, `±1`, or `±inf` for `tan`) instead of `math.sin/cos/tan(radians(x))`, which accumulate floating-point noise (`cos(90)` would be `6.12e-17`, `tan(90)` would be `1.63e+16`) — matching real OpenSCAD's degree-based trig. Non-multiples (e.g. `cos(90.0000001)`) are unaffected. `nan`/`inf` input returns `nan` (Python's `math.sin/cos/tan` raise `ValueError` on `inf`).
- **Negative string/list indexing** returns `undef`, not Python wraparound. `"hello"[-1]` → `undef`. `PrimaryIndex` rejects any `i < 0`.
- **`round()`** rounds half away from zero (`round(2.5)` → `3`, `round(-2.5)` → `-3`), via `math.floor(x+0.5)`/`math.ceil(x-0.5)` — NOT Python's `round()`, which rounds half to even (`round(2.5)` → `2`).
- **`floor()`/`ceil()`/`round()`** pass `nan`/`inf` through unchanged (`floor(0/0)` → `nan`, `ceil(1/0)` → `inf`) instead of raising — Python's `math.floor()`/`math.ceil()` raise `ValueError`/`OverflowError` on non-finite input.
- **`==`/`!=`** use `_osc_equal()`, not Python's `==`/`!=`: `bool` is a distinct type from `number` in OpenSCAD, so `1 == true`, `true == 1`, and `0 == false` are all `false` (Python's `==` would say `true` since `bool` is an `int` subclass). `1 == 1.0` is still `true` (both `number`). List equality recurses element-wise with the same rule, so `[1, true] == [1, 1]` → `false`; mismatched lengths are `false`.
- **`<`/`>`/`<=`/`>=`** (`_osc_comparable()`) require both operands to be the *same* OpenSCAD type — number/number (int/float mix ok), string/string, vector/vector, or bool/bool. Any other pairing (`true > 0`, `"a" < 1`, `[1,2] < 5`, `undef < 1`) emits `WARNING: undefined operation (TYPE1 OP TYPE2)` and evaluates to `undef`. (`==`/`!=` do *not* warn on type mismatches — they just return `false`.)
- **`min`/`max`** (`_builtin_minmax()`): a single list argument returns the min/max of its elements; a single scalar argument returns itself; multiple arguments must all be scalars — mixing in a vector (e.g. `min([1,5],[3,2])`) is `undef`, matching real OpenSCAD (which does *not* do element-wise min/max across vector arguments).
- **`cross()`** supports both the 3D cross product (returns a vector) and the 2D cross product `cross([a,b],[c,d])` → `a*d - b*c` (returns a scalar). Mismatched/other dimensions are `undef`.
- **`ord()`** of a multi-character string returns the code point of its *first* character (`ord("ab")` → `97`), not `undef`.
- **Named args to built-in math functions** map to positional order as fallback (e.g. `abs(x=-3)` → `3`): positional args tried first, then named args in declaration order.
- **`parent_module(idx)`** looks up `_call_stack` for only "module"-type frames (skipping function calls), reverses them, and indexes by `idx` (0 = current module, 1 = its caller, etc.). Returns `undef` when `idx` is out of range. Integer conversion is applied to `idx` since numeric literals arrive as floats from the evaluator.
- **`lookup()`** on an empty table (`lookup(5, [])`) returns `undef`, not `0`.
- `search()` match modes depend on the first argument's type:
  - **String**: character array, each character searched independently. `num_returns=1` (default) drops not-found characters; `num_returns=0` includes them as `[]`. Only valid when the vector is also a string.
  - **List**: each element is searched for independently. If an element is itself a list/vector, it's compared via **direct equality** against each whole `vector[i]` entry (`index_col` is ignored) — correct idiom for finding a string in a list of strings (`search(["foo"], ["foo","bar","baz"])` → `[0]`) and for BOSL2's `in_list(v, [UP,RIGHT,BACK])`. If an element is a scalar, it's compared against `vector[i][index_col]` (or `vector[i]` if not a list).
  - **Scalar**: returns up to `num_returns` matching indices (`[]` if none); `num_returns=0` returns all matches.
- **Assert message format**: `to_openscad([cond_expr]).strip()` recovers the condition source text for `Assertion 'expr' failed` (requires `from openscad_lalr_parser import to_openscad`).
- **String literals with leading/trailing whitespace**: the PEG parser's `skipws=True` would strip whitespace before sub-rules in `(DQUOTE, contents, DQUOTE)`, eating leading spaces (`"  bar"` → `"bar"`). Fixed in the LALR parser by using a regex terminal for string literals, avoiding whitespace skipping inside quotes.
- **`chr()`** accepts either a single code point (`chr(65)` → `"A"`) or a vector of code points (`chr([65,66,67])` → `"ABC"`), converting and concatenating each element; `chr([])` → `""`. Floats are truncated via `int()` (`chr(65.7)` / each element of a vector → `"A"`).
- **`+`/`-` involving strings**: OpenSCAD has no `+`/`-` operator for strings (unlike Python's `str.__add__`). `"ab" + "cd"` → `undef`, not Python-style concatenation `"abcd"`. `_vec_add()`/`_vec_sub()` check for `str` operands before falling back to Python's `+`/`-`.
- **Number formatting (`echo()`/`str()`)**: `_format_number()` replicates OpenSCAD's number-to-string conversion, which differs from Python's `f"{v:g}"`:
  - At most 6 significant digits.
  - Fixed-point notation is used for exponents in `[-5, 5]` (one wider than `%g`'s `[-4, 5]`): `0.00001` → `"0.00001"`, where `%g` would give `"1e-05"`.
  - Scientific notation drops the exponent's leading zero: `1000000` → `"1e+6"` (not `"1e+06"`), `1.23456789e-7` → `"1.23457e-7"` (not `"1.23457e-07"`).
  - `-0.0` → `"0"`. `nan`/`inf`/`-inf` are lowercase.
- **`roof()`** uses a three-tier algorithm. **Tier 1 (`_skeleton_roof`)** builds an exact straight skeleton for a "stable" simple polygon (single contour, no holes): `m3d.CrossSection.offset(-d, m3d.JoinType.Miter, _ROOF_MITER_LIMIT)` is a true straight-skeleton wavefront, so each vertex `k` moves at the closed-form velocity `v_k = (n1 + n2) / (1 + n1·n2)` (where `n1`/`n2` are the inward unit normals of its two adjacent edges) — moving `P0[k]` by `d * v_k` reproduces the mitered offset by `d` exactly, with no sampling. A binary search finds `d_max`, the offset distance where the polygon's area first collapses to ~0; `_offset_is_stable` then samples the offset at `d_max * {0.25, 0.5, 0.75, 0.9}` to confirm the topology stays a single `n`-vertex polygon the whole way down (no intermediate edge-collapse or split events — e.g. squares, regular polygons/circles, and L-shapes with equal-width arms are all stable). If so, `P1 = P0 + d_max * v` is the collapsed ridge/apex ring, and a watertight mesh is built directly: the bottom cap is `_ear_clip(P0)` (handles concave footprints), side faces connect `P0[k]/P0[k+1]` (z=0) to `P1[k]/P1[k+1]` (z=`d_max`, 1 triangle if they've collapsed to the same point, else 2), and all vertices are welded by position (tolerance 1e-4) before `m3d.Mesh`/`m3d.Manifold` construction. This is exact (volume/bbox match the analytic straight skeleton to ~1e-3, limited only by the binary search) and runs in well under a millisecond.

  **Tier 2 (`_skeleton_roof_general`)** handles all cases that tier 1 rejects — unstable single-contour polygons, multi-contour shapes, and polygons with holes (e.g. glyphs like "a", "g", "o"). It builds the *full* straight-skeleton graph via `shapely_polyskel.skeletonize()` (the `shapely-polyskel` package, pulling in `euclid3` and `shapely`) and traces faces directly. The implementation proceeds in three stages:

  1. **Component grouping** (`_skeleton_roof_general`): `cs.to_polygons()` contours are split by signed area into outer polygons (CCW, area > 0) and holes (CW, area < 0). Each hole is assigned to the smallest outer polygon that contains its centroid (using shapely `Polygon.contains`). This groups e.g. the counter of "a" with the outer outline of "a", and handles letters with no holes (like "W") as a single-contour component.

  2. **Skeleton graph construction** (`_build_skeleton_graph_with_holes`): For each component, `skeletonize()` is called with the outer polygon as CW-in-math (`p0[::-1]`) and holes as CCW-in-math (`hole[::-1]`), matching polyskel's y-down screen convention — run in a daemon thread with a 2s timeout, retried once with a tiny deterministic jitter (`< tol` so `key()` still snaps jittered sinks back to the canonical boundary vertex) if polyskel hangs on a degenerate axis-aligned configuration. The returned `Subtree(source, height, sinks)` list is merged with the polygon boundary edges into one planar adjacency graph, deduping nodes by position (tolerance = bbox_span × 1e-6). Self-loop sinks (sink == source) are skipped. Collinear sinks of the same subtree — where polyskel places two sinks on the same ray from the source, making the intermediate vertex a "shortcut" of the source — are chained instead of both being added as direct edges, and a second pass repeats this for same-angle neighbour pairs that arise *across* different subtrees sharing a source; both cases would otherwise give `_trace_face`'s angle-sorted lookup two neighbours at the same angle, causing an arbitrary and wrong turn choice. **Degenerate holes**: polyskel sometimes generates no subtree touching a hole at all (observed for small/symmetric holes, e.g. a Bold "A"'s triangular counter) — detected as a hole whose boundary vertices have no interior (height > 0) neighbour. For each such hole, `skeletonize()` is re-run on it *alone* (its own boundary treated as a mini outer polygon) to get a valid interior apex, which is injected into the shared graph. Because that apex was computed treating the hole as its own outer shape, it sits on the opposite side from a normally-connected hole's skeleton, so `_trace_face` must walk that hole's boundary in reversed order (like an outer boundary) to find the correct face, then reverse the found face's point order again before triangulating to get the winding a manifold mesh requires (`degenerate_holes`, returned alongside the graph, records which holes need this treatment).

  3. **Mesh construction** (`_skeleton_roof_component` / `_build_roof_mesh`): The floor is tessellated via `shapely.constrained_delaunay_triangles` (constrained so that every polygon boundary edge is a triangulation edge, preventing Shapely's unconstrained Delaunay from treating polygon boundary edges as interior diagonal edges — which would produce a non-manifold mesh), then filtered by `shape.contains(centroid)` to exclude triangles inside holes; each triangle's signed area is checked and the winding reversed to CW if positive (ensuring a downward −z normal). Each traced face is triangulated by `_triangulate_planar_face`, which estimates the face's normal via Newell's method (a sum over all vertex pairs — numerically stable when the leading vertices happen to be near-collinear, unlike a 3-point cross product) before projecting onto the face's own 2D basis and running `_ear_clip` (this convention yields outward-facing triangles with no winding reversal); a face is rejected as non-planar if any point deviates from the fitted plane by more than 0.2% of the face's bounding-box span. All vertices are welded by position before `m3d.Mesh`/`m3d.Manifold` construction. Per-component manifolds are union-ed together.

     Roof faces are traced one per boundary edge, but a thin stroke can let one facet's ridge touch a *different* facet's territory directly (a straight-skeleton split/collision event — e.g. where a letter's stroke narrows enough that the outer boundary's ridge reaches the hole boundary, or two non-adjacent stretches of the same ring, or even unrelated ridge segments, end up merged into one non-planar loop by a plain angle-sort walk). No single fix handles every such pattern, so `_build_roof_mesh` is tried with three tracing strategies in order, keeping whichever first produces a valid manifold:
     - `"owned"` (`_assign_edge_ownership` + `_trace_owned_face`): computes, from straight-skeleton geometry alone (a vertex's height must equal its perpendicular distance to the edge(s) it belongs to — edges are first grouped via union-find wherever a shared boundary vertex has no ridge *and* its two edges are collinear, so a flat/gently-curved run is treated as one facet), which facet every vertex belongs to, and refuses to trace across into a different facet's territory, closing back to its own start instead. Most precise, but when a ridge point is *exactly* equidistant from several edges at once (a genuine junction, common along repeatedly-pinched strokes), independent traces can each close via a "virtual chord" with no guarantee another trace produces the matching opposite chord — leaving the mesh open.
     - `"split"` (`_split_pinched_face`): traces without any ownership constraint, then decomposes the resulting loop after the fact — the maximal run of boundary points containing the trace's own start is the "home" run; any other boundary run found elsewhere in the loop (even one sharing the home run's ring) is excised into its own small facet, using its two flanking skeleton points to close both the excised piece and the gap left in the main facet. A coarser, single-pass heuristic, but doesn't have the "owned" strategy's open-mesh failure mode.
     - `"plain"`: the original unconstrained `_trace_face` walk, no cross-facet handling — correct whenever a component has no such pinch to worry about.

  **Tier 3 (`_roof_sdf_fallback`)** is the signed-distance-field fallback, used only when tier 2 fails — a `skeletonize()` call returning no subtrees, a face trace that doesn't close, a non-planar/degenerate face, ear-clipping failure, or `Manifold` construction not reporting `Error.NoError`. For each `(x, y)` inside the union of the 2D children's polygons, `height(x, y) = Euclidean distance from (x, y) to the nearest point on any polygon edge/vertex`, and the solid is `{(x, y, z) : 0 ≤ z ≤ height(x, y)}`, built via `Manifold.level_set()` (marching tetrahedra) at grid spacing `max(width, height, z_max) / 10` and `simplify()`-ed afterward; this yields ~3–10% volume error vs. the analytic shape.

  Both `method="voronoi"` and `method="straight"` route through this same tier-1/tier-2/tier-3 logic and are therefore equivalent in BelfrySCAD — when tier 1 or 2 applies, both produce the true straight skeleton (verified against `--enable=roof` STL output for a square → pyramid and an L-shaped polygon → sharp reflex-corner ridge). An unrecognized `method` value emits `WARNING: Unknown roof method '...'. Using 'voronoi'.` and falls back to `"voronoi"`. `convexity` is accepted and ignored (it's preview-only in real OpenSCAD too).
- **`object()`** (an experimental builtin in OpenSCAD dev snapshots, behind `--enable=object-function`) creates an `OscObject` — an ordered string-keyed map, echoed/`str()`-formatted as `{ a = 1; b = "hello"; }` (empty: `{ }`). Members are read via both `o.field` (`PrimaryMember`) and `o["field"]` (`PrimaryIndex` with a string index); a missing key returns `undef` with no warning, and numeric indexing (`o[0]`) is always `undef`. **`==`/`!=`** are deep AND *order-sensitive*: `object(a=1,b=2) == object(b=2,a=1)` is `false` (verified against the OpenSCAD-dev CLI), implemented in `_osc_equal()` by comparing `items()` pairwise in insertion order. `for (k = obj)` (and list comprehensions / `intersection_for`) iterate over the object's **keys as strings**, in insertion order. Function-valued members are callable (`f.fn(5)`) via the existing function-literal-value call path — no special-casing needed. **Construction/merge**: each *positional* argument must be another `OscObject` (whose entries are merged in first, in their order) or a list of `[key, value]` pairs (set in list order); any other positional argument type emits `WARNING: object(Argument N <type>) An unnamed argument must be either <object> or <list>, it is <type>.` and the whole call is `undef`. *Named* arguments set/override entries in call order (duplicate named keys: last value wins, at the first-seen position — `_resolve_args`' dict already does this). `+`/`-`/`<`/`>` between objects are undefined operations → `undef` (handled by the existing `_vec_add`/`_vec_sub` `TypeError`→`None` fallback and `_osc_comparable`). **Known gaps**: the `[key]`-only "delete entry" form in a positional list argument (an obscure real-OpenSCAD feature) isn't implemented — such entries hit the generic malformed-entry warning instead; and malformed nested list-of-pairs arguments don't replicate real OpenSCAD's exact `[Element N <type>] Entry type is not a list...` message text (a single generic warning is emitted instead).
- **`textmetrics()`/`fontmetrics()`** (also behind `--enable=object-function`, returning `OscObject`s) resolve `font=` the same way `text()` does — via `_resolve_font()` (`fc-match`, see the `text()` entry below) — so measurements genuinely reflect the requested font, not just the bundled default. Falls back to the bundled font — `src/belfryscad/resources/fonts/LiberationSans-Regular.ttf` (the same Liberation Sans 2.00.1 that OpenSCAD itself bundles as its default, OFL-1.1 licensed) — if `font=` is unset, `fc-match` is unavailable, or the font can't be found; read via `fontTools` (`_resolve_font()`/`_measure_text()`). `direction`/`language`/`script`/`$fn` are accepted but have no effect. **Algorithm** (derived empirically against `OpenSCAD-dev --enable=all` for the bundled-font case, matching real output to ~4 significant figures for `fontmetrics()` and to ~0.1-1% for `textmetrics()` — exact match isn't possible without replicating FreeType's hinting/grid-fitting): `scale = size * (100/72) / unitsPerEm`. For each character, look up its glyph via `cmap`; characters with no glyph (e.g. `'\n'`, confirmed absent from Liberation Sans' cmap) contribute zero advance and no bbox — this handles multi-line text without special-casing. Each glyph's ink bbox (`_glyph_bounds()` — `glyf.xMin/xMax/yMin/yMax` for TrueType, only present when `numberOfContours != 0` — e.g. space has none; traces the outline via a `BasePen` for CFF/OTF, since those store bounds nowhere directly) is positioned at the current pen offset (in scaled units); the pen then advances by `hmtx_advance * scale * spacing` — **`spacing` scales each glyph's own advance**, not just the total, which is what makes both `advance.x` and `size.x` come out right for `spacing != 1`. `ascent`/`descent` = max/min ink-bbox `top`/`bottom` over all glyphs (0/0 for empty text); `size = (max(ink_right) - min(ink_left), ascent - descent)`; `advance = (final_pen_x, 0)`. **Alignment**: `offset.x = -hx * advance.x` (`hx` = 0/0.5/1 for left/center/right), `offset.y` = `-ascent` (top) / `-(ascent+descent)/2` (center) / `0` (baseline) / `-descent` (bottom); `position = (offset.x + min(ink_left), offset.y + descent)`. `fontmetrics()`'s `nominal`/`max`/`interline` come straight from the resolved font's `hhea`/`head` tables scaled by the same `scale` (nominal/interline commonly match Liberation Sans exactly for metric-compatible fonts like Arial, by design; `max` — from actual glyph bbox extremes — genuinely differs per font); `font.family`/`font.style` report the *actually resolved* font's real name, read from its `name` table via `getBestFamilyName()`/`getBestSubFamilyName()` (e.g. `font="Times New Roman:style=Bold"` yields `family="Times New Roman"`, `style="Bold"`), not an echo of the request string. The `offset` formula is factored into the module-level `_text_align_offset(halign, valign, m)`, shared with `text()` below.
- **`text()`** renders `text` as a 2D `CrossSection` (`ColoredBody.section`), reusing `_measure_text()`'s per-glyph layout and `_text_align_offset()`'s alignment translation — so its position/bbox match `textmetrics()`'s `size`/`offset`/`position` for the same arguments. **Font resolution**: `font=` is an OpenSCAD/fontconfig pattern (e.g. `"Times New Roman:style=Bold"`); `_resolve_font()` calls `fc-match` to find the best-matching system font file and TTC index, loads it via `fontTools`, and caches the result per spec string (also used by `textmetrics()`/`fontmetrics()`, see above). Falls back to the bundled Liberation Sans if `fc-match` is unavailable or the font cannot be found. `direction`/`language`/`script` are accepted but unused. **Glyph rendering**: outlines come from `font.getGlyphSet()` drawn into a `_FlattenPen` (a `fontTools.pens.basePen.BasePen` subclass) that flattens both quadratic Bezier curves (TrueType `glyf` glyphs, `_qCurveToOne`) and cubic Bezier curves (CFF/OTF glyphs, `_curveToOne`) into `segs = max(2, $fn // 2)` line segments per curve (default `segs=2`, since default `$fn=0`). Contours are cached per `(font_path, ttc_index, glyph_name, segs)` in `_glyph_contour_cache`. Bounding box for layout uses `_glyph_bounds()` (see the `textmetrics` entry above). Per-glyph cross-sections are scaled by `scale` and translated to their pen position, unioned via `m3d.CrossSection.batch_boolean(.., OpType.Add)`, then translated by `_text_align_offset()`'s `(offset_x, offset_y)`. Multi-line text (`\n`) is not supported — matches real OpenSCAD, falls out for free since `'\n'` has no glyph in `cmap`. Empty `text` returns an empty `CrossSection`.

## Manifold API: Geometry Provenance

Manifold tracks provenance through CSG ops via the `Mesh` output (Python bindings use `m3d.Mesh`, not `MeshGL`). Key fields after any boolean op:

| Field | Meaning |
|---|---|
| `run_original_id` | Array of source mesh IDs, one per triangle run |
| `run_index` | Boundaries of runs in the triangle array |
| `face_id` | Which source triangle each output triangle derives from |

Each Manifold body built from scratch gets a unique auto-incremented `originalID`. After a boolean (e.g. `body1 - body2`), output triangles form **runs** tagged with the `originalID` of their contributing input body.

### AST ↔ Geometry ID Mapping Pattern

Manifold has no concept of AST nodes — the application maintains the mapping:

1. Assign one `originalID` per geometry-producing AST node (via `ReserveIDs`)
2. After each CSG op, walk `run_original_id` to recover which output triangles belong to which AST node
3. Store a lookup table: `originalID → AST node`

This is how the WYSIWYG pick loop resolves a ray-cast hit to an editable AST parameter:
```
ray cast → hit triangle index → run_original_id lookup → originalID → AST node → source span
```

### Python API (manifold3d)

```python
import manifold3d as m3d

body = m3d.Manifold.cube()          # primitives auto-get an originalID
result = body1 - body2              # CSG ops preserve provenance

mesh = result.to_mesh()             # Mesh output (not MeshGL)
mesh.run_original_id                # numpy array: source ID per run
mesh.run_index                      # numpy array: run boundaries

# 2D
cs = m3d.CrossSection.circle(r, segs)   # 2D primitive
cs2 = cs1 + cs2                         # union; - = difference; ^ = intersection
cs.offset(delta, m3d.JoinType.Round)    # morphological offset
body = m3d.Manifold.extrude(cs, height) # 2D → 3D
body = cs.revolve(segs, angle)          # revolve around Y axis (→ Z in output)
cs = body.project()                     # 3D → 2D outline
cs = body.slice(z)                      # cross-section at height z
```
