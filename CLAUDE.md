# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NeuSCAD is a hybrid procedural CAD application combining OpenSCAD-style script-based modeling with live WYSIWYG 3D interaction. Its defining feature is **bidirectional synchronization** between source code and 3D geometry — editing code or dragging geometry keeps both views in sync.

**Status**: In active development. Core pipeline, rendering, editor, and several WYSIWYG features are implemented. Full design in `PRD.md`.

## Technology Stack

- **UI Framework**: PySide6 (Qt)
- **Code Editor**: `QPlainTextEdit` + `QSyntaxHighlighter` (PySide6 built-ins; text layer only — not semantically aware)
- **Parser**: openscad_parser ≥2.5.1 (strict PEG-based, generates AST with file/line/col/span metadata; parses full OpenSCAD syntax but has no knowledge of built-in functions/modules — the evaluator implements all built-ins). Fetched from PyPI.
- **CSG Kernel**: Manifold (union, difference, intersection, boolean ops)
- **Renderer**: ModernGL (GPU mesh rendering, camera controls)
- **Language**: Python

## Core Architecture

The pipeline flows strictly one direction during normal operation:

```
Source Code → QScintilla Editor → openscad_parser (AST) → Evaluator → Manifold (CSG/mesh) → ModernGL → PySide6 UI
```

**The AST is the single source of truth** — not the rendered geometry, not the editor text.

### Error Display

Parse errors get a squiggly underline at the error location via `QTextCharFormat` with `SpellCheckUnderline` style, applied as an extra selection on the `QPlainTextEdit`. Also reported in the console.

### Find / Replace

`CodeEditor.show_find(replace=False)` opens a `FindBar` overlay parented to the editor, top-right corner. `show_find(replace=True)` also shows the replace row. Triggered by Cmd+F / Cmd+H.

`FindBar` features:
- Plain-text and regex search (`.*` toggle), case-sensitive toggle (`Aa`)
- All matches highlighted pale yellow; current match orange with white text
- Match count label ("N of M"); prev/next navigation (◀ ▶ or Shift+Enter / Enter)
- Replace one (current match) and Replace All (works backwards through matches to preserve positions, wrapped in one `beginEditBlock`/`endEditBlock` for one undo step)
- A single-word selection present when Find opens pre-populates the search field
- Escape closes the bar and returns focus to the editor
- `_find_selections` is a separate extra-selection list on `CodeEditor`, inserted between `_selection_extra` and `_exec_selection` in `_refresh_extra_selections`
- Document changes while open auto-rerun the search via `document().contentsChanged`

### Indent Guides

Faint vertical lines drawn inside each indented line's leading whitespace, every `_indent_size` columns, except at the column of the first non-whitespace character. Implemented as `_IndentGuides(QWidget)`, a transparent overlay on `CodeEditor.viewport()`, created before `_ColumnGuide` so the column guide renders on top. Repainted on `document().contentsChanged` and `set_indent_size()`.

Paint logic: for each visible block, count leading spaces `n`; draw guides at `indent_size, 2*indent_size, …` while `col < n` (strictly less, so the column at `n` is never drawn). Empty/unindented lines skipped. Uses `QFontMetricsF` for sub-pixel accuracy.

### Column Guide

A faint vertical line at column 80, implemented as `_ColumnGuide(QWidget)`, a transparent overlay on `CodeEditor.viewport()`:
- `WA_TransparentForMouseEvents` + `WA_TranslucentBackground` so only the line pixel shows and mouse events pass through
- `update_geometry()` keeps the overlay sized to the full viewport rect; called from `CodeEditor.resizeEvent()`
- x position = `cursorRect(cursor_at_pos_0).x() + QFontMetricsF(font).horizontalAdvance('0' * 80)`. `QFontMetricsF` (not `QFontMetrics`) is required — the integer version rounds character width up by ~0.2px, accumulating to ~2 columns of error over 80 characters.

### Code Folding

Fold markers (▼ unfolded, ▶ folded) appear in the right section of the line-number gutter; clicking calls `toggle_fold(block_number)`.

