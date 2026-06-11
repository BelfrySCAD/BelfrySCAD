# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NeuSCAD is a hybrid procedural CAD application combining OpenSCAD-style script-based modeling with live WYSIWYG 3D interaction. The defining feature is **bidirectional synchronization** between source code and 3D geometry — users can edit code or drag geometry, and both views stay in sync.

**Status**: In active development. Core pipeline, rendering, editor, and several WYSIWYG features are implemented. The full design is in `PRD.md`.

## Technology Stack

- **UI Framework**: PySide6 (Qt)
- **Code Editor**: `QPlainTextEdit` + `QSyntaxHighlighter` (PySide6 built-ins; text layer only — not semantically aware)
- **Parser**: openscad_parser ≥2.5.1 (strict PEG-based, generates AST with file/line/col/span metadata; parses full OpenSCAD syntax but has no knowledge of built-in functions or modules — the evaluator layer implements all built-ins). Declared as a local editable dependency via `[tool.uv.sources]` pointing to `../openscad_parser`.
- **CSG Kernel**: Manifold (union, difference, intersection, boolean ops)
- **Renderer**: ModernGL (GPU mesh rendering, camera controls)
- **Language**: Python

## Core Architecture

The pipeline flows strictly in one direction during normal operation:

```
Source Code → QScintilla Editor → openscad_parser (AST) → Evaluator → Manifold (CSG/mesh) → ModernGL → PySide6 UI
```

**The AST is the single source of truth** — not the rendered geometry, not the editor text.

### Error Display

Parse errors are indicated in the editor with a squiggly underline at the error location, implemented via `QTextCharFormat` with `SpellCheckUnderline` style applied as an extra selection on the `QPlainTextEdit`. Errors are also reported in the console.

### Find / Replace

`CodeEditor.show_find(replace=False)` opens a `FindBar` overlay widget parented to the editor, positioned at the top-right corner. `show_find(replace=True)` also reveals the replace row. Triggered by Cmd+F and Cmd+H respectively.

`FindBar` features:
- Plain-text and regex search (toggled by `.*` button), case-sensitive toggle (`Aa`)
- All matches highlighted in pale yellow; current match in orange with white text
- Match count label ("N of M"); previous/next navigation (◀ ▶ or Shift+Enter / Enter)
- Replace one (replaces current match) and Replace All (works backwards through matches to preserve positions, wrapped in a single `beginEditBlock`/`endEditBlock` for one undo step)
- If the editor has a single-word selection when Find opens, it is pre-populated into the search field
- Escape closes the bar and returns focus to the editor
- `_find_selections` is a separate extra-selection list on `CodeEditor`, inserted between `_selection_extra` and `_exec_selection` in `_refresh_extra_selections`
- Document changes while the bar is open automatically re-run the search via `document().contentsChanged`

### Indent Guides

Faint vertical lines are drawn inside the indentation whitespace of each indented line, at every `_indent_size` columns — except at the column of the first non-whitespace character itself. Implemented as `_IndentGuides(QWidget)`, a transparent overlay parented to `CodeEditor.viewport()`, created before `_ColumnGuide` so the column guide renders on top. Repaint is triggered by `document().contentsChanged` and by `set_indent_size()`.

Paint logic: for each visible block, count leading spaces `n`. Draw a guide at columns `indent_size, 2*indent_size, …` while `col < n` (strictly less than, so the column at `n` — right before the first non-whitespace character — is never drawn). Empty and unindented lines are skipped. Uses `QFontMetricsF` for sub-pixel accuracy.

### Column Guide

A faint vertical line is drawn at column 80 in the code editor. It is implemented as `_ColumnGuide(QWidget)`, a transparent overlay widget parented to `CodeEditor.viewport()`. Key implementation notes:
- `WA_TransparentForMouseEvents` + `WA_TranslucentBackground` so only the line pixel is visible and all mouse events pass through to the text
- `update_geometry()` keeps the overlay sized to the full viewport rect; called from `CodeEditor.resizeEvent()`
- The x position is computed as `cursorRect(cursor_at_pos_0).x() + QFontMetricsF(font).horizontalAdvance('0' * 80)`. `QFontMetricsF` (not `QFontMetrics`) is required — the integer version rounds character width up by ~0.2px, which accumulates to ~2 columns of error over 80 characters.

### Code Folding

Fold markers (▼ unfolded, ▶ folded) appear in the right section of the line-number gutter. Clicking triggers `toggle_fold(block_number)`.

`_compute_fold_regions(doc)` returns `{open_block: close_block}` using two passes:
1. **Delimiter matching** — `{…}`, `(…)`, `[…]` pairs; a region is created only when the opener and closer are on different lines
2. **Indentation continuation** — any non-empty line followed by at least one non-empty line that is strictly more indented; covers function bodies, ternary chains, nested list comprehensions, and any other multi-line indented expression; `setdefault` ensures delimiter regions from pass 1 take precedence

`_fold_regions` is recomputed lazily on the first paint after `_fold_dirty` is set by `_on_doc_changed`. `_fold_busy` guards prevent re-entrant recomputation and prevent `_on_doc_changed` from resetting `_fold_dirty` while a fold toggle is in progress.

