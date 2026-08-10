# WYSIWYG Interaction Design

Detailed design for viewport interaction, selection, and gizmo-driven AST edits. See `CLAUDE.md` for the bidirectional sync overview and the AST ↔ geometry ID mapping pattern.

> ## ⚠ This is a design document, not a description of current behaviour
>
> Parts of it describe the intended end state and are **not implemented**.
> Those are marked inline:
>
> - **`[NOT IMPLEMENTED]`** — nothing in the code does this.
> - **`[DIFFERS]`** — something does this, but not the way described here; the
>   actual behaviour follows.
>
> Everything unmarked was checked against the code and is accurate as of
> BelfrySCAD 0.31.1. Please keep the markers current when implementing or
> changing any of it — unmarked text here reads as fact, and has repeatedly
> been taken as such.

## Camera Controls

| Input | Action |
|---|---|
| Left-button drag | Orbit (Turntable) |
| Shift+left-button drag | Orbit (trackball) |
| Option+left-button drag | Rotate lighting |
| Right-button drag | Pan |
| Scroll wheel | Zoom centered on the cursor (adjusts `$vpd` distance and, since the zoomed-toward point generally isn't `$vpt`, `$vpt` too — see `Viewport._zoom_to_cursor`/`Camera.zoom_to_point`) |
| Shift+scroll wheel | Adjust FOV (adjusts `$vpf`; clamped 1°–120°) |
| Trackpad click+drag | Orbit (Turntable) |
| Trackpad two-finger scroll | Pan (`pixelDelta()` non-null distinguishes trackpad from wheel — see `Viewport.wheelEvent`) |
| Cmd+trackpad two-finger scroll | Zoom |
| Trackpad pinch | Zoom, centered on the cursor (`ZoomNativeGesture`) |
| Trackpad two-finger twist | Roll (`RotateNativeGesture`; value is clockwise-positive on macOS, contrary to Qt's docs) |
| Trackpad two-finger double-tap | View All (`SmartZoomNativeGesture`) |

**Turntable vs orbit (trackball)**: plain left-drag ("Turntable" mode) always keeps world-Z level — horizontal movement spins `Camera.azimuth` around world-Z, vertical movement tilts `Camera.elevation` (clamped ±89° to avoid `_look_at`'s gimbal-lock fallback at the exact pole). Shift+left-drag is a true trackball/arcball rotation (`Camera.orbit_free`) instead: each call re-derives the camera's *own current* up ("vertical") and right ("horizontal") axes from its current eye/target/roll, then rotates the eye around those two axes by the drag delta — horizontal movement orbits around the current up axis, vertical movement around the current right axis — with the target always staying centered in the viewport. Unlike Turntable mode, there is no elevation clamp: the view can tumble continuously through either pole with no jump (the *derived* azimuth number can flip when crossing a pole, which is expected and harmless since azimuth/elevation/roll are all re-derived fresh from the resulting orientation via `Camera._set_from_eye_and_up` rather than accumulated). This composes in the camera's own local frame from call to call, so successive small drags stack the way a physical trackball would, rather than being Euler angles applied against a fixed world frame. `roll` (the third, Y component of `$vpr`, applied via Rodrigues' rotation in `Camera._rolled_up` — verified pixel-for-pixel against real OpenSCAD.app's `--camera`/`$vpr` rendering) is what orbit_free's horizontal drag actually changes when the camera isn't level; it never affects eye position on its own, only the up vector fed to `_look_at`. Any named view preset (Top/Front/Iso/etc., `Viewport.set_view_preset`) resets `roll` to 0 — presets are always level.

**Outer-ring roll**: within Shift+drag, if the drag's current mouse position falls in the outer 20% of the viewport (by radial distance from center, normalized against the inscribed circle — `Viewport._outer_ring_roll_delta_deg`), the drag rolls the view around the target→camera axis like turning a dial rim instead of tilting it: `Camera.roll` changes by the mouse's actual angular sweep around the viewport center, 1:1, in whichever direction makes the on-screen content spin the same way the mouse is dragged (clockwise drag → clockwise-looking roll, from the viewer's own point of view — note this is the *opposite* sign of `Camera.roll` itself, since increasing `roll` rotates the *up vector* clockwise around the view axis, which makes the rendered scene appear to rotate counterclockwise). Falling back to the ordinary trackball tilt inside the inner 80% keeps a single Shift+drag able to both tilt and roll depending on where on the rim you grab it.

## Selection

Command-click triggers:
```
ray cast → hit triangle → run_original_id lookup → AST node → highlight source span in the editor + visual highlight in viewport
```

Command-click always lands on the leaf geometry node (innermost primitive).

**[NOT IMPLEMENTED]** — everything in this subsection below. `_do_selection` ray-casts to one `originalID` and stores it; there is no parent/child navigation, and selecting again simply replaces the selection.

> The selection can be walked up or down the AST hierarchy — up expands to a parent node (e.g. `cube()` → enclosing `translate()` → `difference()`), highlighting the entire subtree's geometry and the corresponding source span; down moves back toward the leaf.
>
> When walking down from a node with multiple children, select the child whose geometry is closest to the original ray-cast hit point.
>
> Multiple objects can be selected only as a complete subtree — walking up to a parent selects all its children as a unit. Arbitrary disjoint selections are not supported.

**[DIFFERS]** Selected objects are tinted green, not outlined — the fallback below is what shipped; no stencil-buffer outline exists (`SceneRenderer._highlight_color`, applied per-buffer in the draw loop).

> Original intent: outlined via a stencil buffer technique, falling back to mesh tinting if outline rendering proved too expensive.

Selecting a shape reveals the transform tool buttons (Translate, Rotate, Scale). They are **in the viewport**, stacked down its left edge under the perspective toggle — not on the main toolbar, where they first shipped: a tool that only applies to a selection reads better next to the selection than in a strip that is mostly view and render commands. `Viewport._tool_btns` is a dict of three checkable `QToolButton`s; `_sync_tool_buttons()` shows or hides them with the selection and disarms the active tool when the selection goes away, so no gizmo is ever left drawn over nothing.

Only one runs at a time, and clicking the running one turns it off (`Viewport._active_tool`, `-1` for none).

## Measurement

Two viewport measuring tools, toggled from the toolbar and mutually exclusive (a `QActionGroup` with `ExclusionPolicy.ExclusiveOptional`, so both may be off). Both are disabled whenever there is no geometry to measure — `MainWindow._update_measure_actions_enabled()`.

| Tool | Picks | Reports |
|---|---|---|
| Linear | Two points | Distance between them |
| Angle | Three points | Angle at the second, the vertex |

Each click ray-casts to a surface point (`SceneRenderer.ray_cast_point`), then `snap_at()`/`choose_snap()` snap it to a vertex or feature edge if one is near in *screen* space, falling back to the raw surface point. Priority is vertex, then edge, then face.

The edges offered are filtered by `feature_edges_of_triangle()`, which keeps an edge only if it is a boundary or its dihedral angle is sharp. Without that, a flat square face — two triangles — would offer its triangulation diagonal as a snap target, which moves when `$fn` changes and means nothing.

Finished measurements draw as overlay lines (`Viewport.upload_lines`) with a `_MeasureLabel` per measurement. A label is dismissed by clicking it; Escape peels one measurement at a time rather than clearing them all.

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

**[DIFFERS]** in step 1 only: the search is a regex over the raw source text immediately preceding the node, not an AST walk — so it matches only a literal `translate([a, b, c])`-shaped wrapper, and misses any spelled differently (named arguments, a 2-element vector, a wrapper separated by a comment). Steps 2 and 3 are accurate. See the Source Rewrite Rules section for what "update" actually does.

## Value Overlay

During translate/rotate/scale, a text readout of the current value is shown in the viewport (`Viewport._delta_label`, bottom-centre).

**[NOT IMPLEMENTED]** — the rest of this subsection. `_delta_label` is a read-only `QLabel`: it displays, it cannot be typed into or focused, and there is no commit/cancel path through it. (The same label is reused by data-viewer vertex drags to name the constrained plane.)

> The user can type an exact value instead of dragging; committing applies the same source rewrite rules as a drag commit.
>
> Enter commits; Escape cancels and reverts to the pre-interaction state. The ghost mesh updates on commit (Enter), not while typing. The text field only gets focus on click — no auto-focus on drag-start.
>
> Displayed value follows the source rewrite classification: absolute value for a literal number or a bare variable set to a number; delta for an expression.

## Transform Edit Rules

- **Nested transforms of the same type**: modify the innermost matching wrapper. The regex is anchored to the text immediately preceding the node, so the innermost is the only one it can match.
- **Transform composition order** — **[DIFFERS]**. A new wrapper is inserted immediately before the selected node, i.e. *inside* any existing transform wrappers, not outside them (`source[:start] + insert + source[start:]`).
  > Original intent: new wrappers are always inserted outside any existing transform wrappers on the selected node.
- **Live drag preview** — **[NOT IMPLEMENTED]**. There is no ghost mesh; only the delta readout updates during a drag. The AST edit and re-render still happen on mouse-up, as one undo step.
  > Original intent: wireframe ghost copy of the mesh during drag.
- **Gizmo orientation** — **[DIFFERS]**. Handles are world-axis aligned (`Viewport._AXIS_DIRS` is the identity basis), positioned at the selection's bounding-box centre. A rotated object gets world-aligned handles, not ones following its own frame.
  > Original intent: handles drawn in local (post-transform) space.

## Source Rewrite Rules (Intent Preservation)

**[NOT IMPLEMENTED]** as described. What ships is a single regex merge with no intent classification at all — see `MainWindow._on_translate_committed` / `_on_rotate_committed` / `_on_scale_committed`:

| What precedes the node | Actual behaviour |
|---|---|
| A matching wrapper whose components are **all plain numeric literals** | Add the delta and rewrite **all three** components via `f"{v:.4g}"` |
| Anything else — no wrapper, or one containing a variable or expression | Insert a **new** wrapper immediately before the node |

Two consequences: components are reformatted even when unchanged (`1.500` → `1.5`, `1e3` → `1000`), and expression-positioned geometry accumulates nested transforms rather than having its expression edited.

Nothing reads or writes a variable's declaration site. Note the reformatting is now fixable without the classification below: `openscad_cpp_evaluator.parse_ast()` (≥0.16.0) exposes every node's `start_offset`/`end_offset`, so original number text can be sliced and reused verbatim — and the existing regex already captures each component's text in its match groups.

> Original intent — a drag commit rewrites the minimum source text based on the transform argument's form:
>
> | Argument form | Rewrite strategy |
> |---|---|
> | Literal value (`[10, 0, 0]`) | Replace the affected component(s) in place; preserve named vs. positional style |
> | Variable set to a literal (`x = 10`) | Update the literal at the variable's declaration site |
> | Variable set to an expression (`x = base/2`) | Append a delta at the declaration site: `x = base/2 + 5` |
> | Inline expression (`[base/2, 0, 0]`) | Append a delta inline: `[base/2 + 5, 0, 0]` |
>
> Editing a variable declaration affects all sites referencing it — intentional, preserving the user's parametric relationships.