`_compute_fold_regions(doc)` returns `{open_block: close_block}` via two passes:
1. **Delimiter matching** — `{…}`, `(…)`, `[…]` pairs; a region only forms when opener and closer are on different lines
2. **Indentation continuation** — any non-empty line followed by at least one more-indented non-empty line; covers function bodies, ternary chains, nested list comprehensions, etc.; `setdefault` lets pass-1 delimiter regions take precedence

`_fold_regions` recomputes lazily on first paint after `_fold_dirty` is set by `_on_doc_changed`. `_fold_busy` guards against re-entrant recomputation and against `_on_doc_changed` resetting `_fold_dirty` mid-toggle.

`_set_range_visible(start_bn, end_bn, visible)` sets `QTextBlock.setVisible()` on each hidden block, then a no-op `cursor.beginEditBlock(); cursor.endEditBlock()` forces `QPlainTextDocumentLayout` to recalc block heights (required for visibility changes to take effect).

Fold indicators are drawn with `painter.drawPolygon(QPoint[])` — `QPainterPath.drawPath` was invisible at small sizes on macOS; `drawPolygon` is reliable.

### Go to Definition

Right-click an identifier shows "Go to Definition of 'name'", only for words matching `[A-Za-z_][A-Za-z0-9_]*`.

`CodeEditor.go_to_definition_requested` (emits the word) connects to `MainWindow._go_to_definition(tab, word)` per tab.

`_go_to_definition` requires a cached `root_scope` on the tab (set after every successful `build_scopes()`, both in the render worker and `_start_debug()`); if absent, logs a message asking the user to render first.

Lookup order: `scope.lookup_variable(word)` → `scope.lookup_function(word)` → `scope.lookup_module(word)`, first non-None wins. Built-in modules return `None` from `lookup_module` and are skipped.

The definition node's `.position.origin` gives the source file path, `.position.line` the 1-indexed line. Navigation:
- Same file (or origin `None` / untitled tab): scroll current editor to the line
- Different file: switch to a matching open tab by `file_path`, or open via `_create_and_add_tab()` (view-only, no render)

`_create_and_add_tab(path, text) -> DocumentTab` creates a fully-connected tab (viewport/debugger-pane signals, perspective, Go-to-Definition) and adds it to the UI. Used by `_open_file`, `_open_recent`, `_go_to_definition`; not by `_new_document` (different setup path for blank tabs).

### Critical Constraint: Strict Parser

The parser produces **no partial AST** — it either succeeds fully or fails entirely. Handle the no-AST state gracefully:
- Cache the last valid AST
- Display last valid geometry while code is invalid
- Never block the UI or break the viewport

### Bidirectional Loop (future-critical, v1 groundwork required)

Dragging geometry in the viewport:
```
Drag event → ray cast → pick geometry ID → map ID to AST node (via span) → modify AST parameter → regenerate code + model
```

Requires every AST node to carry both its **source span** (file/line/col) and its **geometry ID(s)** from Manifold output. This mapping is the hardest design problem in the project.

## WYSIWYG Interaction Design

### Camera Controls

| Input | Action |
|---|---|
| Left-button drag | Orbit |
| Right-button drag | Pan |
| Scroll wheel | Zoom |
| Trackpad click+drag | Orbit |
| Trackpad two-finger scroll | Pan |
| Trackpad pinch | Zoom |

### Selection

Command-click triggers:
```
ray cast → hit triangle → run_original_id lookup → AST node → highlight source span in QScintilla + visual highlight in viewport
```

Command-click always lands on the leaf geometry node (innermost primitive). The selection can be walked up or down the AST hierarchy — up expands to a parent node (e.g. `cube()` → enclosing `translate()` → `difference()`), highlighting the entire subtree's geometry and the corresponding source span; down moves back toward the leaf.

When walking down from a node with multiple children, select the child whose geometry is closest to the original ray-cast hit point.

Multiple objects can be selected only as a complete subtree — walking up to a parent selects all its children as a unit. Arbitrary disjoint selections are not supported.

Selected objects are outlined (stencil buffer technique); fall back to mesh tinting if outline rendering proves too expensive.

Selecting a shape enables the transform toolbar (Translate, Rotate, Scale, and future tools).

### Transform Gizmos