`_set_range_visible(start_bn, end_bn, visible)` sets `QTextBlock.setVisible()` on each hidden block then calls `cursor.beginEditBlock(); cursor.endEditBlock()` — a no-op edit that forces `QPlainTextDocumentLayout` to recalculate block heights (required for visibility changes to take effect).

Fold indicators are drawn with `painter.drawPolygon(QPoint[])` — `QPainterPath.drawPath` was tested and found invisible at small sizes on macOS; `drawPolygon` renders reliably.

### Go to Definition

Right-click on any identifier in the code editor shows a context menu with "Go to Definition of 'name'". The menu item only appears for words matching `[A-Za-z_][A-Za-z0-9_]*` (standard identifiers).

`CodeEditor.go_to_definition_requested` signal (emits the word string) is connected to `MainWindow._go_to_definition(tab, word)` for each tab.

`_go_to_definition` requires a cached `root_scope` on the tab (set after every successful `build_scopes()` call — both in the render worker and in `_start_debug()`). If no scope is cached yet, it logs a message asking the user to render first.

Lookup order: `scope.lookup_variable(word)` → `scope.lookup_function(word)` → `scope.lookup_module(word)`. The first non-None result wins. Built-in modules return `None` from `lookup_module` and are silently skipped.

The definition node's `.position.origin` gives the source file path; `.position.line` gives the 1-indexed line number. Navigation:
- Same file (or origin is `None` / tab is untitled): scroll the current editor to the definition line
- Different file: search open tabs for a matching `file_path`; if found, switch to it; if not, open the file via `_create_and_add_tab()` (view-only, no render triggered)

`_create_and_add_tab(path, text) -> DocumentTab` is a helper that creates a fully-connected tab (all viewport and debugger-pane signals, perspective, Go-to-Definition signal) and adds it to the UI. Used by `_open_file`, `_open_recent`, and `_go_to_definition`; not used by `_new_document` (which creates a blank tab with a different setup path).

### Critical Constraint: Strict Parser

The parser produces **no partial AST** — it either succeeds fully or fails entirely. The system must handle the no-AST state gracefully:
- Cache the last valid AST
- Display last valid geometry while code is invalid
- Never block the UI or break the viewport

### Bidirectional Loop (future-critical, v1 groundwork required)

When the user drags geometry in the viewport:
```
Drag event → ray cast → pick geometry ID → map ID to AST node (via span) → modify AST parameter → regenerate code + model
```

This requires every AST node to carry both its **source span** (file/line/col) and its **geometry ID(s)** from the Manifold output. This mapping is the hardest design problem in the project.

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

Command-click in the viewport triggers selection:
```
ray cast → hit triangle → run_original_id lookup → AST node → highlight source span in QScintilla + visual highlight in viewport
```

Command-click always lands on the leaf geometry node (the innermost primitive). The selection can then be walked up or down the AST hierarchy — expanding the selection to a parent node (e.g., from `cube()` up to its enclosing `translate()`, then up to a `difference()`) or back down toward the leaf. Moving the selection up highlights the geometry produced by the entire subtree rooted at that node. The editor highlight tracks accordingly, covering the full source span of the selected node.

When walking down from a node with multiple children, select the child whose geometry is closest to the original ray-cast hit point.

Multiple objects can be selected, but only as a complete subtree — walking up to a parent node selects all its children as a unit. Selecting arbitrary disjoint objects is not supported.

Selected objects are highlighted with an outline (stencil buffer technique). If outline rendering proves too expensive, fall back to mesh tinting.

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

The user's tool selection declares which transform type to edit — the system does not need to infer intent. For each tool activation on a selected node:

1. Search the AST for an existing transform wrapper of the matching type immediately enclosing the selected node
2. If found: update its vector argument via a **targeted source span replacement** (not a full code regeneration)
3. If not found: insert a new wrapper around the selected node's source span

### Value Overlay

During any translate, rotate, or scale operation a text readout of the current value is displayed in the viewport. The user can edit this value directly (type an exact number) rather than dragging. Committing the typed value applies the same source rewrite rules as a drag commit.

Enter commits the typed value; Escape cancels and reverts to the pre-interaction state.

The ghost mesh updates on commit (Enter), not while typing.

The text field only receives focus when clicked — it does not auto-focus on drag-start.

Displayed value follows the same classification as the source rewrite rules: show the absolute value when the argument is a literal number or a bare variable set to a number; show a delta when the argument is an expression.

### Transform Edit Rules

- **Nested transforms of the same type**: Modify the innermost matching wrapper.
- **Transform composition order**: New wrappers are always inserted outside any existing transform wrappers on the selected node.
- **Live drag preview**: Display a wireframe ghost copy of the mesh during drag; commit the AST edit and trigger a render on mouse-up.
- **Gizmo orientation**: Handles are drawn in local (post-transform) space.

### Source Rewrite Rules (Intent Preservation)

When a drag commits, the system rewrites the minimum necessary source text based on what the transform argument contains:

| Argument form | Rewrite strategy |
|---|---|
| Literal value (`[10, 0, 0]`) | Replace the affected component(s) in place; preserve named vs. positional style |
| Variable set to a literal (`x = 10`) | Update the literal at the variable's declaration site |
| Variable set to an expression (`x = base/2`) | Append a delta at the declaration site: `x = base/2 + 5` |
| Inline expression (`[base/2, 0, 0]`) | Append a delta inline: `[base/2 + 5, 0, 0]` |

Note: editing a variable declaration affects all sites that reference that variable, which is intentional — the user's parametric relationships are preserved.

## AST Evaluator

The evaluator sits between openscad_parser and Manifold. It is a recursive AST walker that produces Manifold geometry from a parsed AST.

### Scope processing

Call `build_scopes()` on the AST immediately after parsing. This annotates every node with a `.scope` attribute. Three independent namespaces exist — variables, functions, modules — and parent-chain lookup is automatic:

```python
scope.lookup_variable(name)  # returns the Assignment/ParameterDeclaration node
scope.lookup_function(name)  # returns the FunctionDeclaration node
scope.lookup_module(name)    # returns the ModuleDeclaration node or None (built-in)
```

Declarations are hoisted within their block (forward references work). Last-wins scoping is already implemented by the library — later assignments in the same scope overwrite earlier ones.

### Architecture

Recursive AST walker with a built-ins dispatch table:

1. For each `ModularCall` node: look up via `scope.lookup_module(name)`
   - If `None` (not user-defined) → dispatch to built-ins table
   - If found → recursively evaluate the module body in a new child scope
2. For each `Identifier` in an expression: call `scope.lookup_variable(name)` then evaluate the bound value; if not found in variable namespace, fall back to `scope.lookup_function(name)` — this allows passing named functions as values (required for `is_function()`)
3. For each function call: look up via `scope.lookup_function(name)`, evaluate args in caller's scope, evaluate body in new scope
4. Default parameter values are evaluated in the **caller's** scope, not the callee's

### Assignment execution order

Within each scope (top-level, module body, `if`/`for` block), all `Assignment` nodes are evaluated **before** any geometry statements, matching OpenSCAD's last-wins semantics. For example, `a = 5; cube(a); a = 10;` produces a 10×10×10 cube — `a = 5` and `a = 10` both run before `cube(a)`. This applies recursively at every level processed by `evaluate()` and `_eval_children()`.

Assignments are **eager**: when `_eval_statement` processes an `Assignment`, it evaluates the expression immediately and stores the result in `ctx.dyn` as `__let_{name}`. `_eval_identifier` checks `ctx.dyn` first, so the cached value is used for subsequent references in the same scope. Forward references (a variable used before its assignment in source order) fall back to `scope.lookup_variable()` and lazy evaluation.

When the same variable is assigned twice in the same scope, the second assignment overwrites the first and emits:
```
WARNING: a was assigned on line 1 but was overwritten in file foo.scad, line 3
```
matching OpenSCAD's exact warning format. `EvalContext.dyn_positions` tracks the source position of each `__let_*` entry for this purpose.

`_eval_children` shares `ctx.dyn` (not a copy) across all sibling nodes so that eager assignments from one sibling are immediately visible to subsequent ones in the same block.

`EvalContext` has two context-creation methods with different inheritance rules:

| Method | `__let_*` inherited | Use for |
|---|---|---|
| `child_ctx()` | Yes (full copy) | `for`/`let` iterations, `_eval_let_block`, list comprehension scopes — where outer variable bindings must remain visible |
| `call_ctx()` | No (only `$*` dynamic vars) | Module and function calls — callee has its own variable scope; inheriting caller `__let_*` would trigger spurious double-assignment warnings |

### Built-ins implemented

**3D Primitives** (→ `ColoredBody.body`): `cube`, `sphere`, `cylinder`, `polyhedron`

**2D Primitives** (→ `ColoredBody.section`): `circle`, `square`, `polygon`

**Extrusion** (2D → 3D): `linear_extrude`, `rotate_extrude`

