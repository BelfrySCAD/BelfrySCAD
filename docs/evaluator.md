# AST Evaluator Reference

The evaluator sits between openscad_parser and Manifold: a recursive AST walker producing Manifold geometry from a parsed AST.

## Scope processing

Call `build_scopes()` immediately after parsing to annotate every node with `.scope`. Three independent namespaces — variables, functions, modules — with automatic parent-chain lookup:

```python
scope.lookup_variable(name)  # returns the Assignment/ParameterDeclaration node
scope.lookup_function(name)  # returns the FunctionDeclaration node
scope.lookup_module(name)    # returns the ModuleDeclaration node or None (built-in)
```

Declarations are hoisted within their block (forward references work). Last-wins scoping is implemented by the library — later assignments in the same scope overwrite earlier ones.

## Architecture

Recursive AST walker with a built-ins dispatch table:

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

`EvalContext` has two context-creation methods with different inheritance rules:

| Method | `__let_*` inherited | Use for |
|---|---|---|
| `child_ctx()` | Yes (full copy) | `for`/`let` iterations, `_eval_let_block`, list comprehension scopes — outer bindings must stay visible |
| `call_ctx()` | No (only `$*` dynamic vars) | Module/function calls — callee has its own variable scope; inheriting caller `__let_*` would trigger spurious double-assignment warnings |

`_call_ctx_for(decl, ctx, ...)` picks between the two for a module/function call: it walks `_call_stack` and uses `child_ctx()` (inherit `__let_*`) if `decl`'s source span is *strictly contained* within an already-active frame's declaration span — i.e. `decl` is a module/function declared lexically inside the body of a module/function currently being evaluated (a closure over that call's locals), otherwise `call_ctx()` (isolated). Direct recursion (a declaration's span containing itself) is excluded from "nested" so a recursive call doesn't inherit its own in-progress locals as if they were its caller's. This is what lets BOSL2's `cuboid()` — which reassigns its `edges` parameter (`edges = _edges(edges, ...)`) and then calls a nested `module corner_shape() { ... }` referencing `edges` — see the *reassigned* value instead of recursing forever back into `scope.lookup_variable("edges")` → the same reassignment's own RHS.

## Built-ins implemented

**3D Primitives** (→ `ColoredBody.body`): `cube`, `sphere`, `cylinder`, `polyhedron`

**2D Primitives** (→ `ColoredBody.section`): `circle`, `square`, `polygon`

**Extrusion** (2D → 3D): `linear_extrude`, `rotate_extrude`, `roof`

**Transforms** (3D and 2D): `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `resize`, `color`, `offset`

**Booleans** (3D or 2D, dispatched by child type): `union`, `difference`, `intersection`

**Topology**: `hull`, `minkowski`, `projection`

**Control / utility**: `for`, `intersection_for`, `let`, `if`/`else`, `echo`, `assert` (modular + expression forms), `render`, `children()`, `breakpoint()`

**Data**: `object`, `is_object`

`breakpoint()` — pauses the debugger at the call site. Optional first positional/keyword `condition`: skipped if falsy. No-op outside the debugger. Implemented via `_check_debug(node, ctx, forced=True)`, which passes `forced=True` to the debug hook to bypass the normal step/breakpoint-line check.

**Math functions**: `abs`, `sign`, `ceil`, `floor`, `round`, `sqrt`, `ln`, `log`, `exp`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `min`, `max`, `pow`, `norm`, `cross`, `rands`, `lookup`

**String / list functions**: `str`, `chr`, `ord`, `concat`, `len`, `search`

**Type checks**: `is_undef`, `is_bool`, `is_num`, `is_string`, `is_list`, `is_function`

Note: `is_range`, `is_nan`, and `is_finite` are **not** real OpenSCAD builtins despite the `is_*` naming convention — they're ordinary functions defined by BOSL2 (`utility.scad`). Calling them without BOSL2's `std.scad` included emits `WARNING: Ignoring unknown function '...'` and evaluates to `undef`, matching real OpenSCAD. Do not add them to `math_fns` — doing so would shadow BOSL2's own definitions.

`is_function(x)` is `isinstance(x, (FunctionDeclaration, FunctionLiteral))`. In practice only `FunctionLiteral` values (`g = function(x) ...`) ever reach it as a value — a `FunctionDeclaration` (`function f(x) = ...`) is never returned by identifier lookup (see Architecture #2), so `is_function(f)` for a named function `f` is `false`, matching real OpenSCAD.

`is_num(x)` is `false` for `nan` (`is_num(0/0)` → `false`), even though `nan` is a Python `float` — matching real OpenSCAD's quirk that `nan` fails `is_num()` while `inf`/`-inf` pass. `math_fns["is_num"]` explicitly excludes `math.isnan(x)`.

**Constants**: `PI`

**Other**: `version`, `version_num`, `parent_module` (stub)

**`surface(file, center=false, invert=false)`**: loads a heightmap from a `.dat` text file or PNG and builds a closed solid mesh. `.dat`: whitespace-separated number matrix; `#`-prefixed and blank lines ignored; first row = highest Y (OpenSCAD convention). PNG: linear luminance `Y = 0.2126R + 0.7152G + 0.0722B` scaled to 0–100; `invert=true` flips the mapping. `center=true` centers on X/Y; bottom face always at z=0. Requires Pillow for images.