When a tool is active, axis handles are drawn over the selected shape. Dragging a handle edits the AST directly:

| Tool | Handle | AST effect |
|---|---|---|
| Translate | Arrow per axis | Modify/insert `translate([x,y,z])` wrapper |
| Rotate | Arc per axis | Modify/insert `rotate(...)` wrapper |
| Scale | Handle per axis | Modify/insert `scale([x,y,z])` wrapper |
| Scale (Shift+drag) | Any axis handle | Scale all three components uniformly |

### How Tool Choice Resolves Edit Ambiguity

The active tool declares which transform type to edit — no intent inference needed. For each tool activation on a selected node:

1. Search the AST for an existing transform wrapper of the matching type immediately enclosing the selected node
2. If found: update its vector argument via a **targeted source span replacement** (not full code regeneration)
3. If not found: insert a new wrapper around the selected node's source span

### Value Overlay

During translate/rotate/scale, a text readout of the current value is shown in the viewport. The user can type an exact value instead of dragging; committing applies the same source rewrite rules as a drag commit.

Enter commits; Escape cancels and reverts to the pre-interaction state. The ghost mesh updates on commit (Enter), not while typing. The text field only gets focus on click — no auto-focus on drag-start.

Displayed value follows the source rewrite classification: absolute value for a literal number or a bare variable set to a number; delta for an expression.

### Transform Edit Rules

- **Nested transforms of the same type**: modify the innermost matching wrapper.
- **Transform composition order**: new wrappers are always inserted outside any existing transform wrappers on the selected node.
- **Live drag preview**: wireframe ghost copy of the mesh during drag; commit the AST edit and render on mouse-up.
- **Gizmo orientation**: handles drawn in local (post-transform) space.

### Source Rewrite Rules (Intent Preservation)

A drag commit rewrites the minimum source text based on the transform argument's form:

| Argument form | Rewrite strategy |
|---|---|
| Literal value (`[10, 0, 0]`) | Replace the affected component(s) in place; preserve named vs. positional style |
| Variable set to a literal (`x = 10`) | Update the literal at the variable's declaration site |
| Variable set to an expression (`x = base/2`) | Append a delta at the declaration site: `x = base/2 + 5` |
| Inline expression (`[base/2, 0, 0]`) | Append a delta inline: `[base/2 + 5, 0, 0]` |

Editing a variable declaration affects all sites referencing it — intentional, preserving the user's parametric relationships.

## AST Evaluator

The evaluator sits between openscad_parser and Manifold: a recursive AST walker producing Manifold geometry from a parsed AST.

### Scope processing

Call `build_scopes()` immediately after parsing to annotate every node with `.scope`. Three independent namespaces — variables, functions, modules — with automatic parent-chain lookup:

```python
scope.lookup_variable(name)  # returns the Assignment/ParameterDeclaration node
scope.lookup_function(name)  # returns the FunctionDeclaration node
scope.lookup_module(name)    # returns the ModuleDeclaration node or None (built-in)
```

Declarations are hoisted within their block (forward references work). Last-wins scoping is implemented by the library — later assignments in the same scope overwrite earlier ones.

### Architecture

Recursive AST walker with a built-ins dispatch table:

1. `ModularCall`: look up via `scope.lookup_module(name)` — `None` → dispatch to built-ins table; found → recursively evaluate the module body in a new child scope
2. `Identifier` in an expression: `scope.lookup_variable(name)` then evaluate the bound value; if not found, fall back to `scope.lookup_function(name)` (lets named functions be passed as values, required for `is_function()`)
3. Function call: look up via `scope.lookup_function(name)`, evaluate args in caller's scope, body in new scope
4. Default parameter values are evaluated in the **caller's** scope, not the callee's

### Assignment execution order

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

### Built-ins implemented

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

### originalID assignment

Each geometry-producing node (primitives and their transform/boolean ancestors) gets a unique Manifold `originalID` via `ReserveIDs`. The evaluator builds and returns the `originalID → AST node` lookup table alongside the mesh.

### 2D geometry