**Transforms** (apply to child geometry, 3D and 2D): `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `resize`, `color`, `offset`

**Booleans** (3D or 2D, dispatched by child type): `union`, `difference`, `intersection`

**Topology**: `hull`, `minkowski`, `projection`

**Control / utility**: `for`, `intersection_for`, `let`, `if`/`else`, `echo`, `assert` (modular + expression forms), `render`, `children()`, `breakpoint()`

`breakpoint()` — immediately pauses execution in the debugger at the call site. Optional first positional or keyword argument `condition`: if provided and falsy, the breakpoint is skipped. No-op when running outside the debugger. Implemented via `_check_debug(node, ctx, forced=True)`, which passes `forced=True` through to the debug hook so the hook bypasses its normal step/breakpoint-line check.

**Math functions**: `abs`, `sign`, `ceil`, `floor`, `round`, `sqrt`, `ln`, `log`, `exp`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `min`, `max`, `pow`, `norm`, `cross`, `rands`, `lookup`

**String / list functions**: `str`, `chr`, `ord`, `concat`, `len`, `search`

**Type checks**: `is_undef`, `is_bool`, `is_num`, `is_string`, `is_list`, `is_function`

**Constants**: `PI`

**Other**: `version`, `version_num`, `parent_module` (stub)

**Not yet implemented**: `text`, `surface`, `import` (warn and return None)

**Special variables**: `$fn`, `$fa`, `$fs` control mesh resolution. `$children` is set to the child count when entering a user module body. `$`-prefixed named arguments in any call (e.g. `sphere(r=2, $fn=64)`) are merged into the dynamic context for that call and its children.

**Viewport special variables**: `$vpt` (viewport translation = `camera.target` as `[x,y,z]`), `$vpr` (viewport rotation = `[elevation, 0, azimuth]`), and `$vpd` (viewport distance = `camera.distance`) are injected into the root `EvalContext.dyn` at render and debug start. They are snapshotted in the main thread via `MainWindow._viewport_params(tab)` before the worker thread launches, so the values are consistent with what the user sees. `Evaluator.evaluate()` accepts a `viewport_params: dict | None` argument and merges it into `ctx.dyn` before processing begins.

### originalID assignment

Each geometry-producing node (primitives and their transform/boolean ancestors) is assigned a unique Manifold `originalID` via `ReserveIDs`. The evaluator builds and returns the `originalID → AST node` lookup table alongside the Manifold mesh.

### 2D geometry

`ColoredBody` carries either a 3D `body: Manifold` or a 2D `section: CrossSection` (not both). 2D primitives (`circle`, `square`, `polygon`) return a `ColoredBody` with only `section` set. `linear_extrude` and `rotate_extrude` consume 2D children via `_to_cross_section()` (which unions all child sections) and return a 3D body. Boolean ops (`union`, `difference`, `intersection`) dispatch on whether children carry 3D bodies or 2D sections. `_combine()` is robust to mixed children — it uses 3D bodies if any are present, otherwise unions sections.

`manifold3d.CrossSection` supports full 2D CSG: `+` (union), `-` (difference), `^` (intersection), `offset`, `hull`, `batch_hull`, `revolve`, `extrude`, and all 2D transforms. `CrossSection.to_polygons()` returns the contours for polygon construction.

`_builtin_transform` dispatches on the child type: `_apply_transform_2d` handles `CrossSection` children (using `cs.translate`, `cs.rotate`, `cs.scale`, `cs.mirror`); `_apply_transform_3d` handles `Manifold` children. `resize` and `multmatrix` are 3D-only — 2D children are passed through unchanged for those two. This means `translate([4,0]) circle(r=1)` and similar 2D transform chains work correctly, including as inputs to `hull()`.

### Color propagation

`color()` sets the current color in the evaluation context; it cascades to all child geometry. The evaluator passes per-body color information to the renderer alongside the Manifold mesh.

### Error handling

Runtime errors raise `EvalError` and are reported to the console; the last-valid geometry is kept in the viewport.

Error format matches OpenSCAD's output exactly:
```
ERROR: Assertion 'false' failed: "message" in file foo.scad, line 5
TRACE: called by 'assert' in file foo.scad, line 5
TRACE: call of 'inner()' in file foo.scad, line 4
TRACE: called by 'inner' in file foo.scad, line 2
TRACE: call of 'outer()' in file foo.scad, line 1
TRACE: called by 'outer' in file foo.scad, line 7
```

Unknown modules emit `WARNING: Ignoring unknown module 'name' in file ..., line n` followed by the same TRACE lines, without raising an exception.

`_call_stack` entries: modules use 4-tuples `("module", name, call_pos, decl_pos)` where `call_pos` is the call site and `decl_pos` is where the module declaration starts; functions use 3-tuples `("function", name, call_pos)`. `error(msg, node=None, innermost_frame=None)` accepts the failing AST node and an optional innermost frame label (e.g. `"assert"`) for the first TRACE line. If `error_break_fn` is set (debug mode), `error()` calls it before raising `EvalError`, pausing the debugger at the error site.

### Special variable scoping (`$variables`)

`$`-prefixed variables (`$fn`, `$fa`, `$fs`, `$t`, `$children`, etc.) use **dynamic scoping** — they are inherited down the **call chain**, not the lexical scope chain. This is distinct from regular variables, which follow lexical scoping.

The evaluator must maintain a separate dynamic binding context (a dictionary threaded through each module call). When a module is invoked with `$fn=32`, that value propagates to all nested calls made within that invocation, regardless of lexical scope boundaries. Regular `scope.lookup_variable()` must not be used for `$`-prefixed names.

### `include` vs `use`

Follows OpenSCAD semantics exactly:
- `include <file.scad>` — brings all declarations and top-level geometry into the current scope
- `use <file.scad>` — brings only functions and modules (top-level geometry is ignored)

### Implementation quirks

- `UseStatement.filepath` is a `StringLiteral` AST node, not a plain string — always use `.filepath.val` to get the actual path string.
- "file not found" errors from library resolution (e.g. internal BOSL2 files already handled by the parser) are suppressed in the console to avoid noise.
- `sys.setrecursionlimit(10000)` is set in `main()` for BOSL2 compatibility. `RecursionError` is caught around `build_scopes()` and `evaluate()` calls and treated as a runtime error (shows last-valid geometry).
- **Ranges** are represented as an `OscRange(start, step, end)` object, not an expanded list. `echo([1:3])` prints `[1 : 1 : 3]`, not `[1, 2, 3]`. Ranges are expanded to a list only when iterated (in `for`, list comprehensions, `intersection_for`) or indexed with `[i]`. A zero-step range echoes as `[1 : 0 : 5]` and iterates to nothing.
- **Boolean arithmetic** returns `undef` (`None`), not a number. OpenSCAD does not coerce `true`/`false` in arithmetic: `true + 1` → `undef`. The evaluator checks `isinstance(a, bool) or isinstance(b, bool)` before any arithmetic op.
- **Division by zero** returns IEEE 754 values: `1/0` → `inf`, `-1/0` → `-inf`, `0/0` → `nan`. Math domain errors follow the same convention: `sqrt(-1)` → `nan`, `ln(0)` → `-inf`, `asin(2)` → `nan`.
- **Negative string/list indexing** returns `undef` (`None`), not Python-style wraparound. `"hello"[-1]` → `undef`. The `PrimaryIndex` handler rejects any `i < 0`.
- **Named args to built-in math functions** are mapped to positional order as a fallback (e.g. `abs(x=-3)` → `3`). The evaluator tries positional args first; if none, uses named args in declaration order.
- **`parent_module()`** returns `undef` at the top level (not `""`).
- `search()` has two distinct match modes based on the first argument's type:
  - **String match**: treated as a character array — each character is searched independently. With `num_returns=1` (default), not-found characters are **dropped** from the result list entirely (not included as `[]`). With `num_returns=0`, not-found characters are included as `[]`. Only valid when the vector is also a string.
  - **List match**: does direct equality comparison against each vector entry (or `vector[i][index_col]`). This is the correct idiom for finding a string in a list of strings: `search(["foo"], ["foo","bar","baz"])` → `[0]`.
  - **Scalar match**: returns a list of up to `num_returns` matching indices (`[]` if not found). `num_returns=0` returns all matches.
- **Assert message format**: `to_openscad([cond_expr]).strip()` is used to recover the condition source text for the `Assertion 'expr' failed` message. This requires `from openscad_parser.ast import to_openscad`.
- **String literals with leading/trailing whitespace**: arpeggio's `skipws=True` would strip whitespace before each sub-rule in the `(DQUOTE, contents, DQUOTE)` sequence, silently eating leading spaces inside strings (e.g. `"  bar"` → `"bar"`). Fixed in openscad_parser 2.5.1 by collapsing `string_literal` into a single regex terminal `"(?:[^"\\]|\\.|\\$)*"` so no whitespace skipping occurs inside quotes.

