# Threaded Rendering

Parse + evaluate runs in a background `QThread`. Two helper classes in `main_window.py`:

- **`_RenderWorker(QObject)`** — moved to the worker thread via `moveToThread`; does the parse/evaluate work; emits `logged`, `parse_errored`, `finished`, `done`
- **`_RenderCallback(QObject)`** — stays in the main thread; `@Slot` methods receive worker signals; Qt auto-detects the cross-thread boundary (`QueuedConnection`), so callbacks run on the main thread

**Do not connect worker signals to Python lambdas** — lambdas have no thread affinity, so Qt can't determine which event loop to post to. Always route through a `QObject` slot with known thread affinity.

**Source input**: `_render()` reads the editor's current text (`toPlainText()`), not the saved file. The worker writes this to a temp file in the same directory as the original (so relative `include`/`use` paths resolve) and passes that to the parser.

**Cancellation**: `_render()` passes a `threading.Event` to the worker, which checks `cancel.is_set()` between major steps. A `render_id` counter increments per render; the callback discards results whose `render_id` no longer matches.

**Progress indicator**: a QLabel overlay centered in the viewport shows elapsed seconds and cycling dots (`.` → `..` → `...` → blank) during rendering, updated every 100ms via QTimer. A `WaitCursor` override is set/restored at the same time. The viewport geometry is cleared at render start so only the overlay is visible.

**Cancellation by user**: pressing Escape while a render is in progress sets the cancel event, hides the render overlay, and logs "Render cancelled." to the console.

**Elapsed time**: every render outcome logs its elapsed wall-clock time to the console via `_fmt_elapsed()` — formatted as `(Nms)` under 1000ms or `(N.NNNs)` at 1000ms and above. This applies to successful renders (alongside the bounding-box summary), no-geometry renders, eval errors, recursion-limit errors, and uncaught runtime errors.

**Job lifetime**: each `_render()` call appends `(worker, callback, thread)` to `self._render_jobs`, kept alive until `thread.finished` fires `_cleanup_job` (which removes the entry). Without this, Python could GC the worker/callback before `thread.started` fires, raising `AttributeError: Slot '_RenderWorker::run()' not found.`

**`_RenderCallback`**: constructor takes `(main_window, file_tab, render_id)`. `on_logged` routes to `main_window._console.append_output()`; `on_ast_ready` sets `file_tab.root_scope` and calls `file_tab.editor.update_user_names()`; `on_finished` calls `main_window._on_render_done(file_tab, ...)` which stores results in window-level `self._rendered_tab`, `self.id_to_node`, `self._bodies` and loads geometry into `self._viewport`. The `tab` arg in `_on_render_done` et al. identifies which `FileTab` produced the render, used for source write-back by gizmos.

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

## Viewport visuals

