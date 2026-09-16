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

Every path that changes apparent scale calls `_on_zoom_changed()`, which is
what rebuilds screen-space vertex markers so they stay a constant size.
That includes the View menu's Zoom In/Out and their Cmd+]/Cmd+[ shortcuts
(`Viewport.zoom`), which write `cam.distance` directly — the very value
marker size derives from. Trackpad *pan* deliberately does not: it slides
the target without changing scale, and it fires a great many events.
| Click an orientation-cube patch | Swing to that view |
| Drag the orientation cube | Orbit (Turntable) |

**Turntable vs orbit (trackball)**: plain left-drag ("Turntable" mode) always keeps world-Z level — horizontal movement spins `Camera.azimuth` around world-Z, vertical movement tilts `Camera.elevation` (clamped ±89° to avoid `_look_at`'s gimbal-lock fallback at the exact pole). Shift+left-drag is a true trackball/arcball rotation (`Camera.orbit_free`) instead: each call re-derives the camera's *own current* up ("vertical") and right ("horizontal") axes from its current eye/target/roll, then rotates the eye around those two axes by the drag delta — horizontal movement orbits around the current up axis, vertical movement around the current right axis — with the target always staying centered in the viewport. Unlike Turntable mode, there is no elevation clamp: the view can tumble continuously through either pole with no jump (the *derived* azimuth number can flip when crossing a pole, which is expected and harmless since azimuth/elevation/roll are all re-derived fresh from the resulting orientation via `Camera._set_from_eye_and_up` rather than accumulated). This composes in the camera's own local frame from call to call, so successive small drags stack the way a physical trackball would, rather than being Euler angles applied against a fixed world frame. `roll` (the third, Y component of `$vpr`, applied via Rodrigues' rotation in `Camera._rolled_up` — verified pixel-for-pixel against real OpenSCAD.app's `--camera`/`$vpr` rendering) is what orbit_free's horizontal drag actually changes when the camera isn't level; it never affects eye position on its own, only the up vector fed to `_look_at`. Any named view preset (Top/Front/Iso/etc., `Viewport.set_view_preset`) resets `roll` to 0 — presets are always level.

**Outer-ring roll**: within Shift+drag, if the drag's current mouse position falls in the outer 20% of the viewport (by radial distance from center, normalized against the inscribed circle — `Viewport._outer_ring_roll_delta_deg`), the drag rolls the view around the target→camera axis like turning a dial rim instead of tilting it: `Camera.roll` changes by the mouse's actual angular sweep around the viewport center, 1:1, in whichever direction makes the on-screen content spin the same way the mouse is dragged (clockwise drag → clockwise-looking roll, from the viewer's own point of view — note this is the *opposite* sign of `Camera.roll` itself, since increasing `roll` rotates the *up vector* clockwise around the view axis, which makes the rendered scene appear to rotate counterclockwise). Falling back to the ordinary trackball tilt inside the inner 80% keeps a single Shift+drag able to both tilt and roll depending on where on the rim you grab it.

## Axis scale labels

`_render_axis_labels` draws each number as a textured billboard quad. The
pass keeps the depth **test** on — a label behind solid geometry is still
hidden by it — but turns the depth **mask** off.

That matters because a label's texture is mostly empty: the glyphs cover a
fraction of the quad. Blending hid the empty texels, but they still wrote
depth, and this pass runs *before* the background-ghost (`%`) and highlight
(`#`) passes. The whole rectangle therefore punched a depth hole those later
passes were rejected from, so a see-through body came out with a crisp
rectangle of background wherever a number sat in front of it — reported
against BOSL2's `highlight()` example, whose pink cube was cut open by the
scale numbers over it.

Discarding empty texels in `_LABEL_FRAG` is not enough on its own: an
antialiased edge texel is faint but not empty, so it still wrote depth and
still cut its own pixel out. Six pixels survived that way, which is what
sent the fix to the depth mask instead. The discard stays because skipping
empty texels is cheaper than blending nothing.

`test_headless_render.py` renders the same `#` body twice, with and without
scale labels, and fails on any pixel that is body without them and
background with them.

## Orientation cube

A chamfered cube about 45 px across, parked 6 px from the viewport's top-right corner (the widget itself is 58 px, sized to the chamfered cube's farthest vertex rather than a full cube's corner, so the margin is the real gap) (`window/orientation_cube.py`, `OrientationCube`; created only when `Viewport(orientation_cube=True)`, which is the main window — the data-viewer preview viewports leave it off). It mirrors the camera's orientation, including roll, and offers 26 clickable patches: 6 face squares labelled `X+`/`X-`/`Y+`/`Y-`/`Z+`/`Z-`, 12 edge bevels and 8 corner bevels. Clicking one turns it to face the viewer — an edge brings both its faces obliquely into view, a corner all three. `+X`/`+Y`/`+Z` are tinted with the same red/green/blue the axes use, mixed 55% toward the neutral face grey so the label still reads; the negative faces stay grey, mirroring how the axes only colour their positive halves.

**A plain QWidget painted with QPainter, not geometry in the GL scene.** The cube is a fixed-size 2D overlay, and everything it needs is easier and more exact in QPainter: labels are `drawText` rather than a texture per face; clicking is point-in-polygon against the very polygons that were drawn, rather than a colour-picking readback or a ray cast against bevel geometry; and nothing touches the ModernGL context, so there is no GL state to save, restore or get wrong. The projection is orthographic, which is what makes this work as well as it does — under an orthographic projection a square face always projects to a *parallelogram*, so a single affine `QTransform` maps a label's text box onto it exactly.

Two details that are easy to get wrong, and were:

- **Label orientation** must try both directions around each face quad, not just the four rotations of the stored corner order. That order winds outward for three faces and inward for the other three (the `(axis, u, v)` triple flips handedness with the face's sign), so filtering on `det > 0` alone silently dropped the labels on three faces entirely. Among the candidates that read forwards, the one whose "down" edge points most nearly along the face's fixed foot wins: Z- for the four vertical faces, Y- for `Z+`/`Z-`. The foot is fixed in model space rather than "most nearly down the screen" so a label never re-orients itself as the camera swings — that read as the label spinning. The score is by direction, not edge length, or a foreshortened foot edge loses to a longer sideways one at grazing views.
- **Hit-testing runs smallest-target-first** — corners (hit area grown 2.6×), then edges (1.35×), then everything at true size. The bevels are deliberately thin slivers, so their hit areas are grown about their own centres while what gets *painted* stays exactly the shape of the cube. The ordering is the whole point: an edge bevel grown by a third reaches right over the corner triangles at either end of it, so letting edges and corners compete on depth alone made a click aimed dead centre at a corner select the neighbouring edge — corners became *harder* to hit than with no growing at all.

**Click versus drag**: a press commits to nothing. It becomes a snap-to-view on release if the pointer never travelled more than 3 px, or an orbit the moment it does — so a shaky click still snaps, and a drag never also snaps at the end. Dragging uses `Viewport._orbit_turntable`, the same rule and sensitivity as dragging the model itself; the cube is a second handle on one camera, not a second camera.

## View animation

`Viewport.animate_camera_to(azimuth, elevation)` swings the camera over 350 ms on an `InOutCubic` curve rather than cutting. Both the orientation cube and every named View-menu preset (Ctrl+4…Ctrl+0) route through it, so the two ways to reach a view behave identically — and `Viewport.VIEW_PRESETS` holds angles the cube derives independently from its face normals (`orientation_cube.azimuth_elevation_for`), including the same just-shy-of-the-pole dodge for Top/Bottom, so both land on identical numbers.

Azimuth interpolates along the **shortest signed arc** (350° → 10° turns forward through 0, not backwards through 180°), roll interpolates to 0 so a rolled camera levels out on the way, and the animation lands exactly on the requested values rather than on whatever the last eased sample was. Any direct camera input — a viewport drag, a zoom, another cube click — calls `stop_view_animation()` and wins immediately rather than fighting an animation still writing azimuth/elevation underneath it. **View All** is framing rather than an orientation change, so it still applies at once.

## Selection

Command-click triggers:
```
ray cast → hit triangle → run_original_id lookup → AST node → highlight source span in the editor + visual highlight in viewport
```

Command-click always lands on the leaf geometry node (innermost primitive).

**A pick outside the edited file is refused.** An `originalID` maps to the node
that PRODUCED the geometry, which for anything a library builds is a node inside
that library -- including a plain `cube(10)` once BOSL2 is included, since BOSL2
overrides the primitives with its own modules. Those nodes carry byte offsets
into the *library* file, and every consumer here splices at
`node.position.start_offset` in the *user's* buffer: a `translate()` for a node
at offset 85256 of a 59-character script was appended to the end of it, wrapping
nothing. `MainWindow._editable_node_for_id` is the one lookup all of them go
through now, and it returns None unless `position.origin` is the file the
rendered tab is showing (`_tab_owns_origin`, matching `file_path` or the temp
copy the worker parsed, the same pair the coverage overlay matches on).

**With evaluator >=1.21.0 the pick resolves through the call chain.** Each
`id_to_node` entry carries `call_sites`: every frame behind the geometry,
innermost first, library frames included. A single `cuboid()` is 24 of them,
most inside BOSL2's own `attachable`/`_attach_transform` layers.

`_selectable_span_for_id` picks the level and returns it with the tab that owns
it. The default is **the last frame in the rendered script** -- an ordinary user
wants their own line, not BOSL2's insides, even when a library file happens to
be open. Failing that it takes the innermost frame in any *other* open tab, so a
library a BOSL2 author has opened is still reachable when the chain never
re-enters the script. None when nothing is reachable: top-level geometry in a
`use`d file that is not open has no line to show.

Read through `getattr`, so an older evaluator (no `call_sites`) still refuses
rather than breaking.

**A read-only file selects but does not edit.** `_editable_span_for_id` is
everything selectable minus anything in a read-only tab, and installed libraries
open read-only (`_load_into_tab`). So stepping into BOSL2 shows the line that
built the shape without offering to change it. Unticking **Edit ▸ Read Only** is
the deliberate act that makes it editable -- which is what a BOSL2 author does.

The viewport is told which it has: `set_selection_editable` hides the transform
tools and disarms any running one, and arrow-key nudging declines, so a
read-only selection offers nothing that would fail. The viewport itself knows
nothing about tabs; `_on_selection_changed` tells it.

**The level query is pure.** `_span_at_level(orig_id, level=None)` reads no
shared state and writes none; `_selectable_span_for_id` is the only thing that
touches the stored level, and only when the picked id changes. Every scan over
the id map uses the pure one.

That split exists because the mutating version leaked: `_apply_pending_reselect`
walks every id looking for one span, and left the level set from whichever id it
touched last. Carried onto the next pick it was silently **clamped** into that
object's shorter list -- landing on the whole statement instead of the inner
call. With the statement selected there is no enclosing transform in front of
it, so `find_transform_chain` came back empty and a drag stacked a new wrapper
instead of updating the one that was there.

**Stepping through the chain.** ⌥↑ walks the pick outwards, toward top level;
⌥↓ walks it in, toward the callee -- the same sense as a debugger's stack pane,
and Alt is free because `_key_nudge_magnitude` uses Cmd and Shift. The level
resets whenever a different body is picked, so stepping into BOSL2 never carries
over to the next thing clicked.

`_selection_levels` builds the list: the node's own span when the user wrote it,
then every chain frame naming a file that exists. Consecutive frames naming the
same span collapse -- a `cuboid()` chain is 24 frames and repeats lines (BOSL2's
`translate` wrapper is itself a module), so stepping through duplicates would
just feel broken.

Stepping into a frame whose file is not open **opens it without rendering**
(`open_file_by_path(..., render=False)`). Rendering it would replace the very
geometry the selection belongs to. Installed libraries open read-only, so this
reveals BOSL2's layers without offering to edit them, and the console names the
file, line and level so the walk is legible.

Because the span is the innermost frame *in the script*, it sits inside any
enclosing `translate(...)` -- which is what lets the gizmo's backwards-looking
merge regex find that wrapper and update it rather than adding a second.

The consequence to know: geometry from a module called twice attributes to the
same line in that module's body both times, so a drag on either instance moves
both. Telling instances apart is what stepping *outwards* through the chain is
for (#455).

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

## Nudging

Arrow keys move the selected object without arming any tool, matching the
editable data viewports key for key: 1 unit, 0.1 with Cmd, 10 with Shift
(`_key_nudge_magnitude`), along the two world axes the screen most nearly shows
(`_key_nudge_delta` with `_view_locked_axis`). Those three helpers moved from
`data_viewers.py` to `viewport.py` so both sides share one definition --
`data_viewers` already imports `viewport`, so the dependency only runs one way.

A nudge emits `translate_committed`, the same signal a gizmo drag emits on
mouse-up, so it is the same source rewrite and the same single undo step.

**With the Rotate tool armed the arrows turn instead of moving**, in steps of
90/15/1 degrees (`ROTATION_NUDGE_STEPS`, keyed by what `_key_nudge_magnitude`
returns, exactly as `_HEIGHT_NUDGE_STEPS` does for a heightfield). A 1-unit step
is right for a wall thickness and useless for an angle; the coarse/normal/fine
relationship is kept, so Shift and Cmd still mean one thing.

**Left is counter-clockwise and Right is clockwise, as the viewer sees it.**
That is rotation about the **view** axis -- the one `_view_locked_axis` picks out
as useless for *dragging*, because it is foreshortened to a point, and which is
for that very reason the natural one for keys: "turn this a quarter turn" is a
screen-plane operation, not a turntable one.

`_screen_roll_axis` returns that axis and the sign that makes a positive angle
read as counter-clockwise. The sign matters because a rotation looks CCW only
from the positive end of its axis, so viewing the same model from behind flips
it. Verified by rotating a marker from five viewpoints rather than by reasoning
about the right-hand rule.

Up/Down tip the model away from and toward the viewer, about the screen-right
axis from `_key_nudge_axes` (shared with the translate nudge).

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

Step 1 is `scad_format.find_transform_call`, which scans *backwards* from the
node's span: over whitespace and comments, back through the matching `)` --
counting nesting and stepping over strings, so `translate([f("a)b"), 0, 0])`
closes where it actually closes -- and then checks the identifier in front of
it. `vector_arg` reads the argument, by keyword (`v` for translate/scale, `a`
for rotate) or position, padding a short vector the way OpenSCAD does: z=0 for
translate, z=1 for scale, so `scale([2,2])` does not come back flattened.

This replaced a regex that matched only a literal three-element
`translate([a, b, c])` anchored to the node. A named argument, a two-element
vector, a comment between wrapper and child, or anything nested all missed, and
step 3 then inserted a *second* wrapper instead of updating the first (#452).

The span does not always sit *after* its wrappers. The evaluator attributes
some bodies to the statement rather than to the call inside it, and then the
span **begins with** the very transform a drag should update -- scanning only
backwards finds nothing in front of it, and the drag wrapped the statement in a
second one. `transform_at` is the forward counterpart to `find_transform_call`,
tried when the backwards walk yields no wrapper of the right kind. When the
wrapper is at the span's own start the node does not shift, so `new_node_start`
is `call.start` rather than the shifted offset.

A vector that is not plain numbers -- `translate([x, 0, 0])`, `[1+1, 0, 0]` --
is found but deliberately **not** rewritten: the drag wraps instead, because
replacing an expression with a number would throw the user's own work away.

`translate/rotate/scale` share one implementation (`_commit_transform`); they
differed only in the op name and how an existing vector combines with the drag.
A merged rewrite replaces exactly the wrapper's own span, so a comment between
it and the node stays where the user put it. A named argument is normalised to
positional on rewrite.

**Re-selecting after a commit.** A drag or a nudge asks for its node back
through `_restore_selection_after_gizmo`, which only *records* the offset:
`_render()` starts a QThread and returns, so at that moment `id_to_node` is
still the map from before the edit, with spans at the old offsets. Searching it
there matched nothing and cleared the selection -- one nudge deselected the
object, and a second was impossible.

The request records **where the edit began**, not where the node is predicted
to land. Predicting an exact offset does not survive a body's span changing
level between renders: one attributed to its statement before the edit can be
attributed to the call inside it afterwards, a wrapper's width later, and the
prediction then missed by about 24 characters and the selection was cleared.
Bodies are ordered by position, so the first one whose span starts at or after
the edit is the statement that was edited, whatever level it ended up at.

The request also carries **which render must satisfy it**: `(render_id, offset)`,
the id of the render the edit itself started. A render already in flight when
the edit lands carries the id map from *before* it, and if that one finished
first it consumed the offset, matched nothing and cleared the selection. There
is usually only a stale render in flight for the *first* nudge after a click,
which is why it looked intermittent. An older render now leaves the request
pending; a later one still honours it, since it is rendering the same source.

`_on_render_done` consumes it (`_apply_pending_reselect`), past the `render_id`
guard so a superseded render never re-selects. It also refreshes
`set_selection_editable`, which is otherwise left at whatever the last *click*
set. The match is on the **editable**
span, since for library-built geometry the producing node's span is in another
file entirely.

**Two ordering constraints, not one.** It must run after `self.id_to_node =
id_to_node`, or it searches the pre-edit map; and after
`Viewport.load_geometry`, whose `SceneRenderer` call ends with
`self.selected_id = None`. Re-selecting before the upload drew the gizmo for an
instant and then had it wiped -- the arrows flickered and the object deselected
anyway. Both are asserted in `tests/test_selection_origin_guard.py`, along with
the premise that `load_geometry` really does clear the selection.

## Value Overlay

During translate/rotate/scale, a text readout of the current value is shown in the viewport (`Viewport._delta_label`, bottom-centre).

**[NOT IMPLEMENTED]** — the rest of this subsection. `_delta_label` is a read-only `QLabel`: it displays, it cannot be typed into or focused, and there is no commit/cancel path through it. (The same label is reused by data-viewer vertex drags to name the constrained plane.)

> The user can type an exact value instead of dragging; committing applies the same source rewrite rules as a drag commit.
>
> Enter commits; Escape cancels and reverts to the pre-interaction state. The ghost mesh updates on commit (Enter), not while typing. The text field only gets focus on click — no auto-focus on drag-start.
>
> Displayed value follows the source rewrite classification: absolute value for a literal number or a bare variable set to a number; delta for an expression.

## Transform Edit Rules

- **Nested transforms of the same type**: modify the innermost matching wrapper. The backwards scan starts at the node, so the innermost is the only one it reaches.
- **Transform composition order**: a new wrapper goes **outside** any the node is already wrapped in, and a merge updates the **outermost** wrapper of that type. This is what makes a world-aligned handle tell the truth — inserted inside, a drag on the world-x handle of `rotate([0,0,45]) cube(10)` moved the object along the *rotated* x (#453).

  `find_transform_chain` walks out while each enclosing call is one of `TRANSFORM_WRAPPERS` (translate, rotate, scale, mirror, resize, multmatrix, color) and **stops at anything else**. A `for` is a wrapper too, but hoisting a drag outside the loop would move every iteration rather than the one the user grabbed; the same goes for `if`, a `difference()` operand and a user module's call.

  An *inner* wrapper of the same type is deliberately left alone: merging into the `translate` in `rotate([0,0,45]) translate([1,0,0]) cube(10)` would move along the rotated axis, so a new outer one is added instead.
- **Live drag preview** — **[NOT IMPLEMENTED]**. There is no ghost mesh; only the delta readout updates during a drag. The AST edit and re-render still happen on mouse-up, as one undo step.
  > Original intent: wireframe ghost copy of the mesh during drag.
- **Gizmo orientation**: handles are world-axis aligned (`Viewport._AXIS_DIRS` is the identity basis), at the selection's bounding-box centre, and since #453 the *edit* is world-aligned to match — a drag on the world-x handle moves the object along world x whatever it is wrapped in.

  Local-frame handles were the original intent and are not what shipped. They would need the object's accumulated transform, and nothing keeps it: bodies reach the renderer already baked into world space and `ColoredBody` carries no matrix, so it would have to be reconstructed from source or added to the evaluator. Aligning the edit to the handles instead needed neither, and leaves the two agreeing — which was the actual complaint.

## Source Rewrite Rules (Intent Preservation)

A drag commit keeps each component's **own text** and adjusts it:

| Component | Rewrite |
|---|---|
| A number (`10`, `1.5`) | Recomputed: `translate([10,0,0])` dragged +1 becomes `[11, 0, 0]` |
| An expression (`wall/2`) | The adjustment is appended: `wall/2 + 1`. The relationship survives |
| A delta this already added (`wall/2 + 1`) | Folded, not chained: another +1 gives `wall/2 + 2`, and -1 gives back `wall/2` |
| Untouched by this drag | **Left exactly as written.** `1e3` stays `1e3`, `1.500` stays `1.500` |

Scale multiplies instead of adding, and parenthesises: `scale([w+1,1,1])` doubled is `(w+1) * 2`, never `w+1 * 2`.

That last row matters as much as the others: the old rewrite pushed all three components through `f"{v:.4g}"` whatever the drag touched, so a drag along x reformatted y and z as a side effect.

**What is still not implemented** is the variable *declaration* case: nothing reads or writes a variable's declaration site, so `x = base/2` used in `translate([x,0,0])` has the delta appended inline rather than at the declaration.

> Original intent for that case:
>
> | Argument form | Rewrite strategy |
> |---|---|
> | Variable set to a literal (`x = 10`) | Update the literal at the variable's declaration site |
> | Variable set to an expression (`x = base/2`) | Append a delta at the declaration site: `x = base/2 + 5` |
>
> Editing a variable declaration affects all sites referencing it — intentional, preserving the user's parametric relationships.