`ColoredBody` carries either a 3D `body: Manifold` or a 2D `section: CrossSection` (not both). 2D primitives (`circle`, `square`, `polygon`) return only `section`. `linear_extrude`/`rotate_extrude` consume 2D children via `_to_cross_section()` (unions all child sections) and return a 3D body. Booleans dispatch on whether children carry 3D bodies or 2D sections; `_combine()` handles mixed children — uses 3D bodies if any present, else unions sections.

`manifold3d.CrossSection` supports full 2D CSG: `+` (union), `-` (difference), `^` (intersection), `offset`, `hull`, `batch_hull`, `revolve`, `extrude`, and all 2D transforms. `CrossSection.to_polygons()` returns contours for polygon construction.

`_builtin_transform` dispatches on child type: `_apply_transform_2d` handles `CrossSection` (via `cs.translate/rotate/scale/mirror`); `_apply_transform_3d` handles `Manifold`. `resize` and `multmatrix` are 3D-only — 2D children pass through unchanged. So `translate([4,0]) circle(r=1)` and similar 2D transform chains work, including as `hull()` inputs.

### Color propagation

`color()` sets the current color in the evaluation context, cascading to all child geometry. The evaluator passes per-body color to the renderer alongside the mesh.

### Error handling

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

### Special variable scoping (`$variables`)

`$`-prefixed variables (`$fn`, `$fa`, `$fs`, `$t`, `$children`, etc.) use **dynamic scoping** — inherited down the **call chain**, not the lexical scope chain, unlike regular variables.

The evaluator maintains a separate dynamic binding context threaded through each module call. `$fn=32` on a module invocation propagates to all nested calls within it, regardless of lexical scope. `scope.lookup_variable()` must not be used for `$`-prefixed names.

### `include` vs `use`

Exact OpenSCAD semantics:
- `include <file.scad>` — brings all declarations and top-level geometry into the current scope
- `use <file.scad>` — brings only functions and modules (top-level geometry ignored)

### Implementation quirks

- `UseStatement.filepath` is a `StringLiteral` AST node, not a plain string — use `.filepath.val`.
- "file not found" errors from library resolution (e.g. internal BOSL2 files already handled by the parser) are suppressed in the console.
- `sys.setrecursionlimit(10000)` is set in `main()` for BOSL2 compatibility. `RecursionError` around `build_scopes()`/`evaluate()` is treated as a runtime error (shows last-valid geometry).
- **Ranges** are an `OscRange(start, step, end)` object, not an expanded list. `echo([1:3])` prints `[1 : 1 : 3]`. Expanded to a list only when iterated (`for`, list comprehensions, `intersection_for`) or indexed with `[i]`. A zero-step range echoes as `[1 : 0 : 5]` and iterates to nothing.
- **Boolean arithmetic** returns `undef` (`None`): `true + 1` → `undef`. The evaluator checks `isinstance(a, bool) or isinstance(b, bool)` before any arithmetic op.
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

## Color Support

`color()` affects viewport display, applied by the evaluator and passed to ModernGL. It cascades to all children in the subtree, per OpenSCAD's standard behavior.

## Key Design Requirements

- **Code ↔ Geometry mapping**: every geometry-producing AST node owns an `originalID`; the `originalID → AST node` table rebuilds on each render trigger.
- **Stability under invalid code**: UI must never crash or go blank.
- **Deterministic regeneration**: AST → geometry must be reproducible with no hidden rendering state. Full Manifold rebuild on every render trigger (incremental evaluation is a future optimization).
- **Performance**: <200ms model regeneration for small/medium models; 60 FPS viewport.

## File Format & Export

- **File format**: `.scad` (OpenSCAD-compatible plain text)
- **Language**: Full OpenSCAD language (variables, functions, modules, loops, conditionals, all built-in primitives and transforms)
- **Export**: STL, OBJ, 3MF; STEP under investigation (Manifold produces triangle meshes; STEP is B-rep, so any export would be a faceted solid of limited downstream value)
- **Export workflow**: if no current render exists, Export triggers a render first

## Render Triggers

No live preview. Full Manifold CSG processing runs when:

- The user selects **Render** (toolbar or Design menu)
- A file is **opened**
- A file is **saved**
- A **gizmo drag commits** (mouse-up)

