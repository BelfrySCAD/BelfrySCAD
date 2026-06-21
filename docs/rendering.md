# Threaded Rendering

Parse + evaluate runs in a background `QThread`. Two helper classes in `main_window.py`:

- **`_RenderWorker(QObject)`** — moved to the worker thread via `moveToThread`; does the parse/evaluate work; emits `logged`, `parse_errored`, `finished`, `done`
- **`_RenderCallback(QObject)`** — stays in the main thread; `@Slot` methods receive worker signals; Qt auto-detects the cross-thread boundary (`QueuedConnection`), so callbacks run on the main thread

**Do not connect worker signals to Python lambdas** — lambdas have no thread affinity, so Qt can't determine which event loop to post to. Always route through a `QObject` slot with known thread affinity.

**Cancellation**: `_render()` passes a `threading.Event` to the worker, which checks `cancel.is_set()` between major steps. A `render_id` counter increments per render; the callback discards results whose `render_id` no longer matches.

**Progress indicator**: a QLabel overlay centered in the viewport shows elapsed seconds and cycling dots (`.` → `..` → `...` → blank) during rendering, updated every 100ms via QTimer. A `WaitCursor` override is set/restored at the same time. The viewport geometry is cleared at render start so only the overlay is visible.

**Cancellation by user**: pressing Escape while a render is in progress sets the cancel event, hides the render overlay, and logs "Render cancelled." to the console.

**Elapsed time**: every render outcome logs its elapsed wall-clock time to the console via `_fmt_elapsed()` — formatted as `(Nms)` under 1000ms or `(N.NNNs)` at 1000ms and above. This applies to successful renders (alongside the bounding-box summary), no-geometry renders, eval errors, recursion-limit errors, and uncaught runtime errors.

**Job lifetime**: each `_render()` call appends `(worker, callback, thread)` to `self._render_jobs`, kept alive until `thread.finished` fires `_cleanup_job` (which removes the entry). Without this, Python could GC the worker/callback before `thread.started` fires, raising `AttributeError: Slot '_RenderWorker::run()' not found.`

**Shutdown and interpreter exit**: `MainWindow.closeEvent()` pauses every tab's `AnimatePane` (no new renders get queued), sets the cancel event, and waits (with a 5s deadline, pumping `QApplication.processEvents()`) for any `_render_jobs` threads to finish — Qt aborts if a `QThread` is destroyed while still running. It then saves settings (with an explicit `QSettings.sync()`) and clears `tab._bodies` / `viewport.load_geometry([])` to drop references to Manifold geometry via normal refcounting.

## Viewport visuals

**Object colors**: default geometry is yellow `(0.9, 0.85, 0.1)`. Selection applies `_highlight_color`, which tints toward green `(r*0.35, g*0.35+0.65, b*0.35)`.

**Lighting**: Blinn-Phong shading with a key light, fill light, ambient term, and specular highlights (exponent 64, intensity 0.5). The key light direction is defined in view space as `[0.6, 0.8, 1.0]` and transformed to world space, so it follows the camera by default. Option+left-drag adjusts the light direction via azimuth (around viewport vertical Y axis) and elevation (around viewport horizontal X axis) offsets applied in view space before the world transform — the adjusted light stays fixed relative to the user's POV when orbiting.

**Axis ticks and labels**: each axis has perpendicular tick marks (X/Z ticks extend along Y; Y ticks extend along X). Ticks are one-sided, extending only in the positive perpendicular direction; minor ticks are ~24 px, major ticks ~48 px. When only one minor tick would fall between majors (`major_steps <= 2`), spacing is promoted so the minor interval becomes the new major and the old major becomes the label interval, keeping the axis visually dense. Tick labels are depth-aware, camera-facing textured billboards rendered in the 3D scene (not a QPainter screen-space overlay) — each `QImage` is rasterized once into a ModernGL texture and drawn as a unit quad oriented via `right`/`up` vectors from the view matrix. Labels are positioned on the negative perpendicular side (opposite the ticks). Labels and minor ticks on axes within 5° of end-on to the camera are suppressed (major ticks still draw). Positive axis lines are colored (red/green/blue); negative axes are gray.

**Mouse-wheel zoom**: fixed ±1% step (`factor = 1.01` or `0.99`) with a 5-unit deadspot on `angleDelta` to avoid jitter on near-zero deltas.

`main.py` then exits via `os._exit(code)` rather than `sys.exit(code)`/falling off the end of `main()`. This **skips Python's normal interpreter finalization**, which performs a final `gc.collect()` pass — and collecting nanobind-wrapped `m3d.Manifold`/`m3d.CrossSection` objects shortly after a background render `QThread` has been active can SIGSEGV (nanobind's object collection isn't thread-safe across a recently-active worker thread). `gc.disable()` does not prevent this, since CPython's finalizer forces a collection regardless. Because `os._exit()` skips `atexit`/destructors entirely, `closeEvent`'s explicit `QSettings.sync()` is required so window geometry/state are flushed to disk before exit.
