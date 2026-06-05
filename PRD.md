# 📄 Product Requirements Document (PRD)

## Project: Hybrid OpenSCAD + WYSIWYG Procedural CAD System

---

# 1. 🧭 Overview

This project is a cross-platform procedural CAD application combining:

* OpenSCAD-style script-based modeling
* Live 3D WYSIWYG interaction
* Bidirectional synchronization between code and geometry

It enables users to:

* Write parametric CAD models in code
* Directly manipulate 3D geometry
* Have those manipulations reflected back into source code

Core principle:

> The AST is the source of truth, not the rendered geometry.

---

# 2. 🎯 Goals

## 2.1 Primary goals

* Script-first parametric CAD modeling
* Real-time 3D visualization of models
* Interactive manipulation of geometry
* Code ↔ geometry synchronization (bidirectional intent preservation)
* Stable UX even when code is invalid

---

## 2.2 Secondary goals

* Fast iteration loop (edit → view → modify → reflect back)
* Support for complex CSG operations
* Maintain OpenSCAD-like syntax compatibility
* Extensible architecture for future CAD features

---

## 2.3 Non-goals (explicit exclusions for v1)

* Full B-rep CAD kernel replacement
* Constraint solver system (like SolidWorks)
* Real-time collaborative editing (future)
* Cloud-based modeling (future)
* Full IDE replacement

---

# 3. 📁 File Format & Export

* **File format**: `.scad` (OpenSCAD-compatible plain text)
* **Language**: Full OpenSCAD language — variables, functions, modules, loops, conditionals, all built-in primitives and transforms
* **Export formats**: STL, OBJ, 3MF
* **STEP**: under investigation — Manifold produces triangle meshes; STEP is a B-rep format, so any STEP output would be a faceted solid with limited downstream CAD value
* **Export workflow**: if no current render exists, Export triggers a render automatically before exporting

---

# 4. 🧱 System Architecture

## 4.1 Core pipeline

```text
Source Code
   ↓
QScintilla Editor (text layer)
   ↓
  [on Render trigger]
   ↓
openscad_parser (strict PEG AST)
   ↓
AST evaluation layer
   ↓
Manifold CSG engine (full boolean evaluation)
   ↓
Watertight mesh
   ↓
ModernGL renderer
   ↓
PySide6 UI
```

Render triggers: explicit Render action, file open, file save, gizmo commit. The viewport shows the last render result while the user edits; it does not update live.

---

## 4.2 Error display

Parse errors are indicated in the editor with a squiggly underline at the error location (QScintilla `INDIC_SQUIGGLE`). Errors are also reported in the console.

## 4.3 Key architectural constraint

* Parser is **strict (non-tolerant)**
* AST only exists for valid code
* System must remain functional without AST

Fallback behavior:

* last-known-good AST is cached
* last-known-good geometry is displayed when parsing fails

---

# 5. 🧩 Key Components

---

## 5.1 Code Editor

### Technology:

* QScintilla

### Responsibilities:

* text editing
* syntax highlighting
* line numbering
* code folding (basic or default)
* user input surface

### Non-responsibilities:

* semantic understanding
* AST generation
* geometry awareness

---

## 5.2 Parser / AST System

### Technology:

* openscad_parser

### Responsibilities:

* strict parsing of full OpenSCAD language syntax
* AST generation with file/line/column/span metadata
* failure on invalid syntax (by design)

### Constraints:

* no partial AST output
* no recovery mode in v1
* no knowledge of built-in functions or modules — treats `cube()`, `translate()`, etc. as generic calls; the evaluator layer is responsible for implementing all OpenSCAD built-ins

---

## 5.3 AST Evaluator

### Responsibilities:

* Recursive AST walker — sits between openscad_parser and Manifold
* Calls `build_scopes()` on the parsed AST to get scope annotations; uses `scope.lookup_variable()`, `scope.lookup_function()`, `scope.lookup_module()` for all name resolution
* Evaluates all expressions (arithmetic, ternary, list comprehensions, etc.)
* Dispatches built-in module calls to a built-ins table; recursively evaluates user-defined modules
* Evaluates default parameter values in caller's scope

### Built-ins implemented by the evaluator:

* **Primitives** (→ Manifold bodies): `cube`, `sphere`, `cylinder`, `cone`, `polyhedron`
* **Transforms**: `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `resize`, `color`, `hull`, `minkowski`
* **Booleans**: `union`, `difference`, `intersection`
* **Control / utility**: `for`, `let`, `if`/`else`, `echo`, `assert`, `children()`, `$children`
* **Special variables**: `$fn`, `$fa`, `$fs` — control mesh resolution; defaults: `$fn=0`, `$fa=12`, `$fs=2`

### Outputs:

* Manifold mesh (result of full CSG evaluation)
* `originalID → AST node` lookup table (built during evaluation)
* Per-body color information (from `color()` propagation)
* Error messages (parse errors and runtime errors → console)

### Open questions:

* Runtime error handling: abort evaluation or report to console and keep last-valid geometry?
* `include` vs `use` semantics for external `.scad` files

---

## 5.4 Geometry Kernel

### Technology:

* Manifold

### Responsibilities:

* CSG operations (union, difference, intersection)
* mesh generation from AST evaluation output
* high-performance boolean modeling

### Provenance tracking:

Each geometry-producing AST node is assigned a unique `originalID`. Manifold preserves these IDs through boolean operations via the `MeshGL` output structure (`run_original_id`, `run_index`). The application maintains a lookup table of `originalID → AST node`, rebuilt on every render trigger.

---

## 5.5 Rendering System

### Technology:

* ModernGL

### Responsibilities:

* GPU mesh rendering
* camera controls
* selection ray casting
* visual feedback for selection and highlighting
* ghost mesh rendering during drag operations
* color rendering — OpenSCAD's `color()` function affects viewport display; color cascades to all children in the subtree and is passed from the evaluator to the renderer

---

## 5.6 UI Framework

### Technology:

* PySide6

### Responsibilities:

* windowing system
* layout management
* docking panels
* editor + 3D viewport integration
* transform toolbar (Translate, Rotate, Scale)
* value overlay during transform operations

### Undo/Redo:

Both code edits and gizmo drags are undo/redo-able via a unified app-level undo stack. Code edit entries call through to QScintilla's native undo; gizmo op entries wrap their source rewrite in QScintilla's `beginUndoAction()` / `endUndoAction()` to mark it as a single unit. All Cmd+Z / Cmd+Shift+Z goes through the app stack.

### Console output:

* Parse errors (with file/line/col location)
* On each render: bounding box of the resulting mesh and current camera position

### Keyboard Shortcuts:

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

### Application Preferences:

* **Font size**: editor font size
* **Viewport background color**: background color of the 3D display
* **Editor theme**: syntax highlighting color scheme for QScintilla

### Startup:

Opens with a single blank untitled document.

### Layout:

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

* **Toolbar**: Open, Render, Export, Undo, Redo across the top
* **Tabs**: one per open file; tabs can be torn off into separate windows
* **Code editor**: left pane (QScintilla)
* **3D viewport**: center pane; always visible; contains:
  * Cube gizmo in a corner for view angle control
  * Small camera icon adjacent to the cube gizmo; clicking it opens a popup showing the current viewport translation, rotation, distance, and FOV
* **Tools strip**: narrow panel to the right of the viewport — Translate, Rotate, Scale, and future tools
* **Console**: pane at the bottom
* **Status bar**: thin strip at the very bottom; displays the 3D coordinates of the last clicked point on the mesh

All panels except the 3D viewport (toolbar, tabs, code editor, tools strip, console, status bar) are individually hideable via the View menu. Scale markers are tick marks along the viewport axes showing distance units.

### Menus:

**File**: New / Open… / Open Recent ▶ / Close / Save / Save As… / — / Export… / — / Quit

**Edit**: Undo / Redo / — / Cut / Copy / Paste / Select All / — / Expand Selection / Contract Selection / — / Indent / Undent / Comment / Uncomment / — / Find… / Find & Replace…

**Design**: Render / — / Insert Primitive ▶ (Cube, Sphere, Cylinder, Cone, …) / Boolean Operation ▶ (Union, Difference, Intersection) *(behavior of Insert Primitive and Boolean Operation deferred)*

**View**:
* Show Toolbar / Show Tab Bar / Show Code Editor / Show Tools Strip / Show Console
* —
* Top / Bottom / Left / Right / Front / Back / Isometric / View All
* —
* Show Axes / Show Edges / Show Scale Markers / Show Crosshairs / Show Status Bar

**Window**: Minimize / Zoom / — / Move Tab to New Window / — / *(open document list)* / Bring All to Front

---

# 6. 🧠 Core Interaction Model

## 6.1 Primary loop

```text
Render trigger (Render action / file open / file save / gizmo commit)
   ↓
Parse → full Manifold CSG evaluation → watertight mesh → ModernGL viewport
```

There is no live preview. The viewport shows the last render result while the user edits code.

---

## 6.2 WYSIWYG interaction

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

Command-click triggers selection via ray cast:

```text
ray cast → hit triangle → run_original_id lookup → AST node
   ↓