**Not yet implemented**: `text`, `import` (warn and return None)

**Special variables**: `$fn`, `$fa`, `$fs` control mesh resolution. `$children` = the number of module-instantiation child *statements* in the `{}` block passed to this module call (`len(call.children)`, excluding `Assignment`/`ModuleDeclaration`/`FunctionDeclaration`), not the number of geometries they produce — e.g. `children()` counts as one child even when it forwards zero bodies, and `if (false) sphere();` still counts as one child. `$`-prefixed named args in any call (e.g. `sphere(r=2, $fn=64)`) merge into the dynamic context for that call and its children.

**Viewport special variables**: `$vpt` (= `camera.target` as `[x,y,z]`), `$vpr` (= `[((90-altitude)%360+360)%360, 0, ((azimuth-270)%360+360)%360]`), `$vpd` (= `camera.distance`) are injected into the root `EvalContext.dyn` at render/debug start, snapshotted in the main thread via `MainWindow._viewport_params(tab)` before the worker thread launches. `Evaluator.evaluate()` accepts `viewport_params: dict | None` and merges it into `ctx.dyn` before processing.

## originalID assignment

Each geometry-producing node (primitives and their transform/boolean ancestors) gets a unique Manifold `originalID` via `ReserveIDs`. The evaluator builds and returns the `originalID → AST node` lookup table alongside the mesh.

## 2D geometry

`ColoredBody` carries either a 3D `body: Manifold` or a 2D `section: CrossSection` (not both). 2D primitives (`circle`, `square`, `polygon`) return only `section`. `linear_extrude`/`rotate_extrude` consume 2D children via `_to_cross_section()` (unions all child sections) and return a 3D body. Booleans dispatch on whether children carry 3D bodies or 2D sections; `_combine()` handles mixed children — uses 3D bodies if any present, else unions sections.

`manifold3d.CrossSection` supports full 2D CSG: `+` (union), `-` (difference), `^` (intersection), `offset`, `hull`, `batch_hull`, `revolve`, `extrude`, and all 2D transforms. `CrossSection.to_polygons()` returns contours for polygon construction.

`_builtin_transform` dispatches on child type: `_apply_transform_2d` handles `CrossSection` (via `cs.translate/rotate/scale/mirror`); `_apply_transform_3d` handles `Manifold`. `resize` and `multmatrix` are 3D-only — 2D children pass through unchanged. So `translate([4,0]) circle(r=1)` and similar 2D transform chains work, including as `hull()` inputs.

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

`_call_stack` entries: modules are 4-tuples `("module", name, call_pos, decl_pos)` (call site + declaration start); functions are 3-tuples `("function", name, call_pos)`. `error(msg, node=None, innermost_frame=None)` takes the failing node and an optional innermost frame label (e.g. `"assert"`) for the first TRACE line. If `error_break_fn` is set (debug mode), `error()` calls it before raising `EvalError`, pausing the debugger at the error site.

## Special variable scoping (`$variables`)

`$`-prefixed variables (`$fn`, `$fa`, `$fs`, `$t`, `$children`, etc.) use **dynamic scoping** — inherited down the **call chain**, not the lexical scope chain, unlike regular variables.

The evaluator maintains a separate dynamic binding context threaded through each module call. `$fn=32` on a module invocation propagates to all nested calls within it, regardless of lexical scope. `scope.lookup_variable()` must not be used for `$`-prefixed names.

## `include` vs `use`

Exact OpenSCAD semantics:
- `include <file.scad>` — brings all declarations and top-level geometry into the current scope
- `use <file.scad>` — brings only the used file's own functions and modules into scope; its top-level geometry and variable assignments are not injected and its variable namespace stays isolated from the using file's (in both directions)

`_resolve_use_scopes(nodes, current_file, log_fn)` in `main_window.py` implements `use`, called once from both the render-worker and debug-session paths. For each top-level `UseStatement` in `current_file`, it recursively resolves the used file's own `use` statements first, then:

- Injects only the used file's *own* `ModuleDeclaration`/`FunctionDeclaration` nodes (not ones it transitively pulled in via its own `use`) — "nested use has no effect on the base file's environment".
- Builds `current_file`'s combined `root_scope` from its own nodes plus the injected declarations, so `current_file` can call them by name.
- Re-anchors each injected declaration's `.scope` (and its body's scope tree) back to the used file's own root scope — built from the used file's own nodes plus anything *it* injected via nested `use`. This lets the injected modules/functions resolve the used file's own globals (and any nested-`use` declarations) without exposing them to `current_file`, and vice versa.

Re-anchoring works because `ModuleDeclaration.build_scope`/`FunctionDeclaration.build_scope` are idempotent: calling `.build_scope(scope)` a second time just creates a fresh child scope and reassigns `.scope` on the node and its descendants, overwriting the (incorrect) scope assigned by `current_file`'s combined `build_scopes()` call.

## Implementation quirks

- `UseStatement.filepath` is a `StringLiteral` AST node, not a plain string — use `.filepath.val`.
- "file not found" errors from library resolution (e.g. internal BOSL2 files already handled by the parser) are suppressed in the console.
- `sys.setrecursionlimit(10000)` is set in `main()` for BOSL2 compatibility. `RecursionError` around `build_scopes()`/`evaluate()` is treated as a runtime error (shows last-valid geometry).
- **Ranges** are an `OscRange(start, step, end)` object, not an expanded list. `echo([1:3])` prints `[1 : 1 : 3]`. Expanded to a list only when iterated (`for`, list comprehensions, `intersection_for`). A zero-step range echoes as `[1 : 0 : 5]` and iterates to nothing. **Indexing** a range with `[0]`/`[1]`/`[2]` returns its `start`/`step`/`end` components (not iterated values) — e.g. `[2:3:11][0]` → `2`, `[1]` → `3`, `[2]` → `11`, matching real OpenSCAD. This is what BOSL2's `is_finite()`/`is_range()` inspect to detect range values.
- **C-style `for` in list comprehensions** — `[for (a=v[0], i=1; i<=len(v); a = cond?a+v[i]:a, i=i+1) a]` — parses as a `ListCompCFor` node (`inits`, `condition`, `incrs`, `body`), distinct from the assignment-style `ListCompFor`. `_eval_listcomp_cfor()` binds `inits` once into a child context, then loops while `condition` is true, evaluating `body` (via `_eval_list_comp_body`) and then `incrs` *sequentially* (each `incrs` assignment sees the previous ones' new values, matching source order) each iteration. Capped at `_MAX_CFOR_ITERATIONS` (1,000,000) to avoid hangs on a malformed `incrs`/`condition`. Used by BOSL2's `cumsum()`, `product()`, etc.
- **Boolean arithmetic** returns `undef` (`None`): `true + 1` → `undef`. The evaluator checks `isinstance(a, bool) or isinstance(b, bool)` before any arithmetic op.
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
- **`parent_module()`** returns `undef` at the top level (not `""`).
- **`lookup()`** on an empty table (`lookup(5, [])`) returns `undef`, not `0`.
- `search()` match modes depend on the first argument's type:
  - **String**: character array, each character searched independently. `num_returns=1` (default) drops not-found characters; `num_returns=0` includes them as `[]`. Only valid when the vector is also a string.
  - **List**: each element is searched for independently. If an element is itself a list/vector, it's compared via **direct equality** against each whole `vector[i]` entry (`index_col` is ignored) — correct idiom for finding a string in a list of strings (`search(["foo"], ["foo","bar","baz"])` → `[0]`) and for BOSL2's `in_list(v, [UP,RIGHT,BACK])`. If an element is a scalar, it's compared against `vector[i][index_col]` (or `vector[i]` if not a list).
  - **Scalar**: returns up to `num_returns` matching indices (`[]` if none); `num_returns=0` returns all matches.
- **Assert message format**: `to_openscad([cond_expr]).strip()` recovers the condition source text for `Assertion 'expr' failed` (requires `from openscad_parser.ast import to_openscad`).
- **String literals with leading/trailing whitespace**: arpeggio's `skipws=True` would strip whitespace before sub-rules in `(DQUOTE, contents, DQUOTE)`, eating leading spaces (`"  bar"` → `"bar"`). Fixed in openscad_parser 2.5.1 by collapsing `string_literal` into one regex terminal `"(?:[^"\\]|\\.|\\$)*"`, avoiding whitespace skipping inside quotes.
- **`chr()`** accepts either a single code point (`chr(65)` → `"A"`) or a vector of code points (`chr([65,66,67])` → `"ABC"`), converting and concatenating each element; `chr([])` → `""`. Floats are truncated via `int()` (`chr(65.7)` / each element of a vector → `"A"`).
- **`+`/`-` involving strings**: OpenSCAD has no `+`/`-` operator for strings (unlike Python's `str.__add__`). `"ab" + "cd"` → `undef`, not Python-style concatenation `"abcd"`. `_vec_add()`/`_vec_sub()` check for `str` operands before falling back to Python's `+`/`-`.
- **Number formatting (`echo()`/`str()`)**: `_format_number()` replicates OpenSCAD's number-to-string conversion, which differs from Python's `f"{v:g}"`:
  - At most 6 significant digits.
  - Fixed-point notation is used for exponents in `[-5, 5]` (one wider than `%g`'s `[-4, 5]`): `0.00001` → `"0.00001"`, where `%g` would give `"1e-05"`.
  - Scientific notation drops the exponent's leading zero: `1000000` → `"1e+6"` (not `"1e+06"`), `1.23456789e-7` → `"1.23457e-7"` (not `"1.23457e-07"`).
  - `-0.0` → `"0"`. `nan`/`inf`/`-inf` are lowercase.
- **`roof()`** is implemented via a signed-distance-field (SDF) and `Manifold.level_set()` (marching tetrahedra), not a true CGAL straight-skeleton. For each `(x, y)` inside the union of the 2D children's polygons, `height(x, y) = Euclidean distance from (x, y) to the nearest point on any polygon edge/vertex`, and the solid is `{(x, y, z) : 0 <= z <= height(x, y)}`. This matches real OpenSCAD's default `method="voronoi"` (verified against `--enable=roof` STL output for a square → pyramid and an L-shaped polygon → rounded reflex-corner cone). `method="straight"` is currently treated identically to `"voronoi"` — true straight-skeleton sharp ridges at concave/reflex corners are not implemented (for convex inputs both methods are identical in real OpenSCAD too, so this only diverges for concave polygons + `method="straight"`). An unrecognized `method` value emits `WARNING: Unknown roof method '...'. Using 'voronoi'.` and falls back to `"voronoi"`. `convexity` is accepted and ignored (it's preview-only in real OpenSCAD too). The marching-tetrahedra grid spacing is `max(width, height, z_max) / 10` (independent of `$fs`/`$fn`, since real OpenSCAD's roof height field is computed analytically, not from the input's facet count); this yields ~3-10% volume error vs. the analytic shape and keeps `roof()` well under the 200ms regeneration budget for typical models. The result is `simplify()`-ed to merge near-coplanar marching-tetrahedra facets back toward clean roof faces.
- **`object()`** (an experimental builtin in OpenSCAD dev snapshots, behind `--enable=object-function`) creates an `OscObject` — an ordered string-keyed map, echoed/`str()`-formatted as `{ a = 1; b = "hello"; }` (empty: `{ }`). Members are read via both `o.field` (`PrimaryMember`) and `o["field"]` (`PrimaryIndex` with a string index); a missing key returns `undef` with no warning, and numeric indexing (`o[0]`) is always `undef`. **`==`/`!=`** are deep AND *order-sensitive*: `object(a=1,b=2) == object(b=2,a=1)` is `false` (verified against the OpenSCAD-dev CLI), implemented in `_osc_equal()` by comparing `items()` pairwise in insertion order. `for (k = obj)` (and list comprehensions / `intersection_for`) iterate over the object's **keys as strings**, in insertion order. Function-valued members are callable (`f.fn(5)`) via the existing function-literal-value call path — no special-casing needed. **Construction/merge**: each *positional* argument must be another `OscObject` (whose entries are merged in first, in their order) or a list of `[key, value]` pairs (set in list order); any other positional argument type emits `WARNING: object(Argument N <type>) An unnamed argument must be either <object> or <list>, it is <type>.` and the whole call is `undef`. *Named* arguments set/override entries in call order (duplicate named keys: last value wins, at the first-seen position — `_resolve_args`' dict already does this). `+`/`-`/`<`/`>` between objects are undefined operations → `undef` (handled by the existing `_vec_add`/`_vec_sub` `TypeError`→`None` fallback and `_osc_comparable`). **Known gaps**: the `[key]`-only "delete entry" form in a positional list argument (an obscure real-OpenSCAD feature) isn't implemented — such entries hit the generic malformed-entry warning instead; and malformed nested list-of-pairs arguments don't replicate real OpenSCAD's exact `[Element N <type>] Entry type is not a list...` message text (a single generic warning is emitted instead).

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
