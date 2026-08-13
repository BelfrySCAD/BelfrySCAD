# Threaded Rendering

Parse + evaluate runs in a background `QThread`. Two helper classes in `main_window.py`:

- **`_RenderWorker(QObject)`** — moved to the worker thread via `moveToThread`; does the parse/evaluate work; emits `logged`, `parse_errored`, `finished`, `done`
- **`_RenderCallback(QObject)`** — stays in the main thread; `@Slot` methods receive worker signals; Qt auto-detects the cross-thread boundary (`QueuedConnection`), so callbacks run on the main thread

**Do not connect worker signals to Python lambdas** — lambdas have no thread affinity, so Qt can't determine which event loop to post to. Always route through a `QObject` slot with known thread affinity.

**Source input**: `_render()` reads the editor's current text (`toPlainText()`), not the saved file. The worker writes this to a temp file in the same directory as the original (so relative `include`/`use` paths resolve) and passes that to the parser.

Every temp `.scad` in the app goes through `belfryscad/scad_temp.py` — the render worker, the debug session, the AI expression probe, and the CLI's `-D` prelude. Because they land in the *user's* project directory rather than `/tmp`, a leaked one is visible clutter in their work, so that module does two things a bare `tempfile` call does not:

- Names them `belfryscad-*.scad`. A default `tmp*.scad` is indistinguishable from a file the user wrote, which makes it unsafe to ever clean up automatically.
- Sweeps its own stale leftovers from that directory on each new write. `try/finally` covers every failure the process survives; it cannot cover SIGKILL or a segfault in the C++ evaluator, and those are what strand a temp. The sweep only touches the app's own prefix, only in the one directory it was already writing to, and only files older than an hour — so a second BelfrySCAD window rendering out of the same folder never loses its in-flight temp.

`mkstemp` is used rather than `NamedTemporaryFile(delete=False)` because it returns the path at the moment the file starts existing, leaving no window where the file is on disk but the caller has nothing to clean up yet.

**Cancellation**: `_render()` passes a `threading.Event` to the worker, which checks `cancel.is_set()` between major steps. A `render_id` counter increments per render; the callback discards results whose `render_id` no longer matches.

**Progress indicator**: a QLabel overlay centered in the viewport shows elapsed seconds and cycling dots (`.` → `..` → `...` → blank) during rendering, updated every 100ms via QTimer. A `WaitCursor` override is set/restored at the same time. The viewport geometry is cleared at render start so only the overlay is visible.

**Cancellation by user**: pressing Escape while a render is in progress sets the cancel event, hides the render overlay, and logs "Render cancelled." to the console.

**Elapsed time**: every render outcome logs its elapsed wall-clock time to the console via `_fmt_elapsed()` — formatted as `(Nms)` under 1000ms or `(N.NNNs)` at 1000ms and above. This applies to successful renders (alongside the bounding-box summary), no-geometry renders, eval errors, recursion-limit errors, and uncaught runtime errors.

**Job lifetime**: each `_render()` call appends `(worker, callback, thread)` to `self._render_jobs`, kept alive until `thread.finished` fires `_cleanup_job` (which removes the entry). Without this, Python could GC the worker/callback before `thread.started` fires, raising `AttributeError: Slot '_RenderWorker::run()' not found.`

**`_RenderCallback`**: constructor takes `(main_window, file_tab, render_id)`. `on_logged` routes to `main_window._console.append_output()`; `on_ast_ready` sets `file_tab.root_scope` and calls `file_tab.editor.update_user_names()`; `on_finished` calls `main_window._on_render_done(file_tab, ...)` which stores results in window-level `self._rendered_tab`, `self.id_to_node`, `self._bodies` and loads geometry into `self._viewport`. The `tab` arg in `_on_render_done` et al. identifies which `FileTab` produced the render, used for source write-back by gizmos.

