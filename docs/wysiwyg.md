# WYSIWYG Interaction Design

Detailed design for viewport interaction, selection, and gizmo-driven AST edits. See `CLAUDE.md` for the bidirectional sync overview and the AST ↔ geometry ID mapping pattern.

## Camera Controls

| Input | Action |
|---|---|
| Left-button drag | Orbit |
| Option+left-button drag | Rotate lighting |
| Right-button drag | Pan |
| Scroll wheel | Zoom centered on the cursor (adjusts `$vpd` distance and, since the zoomed-toward point generally isn't `$vpt`, `$vpt` too — see `Viewport._zoom_to_cursor`/`Camera.zoom_to_point`) |
| Shift+scroll wheel | Adjust FOV (adjusts `$vpf`; clamped 1°–120°) |
| Trackpad click+drag | Orbit |
| Trackpad two-finger scroll | Pan |
| Trackpad pinch | Zoom |

## Selection

Command-click triggers:
```
ray cast → hit triangle → run_original_id lookup → AST node → highlight source span in QScintilla + visual highlight in viewport
```

Command-click always lands on the leaf geometry node (innermost primitive). The selection can be walked up or down the AST hierarchy — up expands to a parent node (e.g. `cube()` → enclosing `translate()` → `difference()`), highlighting the entire subtree's geometry and the corresponding source span; down moves back toward the leaf.

When walking down from a node with multiple children, select the child whose geometry is closest to the original ray-cast hit point.

Multiple objects can be selected only as a complete subtree — walking up to a parent selects all its children as a unit. Arbitrary disjoint selections are not supported.

Selected objects are outlined (stencil buffer technique); fall back to mesh tinting if outline rendering proves too expensive.

Selecting a shape enables the transform toolbar (Translate, Rotate, Scale, and future tools).

## Transform Gizmos

When a tool is active, axis handles are drawn over the selected shape. Dragging a handle edits the AST directly:

| Tool | Handle | AST effect |
|---|---|---|
| Translate | Arrow per axis | Modify/insert `translate([x,y,z])` wrapper |
| Rotate | Arc per axis | Modify/insert `rotate(...)` wrapper |
| Scale | Handle per axis | Modify/insert `scale([x,y,z])` wrapper |
| Scale (Shift+drag) | Any axis handle | Scale all three components uniformly |

## How Tool Choice Resolves Edit Ambiguity

The active tool declares which transform type to edit — no intent inference needed. For each tool activation on a selected node:

1. Search the AST for an existing transform wrapper of the matching type immediately enclosing the selected node
2. If found: update its vector argument via a **targeted source span replacement** (not full code regeneration)
3. If not found: insert a new wrapper around the selected node's source span

## Value Overlay

During translate/rotate/scale, a text readout of the current value is shown in the viewport. The user can type an exact value instead of dragging; committing applies the same source rewrite rules as a drag commit.

Enter commits; Escape cancels and reverts to the pre-interaction state. The ghost mesh updates on commit (Enter), not while typing. The text field only gets focus on click — no auto-focus on drag-start.

Displayed value follows the source rewrite classification: absolute value for a literal number or a bare variable set to a number; delta for an expression.

## Transform Edit Rules

- **Nested transforms of the same type**: modify the innermost matching wrapper.
- **Transform composition order**: new wrappers are always inserted outside any existing transform wrappers on the selected node.
- **Live drag preview**: wireframe ghost copy of the mesh during drag; commit the AST edit and render on mouse-up.
- **Gizmo orientation**: handles drawn in local (post-transform) space.

## Source Rewrite Rules (Intent Preservation)

A drag commit rewrites the minimum source text based on the transform argument's form:

| Argument form | Rewrite strategy |
|---|---|
| Literal value (`[10, 0, 0]`) | Replace the affected component(s) in place; preserve named vs. positional style |
| Variable set to a literal (`x = 10`) | Update the literal at the variable's declaration site |
| Variable set to an expression (`x = base/2`) | Append a delta at the declaration site: `x = base/2 + 5` |
| Inline expression (`[base/2, 0, 0]`) | Append a delta inline: `[base/2 + 5, 0, 0]` |

Editing a variable declaration affects all sites referencing it — intentional, preserving the user's parametric relationships.