**Clip planes**: `Camera.clip_planes()` returns `(near, far)`, scaled to `camera.distance` rather than fixed constants (floors at the original `0.1`/`10000.0`, so typical/small scenes are unaffected). `frame_bounds()` sets `distance` proportional to the framed object's radius (same heuristic `_render_axes` uses for tick/axis extent via `distance * 2.5`) — a fixed `far=10000` clipped large or elongated models once that pushed the camera far enough away to fit them at the default FOV: `cylinder(h=3500, d=1000)` needs `distance≈10438` to fit at `fov=22.5`, already past a fixed `far=10000`, silently clipping whichever end of the cylinder was farther from the eye (and varying with zoom, since that changes `distance`). `far = max(10000.0, distance * 3.0)`; `near = max(0.1, far / 100000.0)` — grown proportionally rather than held fixed, preserving the original `far/near` ratio (holding `near` fixed while `far` grows would only worsen depth-buffer precision for large scenes on top of the clipping bug). `projection_matrix()` uses these for both perspective (`_perspective`'s own near/far) and orthographic (`_ortho`'s symmetric `±far` depth range) modes. Pure math, unit-tested in `test_renderer.py::TestCameraClipPlanes` — no GL/Qt dependency.

**Object colors**: default geometry is yellow `(0.9, 0.85, 0.1)`. Selection applies `_highlight_color`, which tints toward green `(r*0.35, g*0.35+0.65, b*0.35)`.

**Modifier render passes** — `_paint_scene()` runs the following sequence per eye:
1. **Opaque pass**: bodies with `role` not in `{"background", "highlight_ghost"}` whose resolved color has alpha `>= 1.0` (the common case), rendered normally with full depth test and depth write.
2. **Translucent pass** (`color()`'s alpha `< 1.0`, any of `normal`/`show_only`/`highlight` role — e.g. `color([1,0,0,0.5])`): deferred out of the opaque pass (drawing it there would depth-write it fully opaque, discarding the alpha the fragment shader computed) and drawn afterward with `SRC_ALPHA/ONE_MINUS_SRC_ALPHA` blending and depth write disabled (depth test stays on, so opaque geometry still occludes it correctly). Sorted back-to-front by each buffer's object-space centroid (`cpu_v0.mean(axis=0)`) transformed to world space and measured against the eye position — an approximation (not true per-triangle sorting), sufficient to avoid the worst near/far swaps between multiple overlapping translucent bodies.
3. **Axes and labels**: rendered after the opaque+translucent passes with full depth write still active, so they are composited correctly under the subsequent transparent ghost pass — ghost geometry (at 0.2 alpha) composites over the axes, making the axes visible through ghost objects.
4. **Ghost pass** (`role="background"`, OpenSCAD `%`): `SRC_ALPHA/ONE_MINUS_SRC_ALPHA` blending, depth test LESS, depth write disabled, back-face culling on — the ghost appears only where no opaque geometry or axes occlude it. Color uses the body's own color at a fixed 0.2 alpha (its own `color()` alpha, if any, is not honored here). Background bodies are skipped by ray-cast picking.
5. **Highlight overlay pass** (OpenSCAD `#`): covers two sub-cases:
   - `role="highlight"` (top-level `#`): body is real geometry already rendered in the opaque pass. Re-rendered with polygon offset `(-1.0, -1.0)` shifting toward the camera so the overlay passes the LESS depth test, pink `(1.0, 0.08, 0.45, 0.35)`, blending on, depth write off.
   - `role="highlight_ghost"` (`#` used inside a CSG op like `difference()`): the body was consumed by the CSG kernel and is NOT in the opaque pass. Rendered as a pink ghost with back-face culling on, using the depth buffer written by the opaque pass, so it is occluded by surrounding solid geometry. No polygon offset needed.

`MeshBuffer.role` stores the role string so the renderer can classify each buffer. Background buffers are excluded from ray-cast picking in `ray_cast()`.

**Lighting**: Blinn-Phong shading with a key light, fill light, ambient term, and specular highlights (exponent 64, intensity 0.5). The key light direction is defined in view space as `[0.6, 0.8, 1.0]` and transformed to world space, so it follows the camera by default. Option+left-drag adjusts the light direction via azimuth (around viewport vertical Y axis) and elevation (around viewport horizontal X axis) offsets applied in view space before the world transform — the adjusted light stays fixed relative to the user's POV when orbiting.

**Axis ticks and labels**: each axis has perpendicular tick marks (X/Z ticks extend along Y; Y ticks extend along X). Ticks are one-sided, extending only in the positive perpendicular direction; minor ticks are ~24 px, major ticks ~48 px. When only one minor tick would fall between majors (`major_steps <= 2`), spacing is promoted so the minor interval becomes the new major and the old major becomes the label interval, keeping the axis visually dense. Tick labels are depth-aware, camera-facing textured billboards rendered in the 3D scene (not a QPainter screen-space overlay) — each `QImage` is rasterized once into a ModernGL texture and drawn as a unit quad oriented via `right`/`up` vectors from the view matrix. Labels are positioned on the negative perpendicular side (opposite the ticks). Labels and minor ticks on axes within 5° of end-on to the camera are suppressed (major ticks still draw). Before that hard cutoff, `_axis_density(camera)` (free function, `renderer.py`, pure math — no GL/Camera-instance coupling beyond taking a `Camera` argument, unit-tested in `test_renderer.py::TestAxisDensity`) thins labels and minor ticks per axis to avoid the alternative: evenly-world-spaced labels crowding into an ever-smaller on-screen span as the axis foreshortens, overlapping illegibly well before 5°. For each axis, `view_dir[ai]` (cosine of the angle between the camera's view direction and that world axis) gives foreshortening `sqrt(1 - view_dir[ai]**2)` — 1 when the axis is broadside (full density), shrinking toward 0 as it swings end-on. Half the reciprocal (doubling density vs. a plain 1/foreshorten stride), snapped to the nearest power of two, is the "show every Nth tick" stride — powers of two keep the visible subset nested as the camera rotates (every 4th-tick set ⊂ every-2nd ⊂ every tick) rather than jumping to an unrelated set each frame. `_render_axes` applies this stride via `_tick_is_drawn(k, major_steps, end_on, stride)` (pure, unit-tested in `test_renderer.py::TestTickIsDrawn`): at the hard end-on cutoff, only major ticks draw (unchanged, long-standing behavior — majors always show even dead-on end-on). Below that cutoff, stride applies uniformly to major *and* minor ticks alike, rather than exempting majors the way the hard cutoff does — `major_steps` (how often a major tick falls) and `stride` aren't related, so exempting majors there let an every-major-step tick land arbitrarily close to a kept minor tick while a stride-sized gap opened up elsewhere, a visibly inconsistent spacing bug. Requiring just `k % stride == 0` (majors included) keeps the drawn set a strict, evenly-spaced arithmetic subsequence regardless of where `major_steps` falls. `_axis_tick_world_points` applies the same per-axis stride to labels independently, since labels are keyed to the label-spacing tier (which may differ from the tick marks' major/minor tiers, not to the tick-mark loop's own index) — that loop only ever had the one tier, so it was never susceptible to this bug. Positive axis lines are colored (red/green/blue); negative axes are gray.

**Mouse-wheel zoom**: fixed ±1% step (`factor = 1.01` or `0.99`) with a 5-unit deadspot on `angleDelta` to avoid jitter on near-zero deltas.

`main.py` then exits via `os._exit(code)` rather than `sys.exit(code)`/falling off the end of `main()`. This **skips Python's normal interpreter finalization**, which performs a final `gc.collect()` pass — and collecting nanobind-wrapped `m3d.Manifold`/`m3d.CrossSection` objects shortly after a background render `QThread` has been active can SIGSEGV (nanobind's object collection isn't thread-safe across a recently-active worker thread). `gc.disable()` does not prevent this, since CPython's finalizer forces a collection regardless. Because `os._exit()` skips `atexit`/destructors entirely, `closeEvent`'s explicit `QSettings.sync()` is required so window geometry/state are flushed to disk before exit.