**Profiling**: `_RenderWorker.__init__` takes an opt-in `profile: bool = False` param (default `False` — normal F6 renders are unaffected), passed straight through to `Evaluator(..., profile=self._profile)`. `finished`'s payload is `Signal(object, object, float, object, object, object)` — `(bodies, id_to_node, elapsed_ms, final_vp, csg_tree, profile_result)`, the last element being the evaluator's `ProfileResult` (`None` unless `profile=True`; see openscad_cpp_evaluator's `CLAUDE.md` on profiling). `_on_render_done` stashes it as `self._last_profile_result` and, if it's not `None`, immediately opens the report (`_show_profile_report()`) rather than waiting for a separate Design → "Show Profile Report…" click — that action still works afterward to reopen the same (`ProfileViewer`) result on demand. `MainWindow._render(self, tab=None, profile: bool = False)` threads the flag through from Design → "Render with Profiling" (no keyboard shortcut — F6/Shift+F6 are already Render/Debug) — a separate, explicitly opt-in menu action, not part of the automatic render-trigger set (gizmo drag, save, open, animation frame advance all call `_render()` with `profile` left at its default `False`).

**Shutdown and interpreter exit**: `MainWindow.closeEvent()` pauses the window-level `AnimatePane` (no new renders get queued), sets the cancel event, and waits (with a 5s deadline, pumping `QApplication.processEvents()`) for any `_render_jobs` threads to finish — Qt aborts if a `QThread` is destroyed while still running. It then saves settings (with an explicit `QSettings.sync()`) and clears `self._bodies` / `self._viewport.load_geometry([])` to drop references to Manifold geometry via normal refcounting.

## Stereo (Cross-eye) mode

**View menu → "Stereo (Cross-eye)"** renders two side-by-side perspective views in a single `QOpenGLWidget`. When enabled, `Camera.stereo = True` and `SceneRenderer.paint()` renders two passes:

1. Calls `Camera.stereo_view_matrices(half_vp_w, vp_h)` (device pixels), which shifts each camera ±(`distance × stereo_fraction / 2`) along the camera's right vector (row 0 of the view matrix), pointing both eye cameras at the same target (toe-in). Cross-eye arrangement: left panel = right eye, right panel = left eye.

   `stereo_fraction` is computed from physical viewer measurements stored in `QSettings` and applied to `Camera` fields at preference-apply time:

   ```
   rendered_half_fov_h = atan( tan(fov/2) × half_vp_w / vp_h )
   physical_half_fov_h = atan( (half_vp_w × 25.4 / screen_dpi) / (2 × viewer_screen_dist) )

   stereo_fraction = (viewer_ipd / viewer_screen_dist)
                   × (physical_half_fov_h / rendered_half_fov_h)
                   × stereo_depth_scale
   ```

   The first factor (`IPD / screen_dist`) matches the angular separation of the cameras to the angular separation of the viewer's eyes. The second factor corrects for the viewer not sitting at the "natural" viewing distance for the rendered FOV (i.e., the physical visual angle of the viewport differs from the rendered FOV). `stereo_depth_scale` (default 0.75) is a comfort trim because the geometrically exact value can exceed comfortable disparity limits for objects near the camera.

   `screen_dpi` is read from `QScreen.physicalDotsPerInch()` when preferences are applied. For a 100 DPI monitor, 90 mm IPD, 770 mm screen distance, and a ~900 px tall window, this yields roughly 3–4 % of camera distance.
2. Each pass sets `ctx.viewport` to its half of the framebuffer, temporarily overrides `self._viewport` to `(half_w, h)` so axes, labels, and other screen-size-dependent calculations use the half-width, and calls `_paint_scene(view, proj, L_world)` where `proj` uses the half-width aspect ratio.
3. `_paint_scene()` computes eye position from the view matrix (`eye = -R^T · t`) for correct per-eye specular highlights. Axes, labels, and gizmo all render in both eyes.

Stereo and Perspective are independently togglable. Both states are saved to `QSettings` and restored on launch. Keyboard shortcut: **Ctrl+Cmd+3**.

In orthographic mode, stereo still works: the same toe-in view matrices are used (both cameras shifted laterally and pointed at the target), but `projection_matrix()` produces a parallel projection for each eye instead of a frustum. The eye separation formula is unchanged. Toe-in with orthographic projection introduces a small amount of keystone distortion, but at the typical 3–5 % separation values it is negligible.

## Spin

**View menu → "Spin"** (Ctrl+Cmd+1) continuously rotates the camera azimuth at 6 RPM (1.2°/tick at 30 FPS, driven by a `QTimer` with a 33 ms interval). Spin state is **not** saved between sessions — the app always starts with Spin off.

## Modifier characters

| | model | viewport | export |
|---|---|---|---|
| `#` highlight | unchanged | drawn see-through in red, never solid | included — `#` does not change the model |
| `%` background | excluded | drawn as a translucent grey ghost | **excluded** |
| `!` show only | becomes the whole model | its subtree only | its subtree only |
| `*` disable | removed | nothing | nothing |

`%` is scenery: something to line other work up against. It is kept out of
the booleans upstream, and `exporters.exportable()` keeps it out of files
too — `%cube(10);` on its own exports nothing at all, matching the
reference.

Its ghost is a fixed neutral grey (`_BACKGROUND_COLOR`), not the body's own
colour and not an explicit `color()`: tinting scenery with the live theme
made it read as part of the model. The alpha is measured against the empty
viewport — at 0.2 the ghost sat 23 levels off the background and barely
registered; at 0.35 it sits 35 off and still shows what is behind it.

`#` bodies are drawn only in the see-through pass (`_HIGHLIGHT_COLOR`),
never opaquely: `#` marks something to look at, not something to look at
the outside of, and a solid draw underneath leaves nothing to see through
to. The alpha is measured — below about 0.4 the wash lands within a shade
of the untouched surface colour and reads as no highlight at all.

### Known divergence

A `#` on an operand of a boolean is exported twice over. The evaluator
returns the boolean's result *and* a copy of the highlighted operand for
the viewport to ghost, and nothing in a `ColoredBody` distinguishes that
display copy from a highlighted object at top level, which is real
geometry and must be exported. Measured against the reference:
`difference() { cylinder(h=10,d=10); #cube(10); }` writes 64 triangles
where it should write 52. Fixing it means marking the copy in the
evaluator, not guessing in the exporter.

## Export

**3MF is the default.** It is the only format here that carries colour,
separate objects and per-triangle colour at once — i.e. everything
`split_bodies_for_export` produces — where STL keeps none of it. It leads
the filter list, so it is what the Export dialog opens on.

The dialog returns both a path and the selected filter, and they can
disagree. `_resolve_export_format()` settles it: **a suffix the user typed
wins over the dropdown** (typing `part.ply` with 3MF selected means PLY),
and otherwise the dropdown decides and its extension is appended. Before
this the selected filter was discarded outright, so choosing PLY and typing
a bare name silently wrote an STL. An unrecognised suffix is appended to
rather than replaced (`part.v2` → `part.v2.3mf`), since that text is the
user's and may not be a suffix at all; matching is case-insensitive while
the path keeps its own case, so `PART.STL` is STL and stays `PART.STL`
(appending to it, which the old code did, gave `PART.STL.stl`).

Formats split into two groups by whether the container can hold more than
one object.

| format | objects | colour | per-triangle colour |
|---|---|---|---|
| STL (binary/ASCII) | one merged mesh | none | — |
| OBJ | one `o` group each | `usemtl` + companion `.mtl` | `usemtl` runs |
| 3MF | one `<object>` each | `m:colorgroup` per object | `pid`/`p1` per `<triangle>` |
| PLY | one flat mesh | per-vertex RGB | unwelded, 3 verts/triangle |
| VRML (`.wrl`) | one `Shape` each | `Material diffuseColor` | `Color` + `colorIndex` |
| X3D (`.x3d`) | one `<Shape>` each | `<Material diffuseColor>` | `<Color>` + `colorIndex` |

STL goes through `merge_bodies_to_mesh`; the other three share
`split_bodies_for_export`.

### The object split

`exporters.split_bodies_for_export()` is what the multi-object formats
share. The evaluator hands back one `ColoredBody` per top-level coloured
region, un-unioned; writing those straight out was wrong, because **top
level in OpenSCAD is an implicit union**. `cube(100); cube(100,
center=true);` used to write two overlapping objects with their interior
faces intact — 24 triangles. Real OpenSCAD 2022.08.22 writes one object of
20 vertices and 36 triangles for that script (checked directly), which is
what this now produces.

Three rules, applied in order:

1. **Union, never concatenate.** Same reasoning `merge_bodies_to_mesh` has
   always used for STL: concatenating is only right while the bodies are
   disjoint, and where two touch each writes its own copy of the shared
   face.
2. **One object per colour, and no two objects share volume.** Where
   differently-coloured solids overlap, the **later** one owns the shared
   volume and the earlier is notched around it — painter's order, so
   `color("red") body(); color("blue") detail();` leaves the detail whole.
   Implemented by walking the bodies in reverse and subtracting everything
   already claimed. Only the invisible interior is affected: the visible
   surface is identical either way, since whichever solid is outermost at
   a given face is what you see. Same-coloured pieces then merge into one
   object.
3. **One object per connected component.** Whatever rules 1 and 2 leave is
   run through Manifold's `decompose()`, so a model that falls into
   unconnected pieces writes one object per piece.

The guarantee the tests assert is that the parts *tile* the union: summed
part volumes equal the union volume, and every pairwise intersection is
empty (`test_export_object_split.py`).

The all-one-colour case — by far the most common — skips the per-body
subtraction entirely and goes straight to a `batch_boolean` Add, since one
colour cannot overlap itself into a different answer.

### Per-triangle colour

An explicit CSG op over differently-coloured children — `union() {
color("red") a(); color("blue") b(); }` — produces **one** solid whose
*surface* carries two colours, which the evaluator hands over as
`ColoredBody.tri_colors`, an `(nTri, 4)` array. There is no volume split to
make: the volumes really did merge, and nothing in the surface says where
one colour's material would end. Such a body used to export entirely in its
base colour, which for a merge is just whichever child came first.

3MF's own model is the answer, and the spec is explicit about it —
"physically based materials specify only the appearance of material at the
surface of the object. They do not describe the distribution of the
material through the volume." So colour is written *per triangle*: one
`<colorgroup>` holding the distinct colours, and each `<triangle>` carrying
`pid` + `p1` into it. (`p1` alone applies to the whole triangle; the spec
requires `p2`/`p3` to be absent or equal to it, and absent is the right
form for a flat-shaded face.) This is what the surface-colour printers
consume — PolyJet, Mimaki, HP Jet Fusion 580, binder jetting.

The other two formats carry it as far as they can:

- **OBJ** has no per-face colour except through the material in effect, so
  a multi-coloured surface becomes runs of faces with a `usemtl` between
  them, emitted in triangle order.
- **PLY** puts colour on vertices, and a vertex shared by two
  differently-coloured triangles has no single answer — so those objects
  are **unwelded**, three vertices per triangle. Only the objects that need
  it pay for it; a single-coloured cube still writes 8 vertices.

**When it is dropped.** A per-triangle array indexes the triangle list it
was built against, and every boolean rewrites that list. `_carry_tri_colors`
therefore checks rather than assumes: `batch_boolean` over a single operand
and `decompose()` of a single component both return the triangles unchanged
(measured), so an object that was not actually cut keeps its colours, and
one that was falls back to its base colour. Mis-colouring a recut surface
would be worse than losing the detail.

### VRML and X3D

The two surface-colour interchange formats, and the ones the full-colour
printers actually read — VRML is what GrabCAD Print (PolyJet), Mimaki 3D
Link and HP's Jet Fusion 580 all list, and X3D is its XML successor. They
are the same scene graph in different syntax, so `write_vrml` and
`write_x3d` share `_shape_parts()`.

VRML is written as **VRML97** (`#VRML V2.0 utf8`), which is the version
those front ends name. X3D declares **version 3.3, Interchange profile** —
the accurate claim rather than the safe-looking Immersive: per Annex B,
Interchange is Geometry3D level 2 (`IndexedFaceSet`), Rendering level 3
(`Coordinate`, `Color`) and Shape level 1 (`Appearance`, `Material`),
which is exactly the node set used and nothing more.

Per-face colour is native to both: an `IndexedFaceSet` with
`colorPerVertex FALSE` takes one `colorIndex` entry per face. Note that
`colorIndex`, unlike `coordIndex`, must contain **no negative entries** —
`-1` terminates a face in `coordIndex` and means nothing here.

One lossy corner: neither format's `Color` node carries alpha, so a
per-triangle *alpha* has nowhere to go and the object's base alpha applies
to the whole shape via `Material.transparency` (which is `1 - alpha`, not
alpha).

### What the split does not cover

- **Open shells.** Manifold discards anything that is not a closed solid,
  so an open surface cannot take part in any boolean. Its triangles are
  written as their own object as-is and its 1-based index is reported
  through `open_parts` for the caller to warn about — the same contract
  `merge_bodies_to_mesh` has.

### OBJ writes two files

OBJ carries no colour of its own, so `write_obj` emits a `.mtl` next to
the `.obj` and references it with `mtllib`. Exporting `m.obj` therefore
also writes `m.mtl`. One material per distinct RGBA however many objects
share it; alpha below 1 becomes `d` (opacity, not transparency). A reader
that ignores the `mtllib` still gets correct geometry.

### PLY has no objects

Standard PLY has no multi-object concept, so the objects are concatenated
into one vertex/face list and the colour rides on the vertices instead.
That is lossless only because the split guarantees the objects are
disjoint and never share a vertex.

## Viewport visuals

**Clip planes**: `Camera.clip_planes()` returns `(near, far)`, scaled to `camera.distance` rather than fixed constants (floors at the original `0.1`/`10000.0`, so typical/small scenes are unaffected). `frame_bounds()` sets `distance` proportional to the framed object's radius (same distance-scaling heuristic `_render_axes`/`_axis_extent` use for tick/axis extent) — a fixed `far=10000` clipped large or elongated models once that pushed the camera far enough away to fit them at the default FOV: `cylinder(h=3500, d=1000)` needs `distance≈10438` to fit at `fov=22.5`, already past a fixed `far=10000`, silently clipping whichever end of the cylinder was farther from the eye (and varying with zoom, since that changes `distance`). `far = max(10000.0, distance * 3.0)`; `near = max(0.1, far / 100000.0)` — grown proportionally rather than held fixed, preserving the original `far/near` ratio (holding `near` fixed while `far` grows would only worsen depth-buffer precision for large scenes on top of the clipping bug). `projection_matrix()` uses these for both perspective (`_perspective`'s own near/far) and orthographic (`_ortho`'s symmetric `±far` depth range) modes. Pure math, unit-tested in `test_renderer.py::TestCameraClipPlanes` — no GL/Qt dependency.

**Object colors**: default geometry is yellow `(0.9, 0.85, 0.1)`. Selection applies `_highlight_color`, which tints toward green `(r*0.35, g*0.35+0.65, b*0.35)`.

**Multi-color CSG merges**: a body produced by a real boolean merge (`union()`/`difference()`/`intersection()` — see openscad_cpp_evaluator's `CLAUDE.md` on multi-color merges and `triColors`) can carry `ColoredBody.tri_colors`, a per-triangle RGBA override recovering each part's own original color/alpha, which the single-color `.color` field can no longer represent once the parts are merged into one `Manifold`. `_upload_body` uploads this as a per-vertex `in_vcolor` attribute (white `(1,1,1,1)`, a shader no-op, for the overwhelming majority of ordinary single-color bodies) multiplied against the `object_color` uniform in the fragment shader — `_highlight_color`'s selection tint and per-frame color-theme resolution keep working unchanged on top, since they operate on the uniform, not `in_vcolor`. If a merged body's triangles span both an opaque and a translucent alpha, `_upload_body` splits it into two `MeshBuffer`s (one per alpha bucket) so each still routes into the correct opaque/translucent pass below; both buffers share the *same* (full, unsplit) `original_ids` set, so selecting/dragging/rotating either half moves the whole original body together rather than leaving one half behind (`_selected_buffer_bbox` also aggregates across every buffer matching the selected ID, not just the first found). A split buffer's dummy `MeshBuffer.color` alpha (`1.0` or `<1.0`) exists only to route it into the right pass — `uses_vertex_color=True` tells `_paint_scene` to force the *uniform's* alpha to `1.0` before drawing, so it doesn't also multiply into the real per-triangle alpha already carried by `in_vcolor.a`.

**Modifier render passes** — `_paint_scene()` runs the following sequence per eye:
1. **Opaque pass**: bodies with `role` not in `{"background", "highlight_ghost"}` whose resolved color has alpha `>= 1.0` (the common case), rendered normally with full depth test and depth write.
2. **Translucent pass** (`color()`'s alpha `< 1.0`, any of `normal`/`show_only`/`highlight` role — e.g. `color([1,0,0,0.5])`): deferred out of the opaque pass (drawing it there would depth-write it fully opaque, discarding the alpha the fragment shader computed) and drawn afterward with `SRC_ALPHA/ONE_MINUS_SRC_ALPHA` blending, depth write disabled, and back-face culling on. Sorted back-to-front by each buffer's object-space centroid (`cpu_v0.mean(axis=0)`) transformed to world space and measured against the eye position — an approximation (not true per-triangle sorting), sufficient to avoid the worst near/far swaps between multiple overlapping translucent bodies. Culling matters even for a single body: with depth write off, a thin/coplanar body's own hidden far face (e.g. the underside of a top-level 2D shape's near-zero-height extrusion) is drawn in raw mesh-triangle order rather than depth-sorted, and can land after the visible face and wrongly overwrite it — culling removes the hidden face outright.

   **`depth_mask` lives on `Framebuffer`, not `Context`** — `moderngl.Context` has no such attribute, so `self._ctx.depth_mask = False/True` silently no-ops (sets a stray, unused Python attribute) rather than raising or disabling GL depth writes. This affected every blended pass below too (ghost, highlight), not just this one — depth writes were never actually being disabled during blending, so overlapping translucent geometry got real (order-dependent-looking but actually just wrong) opaque z-buffer occlusion instead of blending. Fixed by resolving the real framebuffer once in `paint()` (`self._active_fbo = fbo`, the same object returned by `self._ctx.detect_framebuffer(qt_fbo_id)`) and toggling `self._active_fbo.depth_mask` in `_paint_scene()` instead of `self._ctx.depth_mask`.
3. **Axes and labels**: rendered after the opaque+translucent passes with full depth write still active, so they are composited correctly under the subsequent transparent ghost pass — ghost geometry (at 0.2 alpha) composites over the axes, making the axes visible through ghost objects.
4. **Ghost pass** (`role="background"`, OpenSCAD `%`): `SRC_ALPHA/ONE_MINUS_SRC_ALPHA` blending, depth test LESS, depth write disabled, back-face culling on — the ghost appears only where no opaque geometry or axes occlude it. Color uses the body's own color at a fixed 0.2 alpha (its own `color()` alpha, if any, is not honored here). Background bodies are skipped by ray-cast picking.
5. **Highlight overlay pass** (OpenSCAD `#`): covers two sub-cases:
   - `role="highlight"` (top-level `#`): body is real geometry already rendered in the opaque pass. Re-rendered with polygon offset `(-1.0, -1.0)` shifting toward the camera so the overlay passes the LESS depth test, pink `(1.0, 0.08, 0.45, 0.35)`, blending on, depth write off.
   - `role="highlight_ghost"` (`#` used inside a CSG op like `difference()`): the body was consumed by the CSG kernel and is NOT in the opaque pass. Rendered as a pink ghost with back-face culling on, using the depth buffer written by the opaque pass, so it is occluded by surrounding solid geometry. No polygon offset needed.

`MeshBuffer.role` stores the role string so the renderer can classify each buffer. Background buffers are excluded from ray-cast picking in `ray_cast()`.

**Lighting**: Blinn-Phong shading with a key light, fill light, ambient term, and specular highlights (exponent 64, intensity 0.5). The key light direction is defined in view space as `[0.6, 0.8, 1.0]` and transformed to world space, so it follows the camera by default. Option+left-drag adjusts the light direction via azimuth (around viewport vertical Y axis) and elevation (around viewport horizontal X axis) offsets applied in view space before the world transform — the adjusted light stays fixed relative to the user's POV when orbiting.

**Axis ticks and labels**: each axis has perpendicular tick marks (X/Z ticks extend along Y; Y ticks extend along X). Ticks are one-sided, extending only in the positive perpendicular direction; minor ticks are ~24 px, major ticks ~48 px. The world-space half-length `L` driving both the drawn axis-line extent and `_nice_spacings(L)`'s tick/label spacing target (`raw = L / 28`) comes from `_axis_extent(camera)` (free function, `renderer.py`, pure math, unit-tested in `test_renderer.py::TestAxisExtent`): `distance * tan(fov/2) / tan(DEFAULT_FOV/2) * 2.5` — scaled by `fov` as well as `distance` so that changing `fov` alone (Shift+wheel, no change in `distance`) keeps tick density visually consistent on screen; previously this was a flat `distance * 2.5`, so narrowing `fov` (optical zoom-in) left tick spacing unchanged in world-space while far fewer ticks fit on screen, and widening `fov` left the axes looking sparse. Dividing by `tan(DEFAULT_FOV/2)` makes this identical to the old flat formula at the default fov, so views that never touch `$vpf` are unaffected. When only one minor tick would fall between majors (`major_steps <= 2`), spacing is promoted so the minor interval becomes the new major and the old major becomes the label interval, keeping the axis visually dense. Tick labels are depth-aware, camera-facing textured billboards rendered in the 3D scene (not a QPainter screen-space overlay) — each `QImage` is rasterized once into a ModernGL texture and drawn as a unit quad oriented via `right`/`up` vectors from the view matrix. Labels are positioned on the negative perpendicular side (opposite the ticks). Labels and minor ticks on axes within 5° of end-on to the camera are suppressed (major ticks still draw). Before that hard cutoff, `_axis_density(camera)` (free function, `renderer.py`, pure math — no GL/Camera-instance coupling beyond taking a `Camera` argument, unit-tested in `test_renderer.py::TestAxisDensity`) thins labels and minor ticks per axis to avoid the alternative: evenly-world-spaced labels crowding into an ever-smaller on-screen span as the axis foreshortens, overlapping illegibly well before 5°. For each axis, `view_dir[ai]` (cosine of the angle between the camera's view direction and that world axis) gives foreshortening `sqrt(1 - view_dir[ai]**2)` — 1 when the axis is broadside (full density), shrinking toward 0 as it swings end-on. Half the reciprocal (doubling density vs. a plain 1/foreshorten stride), snapped to the nearest power of two, is the "show every Nth tick" stride — powers of two keep the visible subset nested as the camera rotates (every 4th-tick set ⊂ every-2nd ⊂ every tick) rather than jumping to an unrelated set each frame. `_render_axes` applies this stride via `_tick_is_drawn(k, major_steps, end_on, stride)` (pure, unit-tested in `test_renderer.py::TestTickIsDrawn`): at the hard end-on cutoff, only major ticks draw (unchanged, long-standing behavior — majors always show even dead-on end-on). Below that cutoff, stride applies uniformly to major *and* minor ticks alike, rather than exempting majors the way the hard cutoff does — `major_steps` (how often a major tick falls) and `stride` aren't related, so exempting majors there let an every-major-step tick land arbitrarily close to a kept minor tick while a stride-sized gap opened up elsewhere, a visibly inconsistent spacing bug. Requiring just `k % stride == 0` (majors included) keeps the drawn set a strict, evenly-spaced arithmetic subsequence regardless of where `major_steps` falls. `_axis_tick_world_points` applies the same per-axis stride to labels independently, since labels are keyed to the label-spacing tier (which may differ from the tick marks' major/minor tiers, not to the tick-mark loop's own index) — that loop only ever had the one tier, so it was never susceptible to this bug. Positive axis lines are colored (red/green/blue); negative axes are gray.

**Mouse-wheel zoom**: fixed ±1% step (`factor = 1.01` or `0.99`) with a 5-unit deadspot on `angleDelta` to avoid jitter on near-zero deltas.

`main.py` then exits via `os._exit(code)` rather than `sys.exit(code)`/falling off the end of `main()`. This **skips Python's normal interpreter finalization**, which performs a final `gc.collect()` pass — and collecting nanobind-wrapped `m3d.Manifold`/`m3d.CrossSection` objects shortly after a background render `QThread` has been active can SIGSEGV (nanobind's object collection isn't thread-safe across a recently-active worker thread). `gc.disable()` does not prevent this, since CPython's finalizer forces a collection regardless. Because `os._exit()` skips `atexit`/destructors entirely, `closeEvent`'s explicit `QSettings.sync()` is required so window geometry/state are flushed to disk before exit.