## Manifold API: Geometry Provenance

Manifold tracks geometry provenance through CSG operations via the `Mesh` output structure (the Python bindings use `m3d.Mesh`, not `MeshGL`). The key fields after any boolean op:

| Field | Meaning |
|---|---|
| `run_original_id` | Array of source mesh IDs, one per triangle run |
| `run_index` | Boundaries of runs in the triangle array |
| `face_id` | Which source triangle each output triangle derives from |

Each Manifold body constructed from scratch receives a unique auto-incremented `originalID`. After a CSG boolean (e.g., `body1 - body2`), output triangles are organized into **runs** tagged with the `originalID` of their contributing input body.

### AST ↔ Geometry ID Mapping Pattern

Manifold has no concept of AST nodes — the mapping layer must be maintained by the application:

1. Assign one `originalID` per geometry-producing AST node (use `ReserveIDs` to allocate)
2. After each CSG operation, walk `run_original_id` to recover which output triangles belong to which AST node
3. Store a lookup table: `originalID → AST node`

This table is how the WYSIWYG pick loop resolves a ray-cast triangle hit back to an editable AST parameter:
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

OpenSCAD's `color()` function affects viewport display. Color is applied by the evaluator and passed to ModernGL for rendering. `color()` cascades to all children in the subtree, following OpenSCAD's standard behavior.

## Key Design Requirements

- **Code ↔ Geometry mapping**: Every geometry-producing AST node owns an `originalID`. The `originalID → AST node` table must be rebuilt on each render trigger.
- **Stability under invalid code**: UI must never crash or go blank when code is invalid.
- **Deterministic regeneration**: AST → geometry must be reproducible with no hidden rendering state. Full Manifold rebuild runs on every render trigger (incremental evaluation is a future optimization).
- **Performance**: <200ms model regeneration for small/medium models; 60 FPS viewport.

## File Format & Export

- **File format**: `.scad` (OpenSCAD-compatible plain text)
- **Language**: Full OpenSCAD language (variables, functions, modules, loops, conditionals, all built-in primitives and transforms)
- **Export**: STL, OBJ, 3MF; STEP under investigation (Manifold produces triangle meshes; STEP is B-rep — any STEP export would be a faceted solid, which may have limited downstream CAD value)
- **Export workflow**: if no current render exists, Export triggers a render automatically before exporting

## Render Triggers

There is no live preview. Full Manifold CSG processing runs when:

- The user selects **Render** (toolbar or Design menu)
- A file is **opened**
- A file is **saved**
- A **gizmo drag commits** (mouse-up)

The viewport always shows the result of the last render. While the user edits code, the viewport is static.

## Threaded Rendering

Parse + evaluate runs in a background `QThread` so the GUI stays responsive. The implementation uses two helper classes in `main_window.py`:

