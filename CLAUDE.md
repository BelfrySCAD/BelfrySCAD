# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NeuSCAD is a hybrid procedural CAD application combining OpenSCAD-style script-based modeling with live WYSIWYG 3D interaction. The defining feature is **bidirectional synchronization** between source code and 3D geometry — users can edit code or drag geometry, and both views stay in sync.

**Status**: Pre-development (specification phase). The full design is in `PRD.md`.

## Planned Technology Stack

- **UI Framework**: PySide6 (Qt)
- **Code Editor**: QScintilla (text layer only — not semantically aware)
- **Parser**: openscad_parser (strict PEG-based, generates AST with file/line/col/span metadata; parses full OpenSCAD syntax but has no knowledge of built-in functions or modules — the evaluator layer implements all built-ins)
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

Parse errors are indicated in the editor with a squiggly underline at the error location. QScintilla supports this natively via its indicator API (`INDIC_SQUIGGLE`). Errors are also reported in the console.

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

## Manifold API: Geometry Provenance

Manifold tracks geometry provenance through CSG operations via the `MeshGL` output structure. The key fields after any boolean op:

| Field | Meaning |
|---|---|
| `run_original_id` | Array of source mesh IDs, one per triangle run |
| `run_index` | Boundaries of runs in the triangle array |
| `face_id` | Which source triangle each output triangle derives from |

Each Manifold body constructed from scratch receives a unique auto-incremented `originalID`. After a CSG boolean (e.g., `body1 - body2`), output triangles are organized into **runs** tagged with the `originalID` of their contributing input body. Use `ReserveIDs(n)` to pre-allocate a contiguous block of IDs for complex sub-assemblies.

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

mesh = result.to_mesh()             # MeshGL output
mesh.run_original_id                # numpy array: source ID per run
mesh.run_index                      # numpy array: run boundaries
mesh.face_id                        # numpy array: source face per triangle (optional)
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

## V1 Scope Boundaries

**In scope**: Script editing, real-time 3D rendering, basic WYSIWYG drag interaction, CSG operations, graceful invalid-code handling.

**Explicitly out of scope for v1**: Constraint solver, collaborative editing, cloud modeling, incremental/tolerant parsing, node-based visual programming, plugin system.

## Undo/Redo

Both code edits and gizmo drags are undo/redo-able. QScintilla handles code edit history natively; gizmo commits must be pushed to a separate application-level undo stack. Undo/redo applies across both.

## Console Output

The console displays:
- Parse errors (with file/line/col from AST metadata)
- On each render: bounding box of the resulting mesh and current camera position

## Keyboard Shortcuts

Standard platform conventions apply throughout. Custom shortcuts:

| Key | Action |
|---|---|
| Cmd+4 | Top view |
| Cmd+5 | Bottom view |
| Cmd+6 | Left view |
| Cmd+7 | Right view |
| Cmd+8 | Front view |
| Cmd+9 | Back view |
| Cmd+0 | Isometric view |
| F6 | Render |

## Application Preferences

- **Font size**: editor font size
- **Viewport background color**: the background color of the 3D display
- **Editor theme**: syntax highlighting color scheme for QScintilla

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
│  Console                                                      │  ← always visible
└──────────────────────────────────────────────────────────────┘
```

- **Toolbar**: traditional bar across the top — Open, Render, Export, Undo, Redo
- **Tabs**: one per open file; tabs can be torn off into separate windows
- **Code editor**: left pane (QScintilla)
- **3D viewport**: center pane; cube gizmo in a corner for view angle control; always visible
- **Tools strip**: narrow panel to the right of the viewport — Translate, Rotate, Scale, and future tools
- **Console**: pane at the bottom

All panels except the 3D viewport (toolbar, tabs, code editor, tools strip, console) are individually hideable via the View menu.

## Menu Structure

**File**: New / Open… / Open Recent ▶ / Close / Save / Save As… / — / Export… / — / Quit

**Edit**: Undo / Redo / — / Cut / Copy / Paste / Select All / — / Expand Selection / Contract Selection / — / Indent / Undent / Comment / Uncomment / — / Find… / Find & Replace…

**Design**: Render / — / Insert Primitive ▶ (Cube, Sphere, Cylinder, Cone, …) / Boolean Operation ▶ (Union, Difference, Intersection)

**View**:
- Show Toolbar / Show Tab Bar / Show Code Editor / Show Tools Strip / Show Console
- —
- Top / Bottom / Left / Right / Front / Back / Isometric / View All
- —
- Show Axes / Show Edges / Show Scale Markers / Show Crosshairs

**Window**: Minimize / Zoom / — / Move Tab to New Window / — / *(open document list)* / Bring All to Front

## Open Design Questions (must resolve before implementing affected subsystems)

1. ~~How is geometry provenance tracked through Manifold CSG operations?~~ Resolved: `originalID` + `run_original_id` in `MeshGL`.
2. ~~What is the ID system for pickable geometry elements in the viewport?~~ Resolved: `originalID` is the pick ID; ray-cast hit triangle → `run_original_id` lookup.
3. ~~How does drag-to-edit decide which AST expression to modify when multiple are candidates?~~ Resolved: tool choice (Translate/Rotate/Scale) declares the transform type; find innermost existing wrapper or insert new one outside existing wrappers.
4. ~~How is user "intent" inferred from code structure (e.g., named parameters vs. positional)?~~ Resolved: literal → rewrite in place; variable = literal → update declaration; variable = expression or inline expression → append delta. See Source Rewrite Rules above.
5. ~~Full rebuild vs. incremental regeneration on each edit?~~ Resolved: full rebuild for v1; incremental rebuild is a planned future optimization.
