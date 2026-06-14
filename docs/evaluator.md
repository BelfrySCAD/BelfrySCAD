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
2. `Identifier` in an expression: `scope.lookup_variable(name)` then evaluate the bound value; if not found, fall back to `scope.lookup_function(name)` (lets named functions be passed as values, required for `is_function()`)
3. Function call: look up via `scope.lookup_function(name)`, evaluate args in caller's scope, body in new scope
4. Default parameter values are evaluated in the **caller's** scope, not the callee's

## Assignment execution order

Within each scope (top-level, module body, `if`/`for` block), all `Assignment` nodes evaluate **before** any geometry statements, matching OpenSCAD's last-wins semantics. E.g. `a = 5; cube(a); a = 10;` produces a 10×10×10 cube — both assignments run before `cube(a)`. Applies recursively at every level processed by `evaluate()` and `_eval_children()`.

Assignments are **eager**: `_eval_statement` evaluates an `Assignment`'s expression immediately, storing it in `ctx.dyn` as `__let_{name}`. `_eval_identifier` checks `ctx.dyn` first, so the cached value serves later references in the same scope. Forward references (used before assigned in source order) fall back to `scope.lookup_variable()` and lazy evaluation.

A variable assigned twice in the same scope: the second overwrites the first and emits:
```
WARNING: a was assigned on line 1 but was overwritten in file foo.scad, line 3
```
matching OpenSCAD's exact format. `EvalContext.dyn_positions` tracks each `__let_*` entry's source position for this.

`_eval_children` shares `ctx.dyn` (not a copy) across siblings so eager assignments are immediately visible to subsequent siblings.

`EvalContext` has two context-creation methods with different inheritance rules:

| Method | `__let_*` inherited | Use for |
|---|---|---|
| `child_ctx()` | Yes (full copy) | `for`/`let` iterations, `_eval_let_block`, list comprehension scopes — outer bindings must stay visible |
| `call_ctx()` | No (only `$*` dynamic vars) | Module/function calls — callee has its own variable scope; inheriting caller `__let_*` would trigger spurious double-assignment warnings |

## Built-ins implemented

**3D Primitives** (→ `ColoredBody.body`): `cube`, `sphere`, `cylinder`, `polyhedron`

**2D Primitives** (→ `ColoredBody.section`): `circle`, `square`, `polygon`

**Extrusion** (2D → 3D): `linear_extrude`, `rotate_extrude`

**Transforms** (3D and 2D): `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `resize`, `color`, `offset`

**Booleans** (3D or 2D, dispatched by child type): `union`, `difference`, `intersection`

**Topology**: `hull`, `minkowski`, `projection`

**Control / utility**: `for`, `intersection_for`, `let`, `if`/`else`, `echo`, `assert` (modular + expression forms), `render`, `children()`, `breakpoint()`

`breakpoint()` — pauses the debugger at the call site. Optional first positional/keyword `condition`: skipped if falsy. No-op outside the debugger. Implemented via `_check_debug(node, ctx, forced=True)`, which passes `forced=True` to the debug hook to bypass the normal step/breakpoint-line check.

**Math functions**: `abs`, `sign`, `ceil`, `floor`, `round`, `sqrt`, `ln`, `log`, `exp`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `min`, `max`, `pow`, `norm`, `cross`, `rands`, `lookup`

**String / list functions**: `str`, `chr`, `ord`, `concat`, `len`, `search`

**Type checks**: `is_undef`, `is_bool`, `is_num`, `is_string`, `is_list`, `is_function`

**Constants**: `PI`

**Other**: `version`, `version_num`, `parent_module` (stub)

**`surface(file, center=false, invert=false)`**: loads a heightmap from a `.dat` text file or PNG and builds a closed solid mesh. `.dat`: whitespace-separated number matrix; `#`-prefixed and blank lines ignored; first row = highest Y (OpenSCAD convention). PNG: linear luminance `Y = 0.2126R + 0.7152G + 0.0722B` scaled to 0–100; `invert=true` flips the mapping. `center=true` centers on X/Y; bottom face always at z=0. Requires Pillow for images.

**Not yet implemented**: `text`, `import` (warn and return None)

**Special variables**: `$fn`, `$fa`, `$fs` control mesh resolution. `$children` = child count when entering a user module body. `$`-prefixed named args in any call (e.g. `sphere(r=2, $fn=64)`) merge into the dynamic context for that call and its children.

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
- **Ranges** are an `OscRange(start, step, end)` object, not an expanded list. `echo([1:3])` prints `[1 : 1 : 3]`. Expanded to a list only when iterated (`for`, list comprehensions, `intersection_for`) or indexed with `[i]`. A zero-step range echoes as `[1 : 0 : 5]` and iterates to nothing.
- **Boolean arithmetic** returns `undef` (`None`): `true + 1` → `undef`. The evaluator checks `isinstance(a, bool) or isinstance(b, bool)` before any arithmetic op.
- **Scalar × matrix/vector** multiplication recurses into nested lists (`_scale()`): `2 * [[1,2],[3,4]]` → `[[2,4],[6,8]]`, not just flat vectors.
- **Division by zero** returns IEEE 754 values: `1/0` → `inf`, `-1/0` → `-inf`, `0/0` → `nan`. Math domain errors follow suit: `sqrt(-1)` → `nan`, `ln(0)` → `-inf`, `asin(2)` → `nan`.
- **Negative string/list indexing** returns `undef`, not Python wraparound. `"hello"[-1]` → `undef`. `PrimaryIndex` rejects any `i < 0`.
- **Named args to built-in math functions** map to positional order as fallback (e.g. `abs(x=-3)` → `3`): positional args tried first, then named args in declaration order.
- **`parent_module()`** returns `undef` at the top level (not `""`).
- `search()` match modes depend on the first argument's type:
  - **String**: character array, each character searched independently. `num_returns=1` (default) drops not-found characters; `num_returns=0` includes them as `[]`. Only valid when the vector is also a string.
  - **List**: direct equality against each vector entry (or `vector[i][index_col]`) — correct idiom for finding a string in a list of strings: `search(["foo"], ["foo","bar","baz"])` → `[0]`.
  - **Scalar**: returns up to `num_returns` matching indices (`[]` if none); `num_returns=0` returns all matches.
- **Assert message format**: `to_openscad([cond_expr]).strip()` recovers the condition source text for `Assertion 'expr' failed` (requires `from openscad_parser.ast import to_openscad`).
- **String literals with leading/trailing whitespace**: arpeggio's `skipws=True` would strip whitespace before sub-rules in `(DQUOTE, contents, DQUOTE)`, eating leading spaces (`"  bar"` → `"bar"`). Fixed in openscad_parser 2.5.1 by collapsing `string_literal` into one regex terminal `"(?:[^"\\]|\\.|\\$)*"`, avoiding whitespace skipping inside quotes.

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