- **`_RenderWorker(QObject)`** — moved to the worker thread via `moveToThread`; does the parse/evaluate work; emits `logged`, `parse_errored`, `finished`, and `done` signals
- **`_RenderCallback(QObject)`** — stays in the main thread; has `@Slot` methods that receive the worker signals; Qt auto-detects the cross-thread boundary and uses `QueuedConnection`, so all callbacks run on the main thread

**Do not connect worker signals to Python lambdas** — lambdas have no thread affinity, so Qt cannot determine which event loop to post to. Always route through a `QObject` slot with known thread affinity.

**Cancellation**: `_render()` passes a `threading.Event` to the worker; the worker checks `cancel.is_set()` between major steps. A `render_id` integer is incremented on each new render; the callback discards results whose `render_id` no longer matches the current one.

**Progress indicator**: an indeterminate `QProgressBar` in the status bar is shown while rendering and hidden when the worker's `done` signal fires. A `WaitCursor` override is set/restored at the same time.

## Debugger

The debugger runs the evaluator in a daemon worker thread (`DebugSession`) and surfaces a `DebuggerPane` widget with call-stack and variables panels.

### DebugSession (`debugger.py`)

Signals (all emitted from worker thread; Qt queues them to main thread):

| Signal | Args | When |
|---|---|---|
| `paused` | `line, all_frame_locals, call_stack` | Hit a breakpoint or step |
| `error_break` | `line, msg, all_frame_locals, call_stack` | Any runtime error |
| `finished` | `bodies, id_to_node` | Evaluation completed |
| `errored` | `str` | Unhandled exception after error_break resume |

`all_frame_locals` is a list of frame dicts, **innermost first**, with one extra `<toplevel>` entry appended when inside a call. `all_frame_locals[0]` matches row 0 (innermost) in the call-stack list. Each entry has three keys:

| Key | Contents |
|---|---|
| `"local_scope"` | All eagerly-assigned vars in the current frame's `ctx.dyn`: `__let_*` (params, `for`/`let`, assignments executed so far) and `$*` specials |
| `"outer_scope"` | Global vars from `_root_ctx.dyn` (innermost frame only, when inside a call; parent frames get `{}`) |
| `"dyn_names"` | `set` of names from `dyn` — the only vars that can be modified via the pane |

**Debug hook** — `_make_hook()` returns a closure passed to `Evaluator(debug_hook=...)`. Signature: `hook(line, locals_dict, call_stack, all_frame_locals) → (cmd, mods)`. `locals_dict` = dyn-bound locals only (used for `mods`). `call_stack` = the real call stack (used for step-depth math). The hook emits a **display** call stack with a `("toplevel", "<toplevel>", None)` entry appended before emitting the `paused` signal. The hook pauses on breakpoints, step-into, step-over, step-out, and user-requested pauses by blocking on a `threading.Event`.

**Pause during execution** — `DebugSession.pause()` sets a `_pause_requested` flag. The hook checks and consumes this flag at the top of every call; if set, it triggers an immediate pause regardless of breakpoints or step state. This allows interrupting a long-running evaluation to inspect what is happening.

**Error break** — `Evaluator(error_break_fn=self._error_break)` intercepts every `error()` call before it raises `EvalError`. `_error_break` emits `error_break` and blocks until the user resumes. After the user presses Continue, `EvalError` propagates normally (caught by `_run`, triggers `errored`).

### Call stack display

The call-stack list is displayed **innermost-first** (the currently executing frame at row 0, `<toplevel>` at the bottom). `_call_stack` in the evaluator is outermost-first; the display stack is built as `list(reversed(call_stack)) + [("toplevel", "<toplevel>", None)]` in both `_make_hook()` and `_error_break()`. `_populate_stack()` iterates the display stack in order without reversing. `all_frame_locals[0]` always corresponds to row 0 (innermost frame).

When inside a call, a corresponding `<toplevel>` frame (whose `local_scope` = the global scope vars) is appended to `all_frame_locals`. Clicking `<toplevel>` and selecting Locals shows the file's global variable declarations.

### Per-frame variable inspection

The evaluator maintains `_frame_ctxs` (an `EvalContext` list, parallel to `_call_stack`), pushed/popped in `_eval_user_module` and `_eval_user_function`. At each `_check_debug` call, `local_scope` is read directly from `ctx.dyn` (all `__let_*` and `$*` entries) — no scope walk needed because assignments are eager. When inside a call, `outer_scope` is populated from `_root_ctx.dyn` (the top-level context) to provide the Globals view. A `<toplevel>` frame with `local_scope = outer_scope` is appended when `_call_stack` is non-empty.

**Step Into for functions**: Function bodies are expressions (not statements), so `_eval_statement`'s `_check_debug` call is never reached for them. `_eval_user_function` explicitly calls `self._check_debug(decl.expr, child_ctx)` after pushing the call frame and before calling `_eval_expr(decl.expr, child_ctx)`. This gives the debugger a pause opportunity at the start of every function body, enabling Step Into to work correctly.