The viewport always shows the last render's result; it stays static while the user edits code.

## Threaded Rendering

Parse + evaluate runs in a background `QThread`. Two helper classes in `main_window.py`:

- **`_RenderWorker(QObject)`** — moved to the worker thread via `moveToThread`; does the parse/evaluate work; emits `logged`, `parse_errored`, `finished`, `done`
- **`_RenderCallback(QObject)`** — stays in the main thread; `@Slot` methods receive worker signals; Qt auto-detects the cross-thread boundary (`QueuedConnection`), so callbacks run on the main thread

**Do not connect worker signals to Python lambdas** — lambdas have no thread affinity, so Qt can't determine which event loop to post to. Always route through a `QObject` slot with known thread affinity.

**Cancellation**: `_render()` passes a `threading.Event` to the worker, which checks `cancel.is_set()` between major steps. A `render_id` counter increments per render; the callback discards results whose `render_id` no longer matches.

**Progress indicator**: an indeterminate `QProgressBar` in the status bar shows while rendering, hidden on the worker's `done` signal. A `WaitCursor` override is set/restored at the same time.

## Debugger

The debugger runs the evaluator in a daemon worker thread (`DebugSession`) and surfaces a `DebuggerPane` with call-stack and variables panels.

### DebugSession (`debugger.py`)

Signals (emitted from the worker thread; Qt queues them to main):

| Signal | Args | When |
|---|---|---|
| `paused` | `line, all_frame_locals, call_stack` | Hit a breakpoint or step |
| `error_break` | `line, msg, all_frame_locals, call_stack` | Any runtime error |
| `finished` | `bodies, id_to_node` | Evaluation completed |
| `errored` | `str` | Unhandled exception after error_break resume |

`all_frame_locals` is a list of frame dicts, **innermost first**, with an extra `<toplevel>` entry appended when inside a call. `all_frame_locals[0]` matches row 0 (innermost) of the call-stack list. Each entry:

| Key | Contents |
|---|---|
| `"local_scope"` | Eagerly-assigned vars in the frame's `ctx.dyn`: `__let_*` (params, `for`/`let`, assignments so far) and `$*` specials |
| `"outer_scope"` | Global vars from `_root_ctx.dyn` (innermost frame only, when inside a call; parent frames get `{}`) |
| `"dyn_names"` | `set` of names from `dyn` — the only vars editable via the pane |

**Debug hook** — `_make_hook()` returns a closure passed to `Evaluator(debug_hook=...)`. Signature: `hook(line, locals_dict, call_stack, all_frame_locals) → (cmd, mods)`. `locals_dict` = dyn-bound locals (used for `mods`); `call_stack` = real call stack (used for step-depth math). The hook builds a **display** call stack with a `("toplevel", "<toplevel>", None)` entry appended before emitting `paused`. It pauses on breakpoints, step-into/over/out, and user-requested pauses, blocking on a `threading.Event`.

**Pause during execution** — `DebugSession.pause()` sets `_pause_requested`. The hook checks/consumes this flag at the top of every call, triggering an immediate pause regardless of breakpoints or step state — useful for interrupting a long-running evaluation.

**Error break** — `Evaluator(error_break_fn=self._error_break)` intercepts every `error()` call before raising `EvalError`. `_error_break` emits `error_break` and blocks until the user resumes; afterward `EvalError` propagates normally (caught by `_run`, triggers `errored`).

### Call stack display

Displayed **innermost-first** (current frame at row 0, `<toplevel>` at bottom). `_call_stack` in the evaluator is outermost-first; the display stack is `list(reversed(call_stack)) + [("toplevel", "<toplevel>", None)]`, built in both `_make_hook()` and `_error_break()`. `_populate_stack()` iterates it in order without reversing. `all_frame_locals[0]` always corresponds to row 0.

When inside a call, a `<toplevel>` frame (`local_scope` = global scope vars) is appended to `all_frame_locals`. Clicking `<toplevel>` → Locals shows the file's global declarations.

### Per-frame variable inspection

