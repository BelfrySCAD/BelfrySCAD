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

**Object colors**: default geometry is yellow `(0.9, 0.85, 0.1)`. Selection applies `_highlight_color`, which tints toward green `(r*0.35, g*0.35+0.65, b*0.35)`.

**Modifier render passes** — `_paint_scene()` runs three passes per eye:
1. **Opaque pass**: all bodies with `role != "background"` rendered normally with full depth test and depth write.
2. **Ghost pass** (`role="background"`, OpenSCAD `%`): rendered after the opaque pass with `SRC_ALPHA/ONE_MINUS_SRC_ALPHA` blending, depth test enabled (LESS), depth write disabled — the ghost appears only where no opaque geometry occludes it. Color uses the body's own color at 0.2 alpha. Background bodies are skipped by ray-cast picking.
3. **Highlight overlay pass** (`role="highlight"`, OpenSCAD `#`): rendered after the opaque pass with depth function `<=` and depth write disabled, using a fixed pink color `(1.0, 0.08, 0.45, 0.35)`. Highlight bodies are already in the opaque pass; this pass adds the pink glow on top.

`MeshBuffer.role` stores the role string so the renderer can classify each buffer. Background buffers are excluded from ray-cast picking in `ray_cast()`.

**Lighting**: Blinn-Phong shading with a key light, fill light, ambient term, and specular highlights (exponent 64, intensity 0.5). The key light direction is defined in view space as `[0.6, 0.8, 1.0]` and transformed to world space, so it follows the camera by default. Option+left-drag adjusts the light direction via azimuth (around viewport vertical Y axis) and elevation (around viewport horizontal X axis) offsets applied in view space before the world transform — the adjusted light stays fixed relative to the user's POV when orbiting.

**Axis ticks and labels**: each axis has perpendicular tick marks (X/Z ticks extend along Y; Y ticks extend along X). Ticks are one-sided, extending only in the positive perpendicular direction; minor ticks are ~24 px, major ticks ~48 px. When only one minor tick would fall between majors (`major_steps <= 2`), spacing is promoted so the minor interval becomes the new major and the old major becomes the label interval, keeping the axis visually dense. Tick labels are depth-aware, camera-facing textured billboards rendered in the 3D scene (not a QPainter screen-space overlay) — each `QImage` is rasterized once into a ModernGL texture and drawn as a unit quad oriented via `right`/`up` vectors from the view matrix. Labels are positioned on the negative perpendicular side (opposite the ticks). Labels and minor ticks on axes within 5° of end-on to the camera are suppressed (major ticks still draw). Positive axis lines are colored (red/green/blue); negative axes are gray.

**Mouse-wheel zoom**: fixed ±1% step (`factor = 1.01` or `0.99`) with a 5-unit deadspot on `angleDelta` to avoid jitter on near-zero deltas.

`main.py` then exits via `os._exit(code)` rather than `sys.exit(code)`/falling off the end of `main()`. This **skips Python's normal interpreter finalization**, which performs a final `gc.collect()` pass — and collecting nanobind-wrapped `m3d.Manifold`/`m3d.CrossSection` objects shortly after a background render `QThread` has been active can SIGSEGV (nanobind's object collection isn't thread-safe across a recently-active worker thread). `gc.disable()` does not prevent this, since CPython's finalizer forces a collection regardless. Because `os._exit()` skips `atexit`/destructors entirely, `closeEvent`'s explicit `QSettings.sync()` is required so window geometry/state are flushed to disk before exit.