**Expression-level step points**: `_check_debug` accepts `expr_level=True` to mark sub-expression pauses. The debug hook only honours `expr_level` checkpoints for `step_into` (`_step_mode`); break-on-first, gutter breakpoints, step-over, and step-out all filter them out (`and not expr_level`). The following expression nodes call `_check_debug(…, expr_level=True)`:
- **`TernaryOp`** — twice: once before condition evaluation, then again at the chosen branch (true or false) after the condition is resolved
- **`ModularIf` / `ModularIfElse`** — the `if` node itself is already paused at by `_eval_statement`; a second `expr_level=True` pause fires at the first statement of the chosen branch after the condition is resolved (`node` used as fallback if the branch is empty)
- **`ListCompIf` / `ListCompIfElse`** — pause at the `if` node before condition, then at the chosen branch expression after; handled in both `_eval_list_comp` and `_eval_list_comp_body`
- **`LetOp`** — after each assignment, with the new variable already in `child_ctx` (so the value is visible in the Variables panel)
- **`ListCompFor`** — at the start of each iteration, after loop variables are bound into `loop_ctx`
- **`ListCompLet`** — after each assignment, in both `_eval_list_comp` and `_eval_list_comp_body`
- **`ListCompEach`** — before the body expression is evaluated, in both `_eval_list_comp` and `_eval_list_comp_body`
- **List element expressions** — before each element-producing expression: the `else` branch in `_eval_list_comp` (plain expression elements) and the fallthrough in `_eval_list_comp_body` (the final expression yielding one element)

**Expression-level Step Out**: When paused at an `expr_level` checkpoint, pressing Step Out backs out one level of listcomp nesting (one `for`, `if`, `each`, or nested `[...]` body). The evaluator tracks `self._expr_depth: int`, incrementing it when entering each listcomp body and decrementing on exit. The hook passes `expr_depth` to `DebugSession`. When paused, `_current_pause_expr_depth` is stored. Pressing Step Out when `_current_pause_expr_depth > 0` sets `_step_out_expr_depth = _current_pause_expr_depth - 1`; the hook fires on any checkpoint (including `expr_level=True` ones) where `expr_depth <= _step_out_expr_depth`. When `_current_pause_expr_depth == 0`, normal call-stack Step Out applies (`_step_out_depth = depth`).

The Variables panel has:
- A **filter dropdown**: Locals / Globals / CONSTANTS / $Specials
- A **Hiddens checkbox**: when unchecked, variables whose name starts with `_` or `$_` are hidden from all filters

Categorization (applied after the hidden check):
- `$`-prefix → $Specials
- ALL_UPPERCASE with at least one letter → CONSTANTS
- Name in `local_scope` → Locals
- Otherwise → Globals

`_filtered_vars(frame_data, category, show_hidden)` computes the display dict. Only vars in `dyn_names` are editable, and only in the Locals filter of the innermost frame. `get_modifications()` skips non-editable rows.

### DebuggerPane states

Button order in the toolbar: Continue/Pause · Step Over · Step Into · Step Out · Stop · Restart

| Method | Status label | Continue/Pause btn | Step buttons | Stop | Restart |
|---|---|---|---|---|---|
| `set_running()` | "Running…" | **Pause** (enabled) | Disabled | Enabled | Enabled |
| `set_paused(line, frames, stack)` | "Paused at line N" | **Continue** (enabled) | All enabled | Enabled | Enabled |
| `set_error_break(line, msg, frames, stack)` | "Line N: \<error\>" | **Continue** (enabled) | Disabled | Enabled | Enabled |
| `set_idle()` | "Not debugging" | **Continue** (disabled) | Disabled | Disabled | Disabled |

The Continue/Pause button is a single `_btn_continue` widget that changes icon and behavior depending on state. In running state it shows the pause icon and emits `pause_requested`; in all other states it shows the continue icon and emits `continue_requested`. `_set_continue_mode()` is a helper that restores the continue icon and clears `_is_running`; it is called at the start of `set_paused`, `set_error_break`, and `set_idle`.

**Restart** — `_on_debug_restart()` in `main_window.py` stops the current session (`tab.debug_session.stop()`, sets `tab.debug_session = None`), clears the execution line highlight, then calls `_start_debug()`. Because `tab.debug_session` is already `None` at that point, `_start_debug()`'s "already running → continue" guard does not fire and a fresh parse + session begins from the top.

## V1 Scope Boundaries

**In scope**: Script editing, real-time 3D rendering, basic WYSIWYG drag interaction, CSG operations, graceful invalid-code handling.

**Explicitly out of scope for v1**: Constraint solver, collaborative editing, cloud modeling, incremental/tolerant parsing, node-based visual programming, plugin system.

## Undo/Redo

Both code edits and gizmo drags are undo/redo-able via Qt's `QUndoStack`. Each operation is a `QUndoCommand` subclass:

- **Code edits**: a `TextEditCommand` that stores the before/after document state and calls `QPlainTextEdit.setPlainText()` on undo/redo
- **Gizmo ops**: a `GizmoCommand` that stores the before/after source text and re-triggers a render on redo

All Cmd+Z / Cmd+Shift+Z routes through `QUndoStack`, which disables `QPlainTextEdit`'s built-in undo (`setUndoRedoEnabled(False)`).

## Console Output

The console displays:
- Parse errors (with file/line/col from AST metadata)
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