The evaluator maintains `_frame_ctxs` (an `EvalContext` list parallel to `_call_stack`), pushed/popped in `_eval_user_module`/`_eval_user_function`. At each `_check_debug`, `local_scope` reads directly from `ctx.dyn` (all `__let_*`/`$*` entries) — no scope walk needed since assignments are eager. When inside a call, `outer_scope` comes from `_root_ctx.dyn` (Globals view). A `<toplevel>` frame (`local_scope = outer_scope`) is appended when `_call_stack` is non-empty.

**Step Into for functions**: function bodies are expressions, so `_eval_statement`'s `_check_debug` never fires for them. `_eval_user_function` explicitly calls `self._check_debug(decl.expr, child_ctx)` after pushing the call frame, before `_eval_expr(decl.expr, child_ctx)` — giving Step Into a pause point at the start of every function body.

**Expression-level step points**: `_check_debug` accepts `expr_level=True` for sub-expression pauses. The debug hook only honours these for `step_into` (`_step_mode`) — break-on-first, gutter breakpoints, step-over, and step-out filter them out (`and not expr_level`). Nodes calling `_check_debug(…, expr_level=True)`:
- **`TernaryOp`** — before condition evaluation, then again at the chosen branch after resolution
- **`ModularIf` / `ModularIfElse`** — `_eval_statement` already pauses at the `if` node; a second `expr_level=True` pause fires at the first statement of the chosen branch (falls back to `node` if the branch is empty)
- **`ListCompIf` / `ListCompIfElse`** — at the `if` node before condition, then at the chosen branch after; in both `_eval_list_comp` and `_eval_list_comp_body`
- **`LetOp`** — after each assignment, with the new variable already in `child_ctx`
- **`ListCompFor`** — at the start of each iteration, after loop variables bind into `loop_ctx`
- **`ListCompLet`** — after each assignment, in both `_eval_list_comp` and `_eval_list_comp_body`
- **`ListCompEach`** — before the body expression, in both `_eval_list_comp` and `_eval_list_comp_body`
- **List element expressions** — before each element-producing expression: the `else` branch in `_eval_list_comp` and the fallthrough in `_eval_list_comp_body`

**Expression-level Step Out**: from an `expr_level` checkpoint, Step Out backs out one level of listcomp nesting (`for`, `if`, `each`, or nested `[...]` body). The evaluator tracks `self._expr_depth: int`, incrementing on entering each listcomp body and decrementing on exit; the hook passes `expr_depth` to `DebugSession`. `_current_pause_expr_depth` stores the depth at pause. If `> 0`, Step Out sets `_step_out_expr_depth = _current_pause_expr_depth - 1`; the hook fires on any checkpoint (including `expr_level=True`) where `expr_depth <= _step_out_expr_depth`. If `== 0`, normal call-stack Step Out applies (`_step_out_depth = depth`).

The Variables panel has:
- A **filter dropdown**: Locals / Globals / CONSTANTS / $Specials
- A **Hiddens checkbox**: when unchecked, names starting with `_` or `$_` are hidden from all filters

Categorization (after the hidden check):
- `$`-prefix → $Specials
- ALL_UPPERCASE with at least one letter → CONSTANTS
- Name in `local_scope` → Locals
- Otherwise → Globals

`_filtered_vars(frame_data, category, show_hidden)` computes the display dict. Only vars in `dyn_names` are editable, and only in the Locals filter of the innermost frame. `get_modifications()` skips non-editable rows.

### DebuggerPane states

Toolbar button order: Continue/Pause · Step Over · Step Into · Step Out · Stop · Restart

| Method | Status label | Continue/Pause btn | Step buttons | Stop | Restart |
|---|---|---|---|---|---|
| `set_running()` | "Running…" | **Pause** (enabled) | Disabled | Enabled | Enabled |
| `set_paused(line, frames, stack)` | "Paused at line N" | **Continue** (enabled) | All enabled | Enabled | Enabled |
| `set_error_break(line, msg, frames, stack)` | "Line N: \<error\>" | **Continue** (enabled) | Disabled | Enabled | Enabled |
| `set_idle()` | "Not debugging" | **Continue** (disabled) | Disabled | Disabled | Disabled |

