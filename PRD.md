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

# 3. 🧱 System Architecture

## 3.1 Core pipeline

```text
Source Code
   ↓
QScintilla Editor (text layer)
   ↓
openscad_parser (strict PEG AST)
   ↓
AST evaluation layer
   ↓
Manifold CSG engine
   ↓
Mesh generation
   ↓
ModernGL renderer
   ↓
PySide6 UI
```

---

## 3.2 Key architectural constraint

* Parser is **strict (non-tolerant)**
* AST only exists for valid code
* System must remain functional without AST

Fallback behavior:

* last-known-good AST is cached
* last-known-good geometry is displayed when parsing fails

---

# 4. 🧩 Key Components

---

## 4.1 Code Editor

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

## 4.2 Parser / AST System

### Technology:

* openscad_parser

### Responsibilities:

* strict parsing of OpenSCAD-like language
* AST generation with file/line/column/span metadata
* failure on invalid syntax (by design)

### Constraints:

* no partial AST output
* no recovery mode in v1

---

## 4.3 Geometry Kernel

### Technology:

* Manifold

### Responsibilities:

* CSG operations (union, difference, intersection)
* mesh generation from AST evaluation output
* high-performance boolean modeling

### Provenance tracking:

Each geometry-producing AST node is assigned a unique `originalID`. Manifold preserves these IDs through boolean operations via the `MeshGL` output structure (`run_original_id`, `run_index`). The application maintains a lookup table of `originalID → AST node`, rebuilt on every successful evaluation cycle.

---

## 4.4 Rendering System

### Technology:

* ModernGL

### Responsibilities:

* GPU mesh rendering
* camera controls
* selection ray casting
* visual feedback for selection and highlighting
* ghost mesh rendering during drag operations

---

## 4.5 UI Framework

### Technology:

* PySide6

### Responsibilities:

* windowing system
* layout management
* docking panels
* editor + 3D viewport integration
* transform toolbar (Translate, Rotate, Scale)
* value overlay during transform operations

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
│  Console                                                      │  ← always visible
└──────────────────────────────────────────────────────────────┘
```

* **Toolbar**: Open, Render, Export, Undo, Redo across the top
* **Tabs**: one per open file; tabs can be torn off into separate windows
* **Code editor**: left pane (QScintilla)
* **3D viewport**: center pane; cube gizmo overlaid in a corner for view angle control; always visible
* **Tools strip**: narrow panel to the right of the viewport — Translate, Rotate, Scale, and future tools
* **Console**: pane at the bottom

All panels except the 3D viewport (toolbar, tabs, code editor, tools strip, console) are individually hideable via the View menu.

### Menus:

**File**: New / Open… / Open Recent ▶ / Close / Save / Save As… / — / Export… / — / Quit

**Edit**: Undo / Redo / — / Cut / Copy / Paste / Select All / — / Indent / Undent / Comment / Uncomment / — / Find… / Find & Replace…

**Design**: Render / — / Insert Primitive ▶ (Cube, Sphere, Cylinder, Cone, …) / Boolean Operation ▶ (Union, Difference, Intersection)

**View**:
* Show Toolbar / Show Tab Bar / Show Code Editor / Show Tools Strip / Show Console
* —
* Top / Bottom / Left / Right / Front / Back / Isometric / View All
* —
* Show Axes / Show Edges / Show Scale Markers / Show Crosshairs

**Window**: Minimize / Zoom / — / Move Tab to New Window / — / *(open document list)* / Bring All to Front

---

# 5. 🧠 Core Interaction Model

## 5.1 Primary loop

```text
User edits code
   ↓
Parse (if valid)
   ↓
Full rebuild: evaluate AST → Manifold CSG → mesh
   ↓
Render updated model
```

Every successful parse triggers a full rebuild. Incremental evaluation is a planned future optimization.

---

## 5.2 WYSIWYG interaction

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

Selecting a shape enables the transform toolbar.

### Transform tools

When a tool is active, axis handles are drawn over the selected shape in local (post-transform) space. A ghost copy of the mesh is displayed during drag; the AST edit is committed on mouse-up.

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

## 5.3 Invalid state handling

When parsing fails:

* keep last valid AST
* keep last valid geometry visible
* allow editing to continue
* do not block UI

---

# 6. 🔑 Key Design Requirements

---

## 6.1 Code ↔ Geometry mapping

* Every geometry-producing AST node is assigned a unique `originalID` (allocated via Manifold's `ReserveIDs`)
* Manifold preserves provenance through CSG operations via `run_original_id` in `MeshGL` output
* The application maintains an `originalID → AST node` lookup table, rebuilt on each successful evaluation

---

## 6.2 Stability under invalid code

* UI must never break when code is invalid
* viewport must always display something meaningful
* system must degrade gracefully

---

## 6.3 Deterministic regeneration

* AST → geometry must be reproducible
* no hidden state in rendering layer
* full rebuild on every successful parse (no incremental evaluation in v1)

---

## 6.4 Performance target (v1)

* model regeneration: interactive (<200ms typical small/medium models)
* viewport: 60 FPS target
* parsing: fast enough for "pause-to-compile" workflow

---

# 7. 🚧 Known Challenges

## 7.1 Strict parser limitation

* no AST during invalid code states
* **Resolution**: cache last-known-good AST and geometry; display cached geometry while code is invalid; never block the UI

---

## 7.2 Bidirectional editing complexity

* mapping geometry edits → AST changes
* preserving user intent (not just numeric edits)
* **Resolution**: tool choice (Translate/Rotate/Scale) declares the transform type; source rewrite rules preserve intent based on argument form (literal, variable, expression)

---

## 7.3 CSG provenance tracking

* tracking which AST node produced which mesh faces, especially after boolean operations
* **Resolution**: Manifold's `originalID` / `run_original_id` system in `MeshGL` output; application maintains `originalID → AST node` table

---

## 7.4 UI/semantic separation

* editor is not semantic source of truth
* AST is not always present
* geometry must remain stable independently
* **Resolution**: editor is text-only (QScintilla); AST drives all semantics when valid; geometry is cached independently of both

---

# 8. 🧪 Future Extensions (out of scope for v1)

* incremental parsing / tolerant AST
* incremental geometry evaluation (full rebuild used in v1)
* constraint solving system
* collaborative editing
* node-based visual programming mode
* plugin system for CAD primitives
* GPU compute acceleration for CSG

---

# 9. 🧭 Product Philosophy

* Code-first, but not code-only
* Geometry is interactive, not passive
* AST is authoritative when valid
* System must remain usable in broken states
* User intent is more important than textual correctness

---

# 10. 📌 Open Questions

1. ~~How is geometry provenance tracked through Manifold operations?~~ **Resolved**: `originalID` assigned per AST node; `run_original_id` in `MeshGL` tracks provenance through boolean ops.
2. ~~What is the ID system for pickable geometry elements?~~ **Resolved**: `originalID` is the pick ID; ray-cast hit triangle → `run_original_id` lookup → AST node.
3. ~~How does drag-to-edit choose which AST expression to modify?~~ **Resolved**: tool choice (Translate/Rotate/Scale) declares the transform type; find innermost existing wrapper or insert new one outside existing wrappers.
4. ~~How is "intent" inferred from code structure?~~ **Resolved**: literal → rewrite in place; variable = literal → update declaration; variable or inline expression → append delta.
5. ~~Should regeneration be full rebuild or incremental?~~ **Resolved**: full rebuild for v1; incremental evaluation is a planned future optimization.