Preferences are stored under the `editor/` key group in `QSettings("NeuSCAD", "NeuSCAD")` and accessed via `load_preference(key, type)` / `save_preferences(dict)` helpers in `preferences.py`.

| Setting | Key | Default |
|---|---|---|
| Font family | `editor/fontFamily` | `"Menlo"` |
| Font size | `editor/fontSize` | `13` |
| Indent size | `editor/indentSize` | `4` |
| Show column guide | `editor/showColumnGuide` | `True` |
| Column guide column | `editor/columnGuide` | `80` |

`MainWindow._apply_preferences()` reads all settings and pushes them to every open tab via `_apply_preferences_to_tab(tab, font, indent, show_guide, guide_col)`. Called on startup (end of `_restore_settings()`) and after the dialog is accepted. New tabs created via `_new_document()` and `_create_and_add_tab()` also call `_apply_preferences_to_tab` so they inherit the current settings immediately.

`CodeEditor.set_indent_size(n)` stores `_indent_size` and updates `tabStopDistance`. All indent/unindent logic in `keyPressEvent` reads `self._indent_size`.

The Preferences action uses `QAction.MenuRole.PreferencesRole` so Qt automatically places it in the application menu on macOS (Cmd+,).

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

- **Toolbar**: traditional bar across the top — Open, Render, Export, Undo, Redo
- **Tabs**: one per open file; tabs can be torn off into separate windows
- **Code editor**: left pane (QScintilla)
- **3D viewport**: center pane; always visible; contains:
  - Cube gizmo in a corner for view angle control
  - Small camera icon adjacent to the cube gizmo; clicking it opens a popup showing the current viewport translation, rotation, distance, and FOV
- **Tools strip**: narrow panel to the right of the viewport — Translate, Rotate, Scale, and future tools
- **Console**: pane at the bottom
- **Status bar**: thin strip at the very bottom; displays the 3D coordinates of the last clicked point on the mesh

The code editor, console, and debugger are `QDockWidget` instances — they can be docked to any side of the window or floated, and their positions and visibility are persisted via `QSettings("NeuSCAD", "NeuSCAD")` using `saveState()`/`restoreState()`. The dock widgets have object names "EditorDock", "ConsoleDock", "DebuggerDock" for state serialization.

Scale markers are tick marks along the viewport axes showing distance units (toggled by Show Scale Markers). Show Edges renders the full triangulation wireframe using `GL_POLYGON_OFFSET_FILL` on the solid pass (pushes fill surfaces away from camera) and then draws edges at true depth in a second pass — this avoids z-fighting on coplanar faces while keeping hidden edges correctly occluded. Show Crosshairs draws four white diagonal lines (the four space diagonals of a unit cube) crossing at the camera target, each extending `camera.distance * 2.5 / 12` in each direction. Perspective/orthographic toggle uses `camera.orthographic`; the current state is persisted in QSettings.

**Restoring settings to checkable actions**: Always use `blockSignals(True/False)` around `setChecked()` when restoring from QSettings, then call the handler explicitly. This avoids double-invocation (signal + explicit call) and ensures the handler fires even if the stored value matches the action's default. Example pattern used in `_restore_settings`:
```python
self._act_perspective.blockSignals(True)
self._act_perspective.setChecked(perspective)
self._act_perspective.blockSignals(False)
self._toggle_perspective(perspective)
```

**New tabs must inherit viewport settings**: Every new `DocumentTab` is created with a fresh `Viewport` and `Camera` at their defaults. After connecting signals and before adding to the tab widget, call `_apply_perspective_to_tab(tab)` (and any future per-viewport settings) to match the current UI state. The `hasattr(self, '_act_perspective')` guard handles the one case where `_new_document()` is called during `__init__` before `_setup_menus()` finishes — but in practice the order is `_setup_menus()` then `_new_document()`, so the guard is defensive only.

## Menu Structure

**File**: New / Open… / Open Recent ▶ / Close / Save / Save As… / — / Export… / — / Quit

**Edit**: Undo / Redo / — / Cut / Copy / Paste / Select All / — / Expand Selection / Contract Selection / — / Indent / Undent / Comment / Uncomment / — / Find… / Find & Replace…

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

## Open Design Questions (must resolve before implementing affected subsystems)

1. ~~How is geometry provenance tracked through Manifold CSG operations?~~ Resolved: `originalID` + `run_original_id` in `MeshGL`.
2. ~~What is the ID system for pickable geometry elements in the viewport?~~ Resolved: `originalID` is the pick ID; ray-cast hit triangle → `run_original_id` lookup.
3. ~~How does drag-to-edit decide which AST expression to modify when multiple are candidates?~~ Resolved: tool choice (Translate/Rotate/Scale) declares the transform type; find innermost existing wrapper or insert new one outside existing wrappers.
4. ~~How is user "intent" inferred from code structure (e.g., named parameters vs. positional)?~~ Resolved: literal → rewrite in place; variable = literal → update declaration; variable = expression or inline expression → append delta. See Source Rewrite Rules above.
5. ~~Full rebuild vs. incremental regeneration on each edit?~~ Resolved: full rebuild for v1; incremental rebuild is a planned future optimization.