The Continue/Pause button is a single `_btn_continue` widget whose icon/behavior depends on state: running → pause icon, emits `pause_requested`; otherwise → continue icon, emits `continue_requested`. `_set_continue_mode()` restores the continue icon and clears `_is_running`; called at the start of `set_paused`, `set_error_break`, and `set_idle`.

**Restart** — `_on_debug_restart()` in `main_window.py` stops the current session (`tab.debug_session.stop()`, sets `tab.debug_session = None`), clears the execution line highlight, then calls `_start_debug()`. Since `tab.debug_session` is already `None`, the "already running → continue" guard doesn't fire and a fresh parse + session starts from the top.

## V1 Scope Boundaries

**In scope**: Script editing, real-time 3D rendering, basic WYSIWYG drag interaction, CSG operations, graceful invalid-code handling.

**Explicitly out of scope for v1**: Constraint solver, collaborative editing, cloud modeling, incremental/tolerant parsing, node-based visual programming, plugin system.

## Undo/Redo

Code edits and gizmo drags are undo/redo-able via Qt's `QUndoStack`. Each operation is a `QUndoCommand` subclass:

- **Code edits**: `TextEditCommand` stores before/after document state and calls `QPlainTextEdit.setPlainText()` on undo/redo
- **Gizmo ops**: `GizmoCommand` stores before/after source text and re-triggers a render on redo

All Cmd+Z / Cmd+Shift+Z route through `QUndoStack`, which disables `QPlainTextEdit`'s built-in undo (`setUndoRedoEnabled(False)`).

## Console Output

The console displays:
- Parse errors (file/line/col from AST metadata)
- On each render: bounding box of the resulting mesh and current camera position

## Keyboard Shortcuts

Standard platform conventions apply throughout. Custom shortcuts:

| Key | Action |
|---|---|
| Cmd+1 | Toggle Show Edges |
| Cmd+2 | Toggle Show Axes |
| Cmd+3 | Toggle Show Crosshairs |
| Cmd+4 | Top view |
| Cmd+5 | Bottom view |
| Cmd+6 | Left view |
| Cmd+7 | Right view |
| Cmd+8 | Front view |
| Cmd+9 | Back view |
| Cmd+0 | Isometric view |
| Cmd++ | Increase editor font size |
| Cmd+- | Decrease editor font size |
| Cmd+[ | Zoom Out |
| Cmd+] | Zoom In |
| Shift+Cmd+V | View All |
| F6 | Render |

## Application Preferences

Preferences live under the `editor/` key group in `QSettings("NeuSCAD", "NeuSCAD")`, accessed via `load_preference(key, type)` / `save_preferences(dict)` in `preferences.py`.

| Setting | Key | Default |
|---|---|---|
| Font family | `editor/fontFamily` | `"Menlo"` |
| Font size | `editor/fontSize` | `13` |
| Indent size | `editor/indentSize` | `4` |
| Show column guide | `editor/showColumnGuide` | `True` |
| Column guide column | `editor/columnGuide` | `80` |

`MainWindow._apply_preferences()` reads all settings and pushes them to every open tab via `_apply_preferences_to_tab(tab, font, indent, show_guide, guide_col)`. Called on startup (end of `_restore_settings()`) and after the dialog is accepted. New tabs from `_new_document()` and `_create_and_add_tab()` also call it to inherit current settings immediately.

`CodeEditor.set_indent_size(n)` stores `_indent_size` and updates `tabStopDistance`. All indent/unindent logic in `keyPressEvent` reads `self._indent_size`.

The Preferences action uses `QAction.MenuRole.PreferencesRole` so Qt places it in the macOS application menu (Cmd+,).

**Word Wrap** is a checkable Edit menu item stored at the top level in `QSettings` under `"wordWrap"` (default `False`), not under `editor/`. Persisted/restored in `closeEvent`/`_restore_settings` using the same `blockSignals` pattern as `perspective`, and applied via `_apply_word_wrap_to_tab(tab)` (also called for new tabs in `_new_document` and `_create_and_add_tab`).

## Startup Behavior

Opens with a single blank untitled document.

## GUI Layout