Highlight source span in editor + visual highlight in viewport
```

Command-click always lands on the leaf geometry node. The selection can be walked up or down the AST hierarchy:

* **Up**: expands to the parent node; editor and viewport highlight the full subtree
* **Down**: selects the child whose geometry is closest to the original ray-cast hit point

Multiple objects can be selected, but only as a complete subtree — walking up to a parent node selects all its children as a unit. Selecting arbitrary disjoint objects is not supported.

Selected objects are highlighted with an outline (stencil buffer technique). If outline rendering proves too expensive, fall back to mesh tinting.

Selecting a shape enables the transform toolbar.

### Transform tools

When a tool is active, axis handles are drawn over the selected shape in local (post-transform) space. A wireframe ghost copy of the mesh is displayed during drag; the AST edit is committed on mouse-up.

| Tool | Handle | AST effect |
|---|---|---|
| Translate | Arrow per axis | Modify/insert `translate([x,y,z])` wrapper |
| Rotate | Arc per axis | Modify/insert `rotate(...)` wrapper |
| Scale | Handle per axis | Modify/insert `scale([x,y,z])` wrapper |
| Scale (Shift+drag) | Any handle | Scale all three components uniformly |

The tool choice declares which transform type to modify — no intent inference needed. The system searches for an existing wrapper of the matching type immediately enclosing the selected node; if found it updates the argument, if not it inserts a new wrapper outside any existing transform wrappers.

For nested wrappers of the same type, the innermost is modified.

### Value overlay

During any transform operation a numeric readout is displayed in the viewport. The displayed value is:

* **Absolute** when the argument is a literal number or a bare variable set to a number
* **Delta** when the argument is an expression

The text field only receives focus when clicked. Enter commits; Escape cancels. The ghost mesh updates on commit, not while typing.

### Source rewrite rules

When a drag commits, the system rewrites the minimum necessary source text:

| Argument form | Rewrite strategy |
|---|---|
| Literal value | Replace the affected component(s) in place; preserve named vs. positional style |
| Variable set to a literal | Update the literal at the variable's declaration site |
| Variable set to an expression | Append a delta at the declaration site |
| Inline expression | Append a delta inline |

Editing a variable declaration intentionally affects all sites that reference it, preserving parametric relationships.

---

## 6.3 Invalid state handling

When parsing fails:

* keep last valid AST
* keep last valid geometry visible
* allow editing to continue
* do not block UI

---

# 7. 🔑 Key Design Requirements

---

## 7.1 Code ↔ Geometry mapping

* Every geometry-producing AST node is assigned a unique `originalID` (allocated via Manifold's `ReserveIDs`)
* Manifold preserves provenance through CSG operations via `run_original_id` in `MeshGL` output
* The application maintains an `originalID → AST node` lookup table, rebuilt on each successful evaluation

---

## 7.2 Stability under invalid code

* UI must never break when code is invalid
* viewport must always display something meaningful
* system must degrade gracefully

---

## 7.3 Deterministic regeneration

* AST → geometry must be reproducible
* no hidden state in rendering layer
* full Manifold rebuild on every render trigger (no incremental evaluation in v1)

---

## 7.4 Performance target (v1)

* model regeneration: interactive (<200ms typical small/medium models)
* viewport: 60 FPS target

---

# 8. 🚧 Known Challenges

## 8.1 Strict parser limitation

* no AST during invalid code states
* **Resolution**: cache last-known-good AST and geometry; display cached geometry while code is invalid; never block the UI

---

## 8.2 Bidirectional editing complexity

* mapping geometry edits → AST changes
* preserving user intent (not just numeric edits)
* **Resolution**: tool choice (Translate/Rotate/Scale) declares the transform type; source rewrite rules preserve intent based on argument form (literal, variable, expression)

---

## 8.3 CSG provenance tracking

* tracking which AST node produced which mesh faces, especially after boolean operations
* **Resolution**: Manifold's `originalID` / `run_original_id` system in `MeshGL` output; application maintains `originalID → AST node` table

---

## 8.4 UI/semantic separation

* editor is not semantic source of truth
* AST is not always present
* geometry must remain stable independently
* **Resolution**: editor is text-only (QScintilla); AST drives all semantics when valid; geometry is cached independently of both

---

# 9. 🧪 Future Extensions (out of scope for v1)

* incremental parsing / tolerant AST
* incremental geometry evaluation (full rebuild used in v1)
* constraint solving system
* collaborative editing
* node-based visual programming mode
* plugin system for CAD primitives
* GPU compute acceleration for CSG

---

# 10. 🧭 Product Philosophy

* Code-first, but not code-only
* Geometry is interactive, not passive
* AST is authoritative when valid
* System must remain usable in broken states
* User intent is more important than textual correctness

---

# 11. 📌 Open Questions

1. ~~How is geometry provenance tracked through Manifold operations?~~ **Resolved**: `originalID` assigned per AST node; `run_original_id` in `MeshGL` tracks provenance through boolean ops.
2. ~~What is the ID system for pickable geometry elements?~~ **Resolved**: `originalID` is the pick ID; ray-cast hit triangle → `run_original_id` lookup → AST node.
3. ~~How does drag-to-edit choose which AST expression to modify?~~ **Resolved**: tool choice (Translate/Rotate/Scale) declares the transform type; find innermost existing wrapper or insert new one outside existing wrappers.
4. ~~How is "intent" inferred from code structure?~~ **Resolved**: literal → rewrite in place; variable = literal → update declaration; variable or inline expression → append delta.
5. ~~Should regeneration be full rebuild or incremental?~~ **Resolved**: full rebuild for v1; incremental evaluation is a planned future optimization.