```
┌──────────────────────────────────────────────────────────────┐
│  [Open] [Render] [Export]  |  [Undo] [Redo]                 │  ← toolbar
├──────────────────────────────────────────────────────────────┤
│  [file1.scad ×]  [file2.scad ×]  [+]                        │  ← tabs
├──────────────────────────┬──────────────────────────┬────────┤
│                          │                          │   T    │
│                          │                  [cube]  │   R    │
│   QScintilla             │   3D Viewport            │   S    │
│   Code Editor            │                          │   ·    │
│                          │                          │   ·    │
├──────────────────────────┴──────────────────────────┴────────┤
│  Console                                                      │
├──────────────────────────────────────────────────────────────┤
│  x: 10.0  y: 5.0  z: 2.5                                    │  ← status bar
└──────────────────────────────────────────────────────────────┘
```

- **Toolbar**: across the top — Open, Render, Export, Undo, Redo
- **Tabs**: one per open file; can be torn off into separate windows
- **Code editor**: left pane (QScintilla)
- **3D viewport**: center pane; always visible; contains:
  - Cube gizmo in a corner for view angle control
  - Camera icon next to the cube gizmo; clicking opens a popup with current viewport translation, rotation, distance, and FOV
- **Tools strip**: right of the viewport — Translate, Rotate, Scale, and future tools
- **Console**: bottom pane
- **Status bar**: bottom strip; shows 3D coordinates of the last clicked point on the mesh

The code editor, console, and debugger are `QDockWidget` instances — dockable to any side or floatable, with position/visibility persisted via `QSettings("NeuSCAD", "NeuSCAD")` (`saveState()`/`restoreState()`). Object names: "EditorDock", "ConsoleDock", "DebuggerDock".

Scale markers are tick marks along the viewport axes showing distance units (Show Scale Markers). Show Edges renders the full triangulation wireframe via `GL_POLYGON_OFFSET_FILL` on the solid pass (pushes fill surfaces away from camera), then draws edges at true depth in a second pass — avoids z-fighting on coplanar faces while keeping hidden edges correctly occluded. Show Crosshairs draws four white diagonal lines (the four space diagonals of a unit cube) crossing at the camera target, each extending `camera.distance * 2.5 / 12`. Perspective/orthographic toggle uses `camera.orthographic`, persisted in QSettings.

**Restoring settings to checkable actions**: wrap `setChecked()` in `blockSignals(True/False)`, then call the handler explicitly — avoids double-invocation (signal + explicit call) and ensures the handler fires even if the stored value matches the default. Pattern used in `_restore_settings`:
```python
self._act_perspective.blockSignals(True)
self._act_perspective.setChecked(perspective)
self._act_perspective.blockSignals(False)
self._toggle_perspective(perspective)
```

**New tabs must inherit viewport settings**: every new `DocumentTab` gets a fresh default `Viewport`/`Camera`. After connecting signals and before adding to the tab widget, call `_apply_perspective_to_tab(tab)` (and any future per-viewport settings) to match the current UI state. The `hasattr(self, '_act_perspective')` guard covers `_new_document()` being called during `__init__` before `_setup_menus()` finishes — in practice `_setup_menus()` runs first, so it's defensive only.

## Menu Structure

**File**: New / Open… / Open Recent ▶ / Close / Save / Save As… / — / Export… / — / Quit

**Edit**: Undo / Redo / — / Cut / Copy / Paste / Select All / — / Expand Selection / Contract Selection / — / Indent / Undent / Comment / Uncomment / — / Find… / Find & Replace… / — / Word Wrap (checkable)

**Design**: Render / — / Insert Primitive ▶ (Cube, Sphere, Cylinder, Cone, …) / Boolean Operation ▶ (Union, Difference, Intersection) *(behavior of Insert Primitive and Boolean Operation deferred)*

**View**:
- Show Toolbar / Show Tab Bar / Show Code Editor / Show Tools Strip / Show Console
- —
- Top / Bottom / Left / Right / Front / Back / Isometric / View All
- —
- Perspective (toggle perspective/orthographic projection)
- —
- Show Axes / Show Edges / Show Scale Markers / Show Crosshairs / Show Status Bar

**Window**: Minimize / Zoom / — / Move Tab to New Window / — / *(open document list)* / Bring All to Front
