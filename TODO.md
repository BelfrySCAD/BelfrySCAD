# TODO

- NURBS parameters a bare point list cannot carry: weights,
  knots/multiplicities, `type="open"`, and `nurbs_interp`/
  `nurbs_interp_surface`'s derivative/curvature/normal/edge constraints.
  Curves (Path viewer) and surfaces (Grid viewer) are done without them,
  drawn by `nurbs.py`'s port of BOSL2's `nurbs.scad`; extend that port.
- Notional 2D layout editor: place multiple 2D shapes, apply transforms
  and CSG to them, then extrude by part.
- Extrude gizmo for 2D shapes selected in the main viewport, with and
  without centering.
- Follow-ups from #554, measured on an M1, none of them the warning flood
  itself (which is fixed):
  - **Cancel cannot stop a silent runaway.** A render is interrupted only
    through the evaluator's echo callback, so a script that loops for a long
    time without printing anything runs to the end. Needs a cancel check
    inside openscad_cpp_evaluator.
  - **Module recursion uses ~5 GB before it stops.** `module m() { m(); }`
    ends with "Recursion too deep" after 2.6 s, peaking at about 5 GB RSS;
    the function case stops in 0.3 s. Enough to look like a hang on a
    smaller machine. Evaluator-side.
  - **60,000 bodies stall the UI for 6.7 s** when the finished render is
    handed to the UI thread -- `for (i=[1:60000]) cube(1);` with the
    warnings removed from the picture. The geometry upload, not the console.
- Colour picker for colour literals in the editor. The swatch tooltip
  shipped (`color_literals.py`, `docs/editor.md`); this is the other half —
  editing one. Same shape as **Choose Font…** (`font_picker.py`): its own
  top-level context-menu item and a write-back through `replace_span` +
  `source_edited_externally`, which renders. `find_color_literal` already
  gives the span and the colour, so the fiddly part left is writing the
  value back in the spelling it was found in — a name stays a name only if
  the picked colour has one, and a vector's components are 0-1.

- Move BOSL2's `Regressions` job to `belfryscad --test`. It is the last
  thing in that repo still downloading the OpenSCAD 2021.01 AppImage;
  `--docsgen` and `--mdimggen` took over `CheckDocs` and `CheckTutorials`
  in BOSL2 #2034. All 909 of its tests now pass on this evaluator (exit 0,
  ~82s serial, against openscad-test's 8.3s for 226 with 5-way
  parallelism), so the swap is a workflow edit rather than a porting job:
  drop the AppImage, `libfuse2` and `pip install openscad-test`, and run
  `belfryscad --test tests/*.scadtest`. Needs belfryscad with
  openscad_cpp_evaluator >= 1.3.0.

- Accelerate textured extrusions and sweeps. (Sweeps take 2D paths or regions;
  extrusions take 2D geometry.) They are slow because the work happens in the
  OpenSCAD language. Measured 2026-09-18 against BOSL2 @ c4067293: a textured
  `linear_sweep` is **21x** a plain one of the same shape (2853 ms vs 137 ms),
  and 3.93 s of that 4.18 s wall is script self-time, so Manifold is barely
  involved. The two heavy forms have *different* bottlenecks:

  - **textured `linear_sweep` / `rotate_sweep`** — 49.4% of script time is
    `polygon_triangulate()` ear-clipping the end caps. Proven by re-running
    with `caps=false`: 2853 ms -> 1813 ms and `_get_ear` / `_none_inside` /
    `_tri_class` vanish from the profile entirely.
  - **textured `path_sweep`** — triangulation is 0.4%; the cost is texture
    *sampling*: 26.3% in one function literal in `_textured_point_array`
    (`skin.scad:5309`), plus `bilerp` (121,860 calls), `mean` -> `sum`.

  The predicate speedups elsewhere in this file do **not** rescue these: they
  bought −25% on a plain `linear_sweep` but only −4% on the textured one.
  The time is algorithmic, not validation.

  Three functions would do most of it. Each replaces a BOSL2 userspace
  implementation whose measured cost is given:

  1. **Ear-clipping triangulation of a 2D or near-planar 3D region.**
     Replaces `polygon_triangulate()` (`geometry.scad:2044-2163`), which is
     **O(n²)** in script — measured 25 ms / 85 ms / 324 ms for a 100 / 200 /
     400-vertex polygon. Must tolerate *near*-planar 3D input, since
     `vnf_triangulate()` feeds it faces that are only approximately coplanar.
     Note the existing `render()` extension already returns Manifold's own
     triangulation for 3D, but for a 2D object it returns `vertices` and
     leaves **`faces` undef** (verified), so there is no way to reach a
     triangulator from script today. Filling that field in may be the smaller
     change — it is a missing field on an existing extension rather than a new
     builtin.
  2. **Vertex deduplication on a VNF/obj** (the `object()` shape `render()`
     returns), renumbering face indices accordingly and returning an obj.
     Replaces `vnf_merge_points()` (`vnf.scad:1288`): measured **~207 ms for a
     3200-face mesh**, roughly linear but at ~65 us per face, which is a
     colossal constant for what is a hash-and-renumber pass in C++.
  3. **Boundary-edge detection on a partial VNF** — which edges have exactly
     one adjacent face, and are therefore the rim of a hole. Replaces
     `vnf_boundary()` (`vnf.scad:2272`): measured **493 ms for the same
     3200-face mesh**. Half-edge counting with a hash map; O(n) and
     near-instant natively.

  Remaining after all three, for whoever gets there: a capless textured sweep
  still spends 27.6% of its script time inside a *single* `vnf_vertex_array`
  call (450 ms of list building) and 10% in `vnf_join`. That is mesh assembly,
  a fourth problem, and it needs its own measurement before anyone designs for
  it. The `path_sweep` sampler likewise has no obvious builtin: a per-sample
  callback into script is the same shape as the `warp()` experiment, where the
  builtin lost to four lines of userspace.

## OpenSCAD GUI parity gaps

Surveyed 2026-09-17 against openscad/openscad `master` @ 4c1d47946 (2026-09-16),
by diffing both menu trees, dock widgets, preference toggles and `src/gui/`
module lists. Preview is deliberately out of scope (see CLAUDE.md). Nothing here
is a commitment to build it — it is the list of what is not there.

### Whole subsystems with no equivalent

- **3D-mouse / game controller input** (`src/gui/input/`) — SpaceNavigator,
  HIDAPI, joystick, gamepad and DBus drivers, plus the Axis/Button/Mouse
  configuration pages in Preferences. Designed; see "Input devices" below.
- **3D Print** (`PrintService`, `PrintInitDialog`, `ExternalToolInterface`) —
  remote print services and handing the model off to a local slicer.
- **Development-snapshot update channel** (`AutoUpdater`'s snapshot option).
  The release check itself is done (see below); there is nothing to point a
  snapshot channel at until snapshots are published somewhere.
- **Welcome/launching screen** (`LaunchingScreen`) — recent files and examples
  on startup.
- **Viewport Control dock** (`ViewportControl`) — numeric camera entry
  (eye, centre, rotation, FOV) instead of dragging.
- **Color List dock** (`ColorList`) — browsable list of OpenSCAD's named colours.

**Done since this list was written:**

- **Editor autocompletion** (`ScadApi`). This entry claimed the editor had
  "no completer to hang this on" and called it the largest item here. That
  was wrong when written — taken from the OpenSCAD side of the diff without
  reading `editor.py` closely enough. A `QCompleter` was already in place
  and already fed both the builtin word sets and every name the parsed
  scope knows, which with BOSL2 included is 948 functions and 271 modules.
  What was genuinely missing was the argument hint, added in #528 (issue
  #517): typing `(` now tooltips the call's parameter list, read from the
  builtin table for builtins and from the declaration itself for everything
  else. See `window/signatures.py`.
- **Go to Definition on a builtin** (#526, issue #525) now opens the
  language reference instead of reporting no definition, and misses reach
  the status bar rather than only the console.
- **Update check** (`AutoUpdater`). Help ▸ Check for Updates… (#557,
  v1.46.0) asks GitHub's `releases/latest` on demand, and the same check runs
  quietly at startup at most once a day, behind the Editor preference "Check
  for a new release when BelfrySCAD starts" (`app/checkForUpdates`, on by
  default). The startup check says nothing unless a newer release exists and
  offers Skip This Version (`app/skippedUpdate`). See
  `window/update_check.py`. Not done: installing in place — the dialog links
  to the release page, and there is no macOS installer to link to.

**Decided against — do not re-propose:**

- **Error Log dock** (`ErrorLog`). The console already has clickable anchors,
  so the navigation that makes OpenSCAD's dock useful is already there; all a
  dock would add is a second, filterable copy of the same messages. Declined
  2026-09-18.

### File menu

- Reload (manual; the file-watcher path exists but there is no command)
- Save a Copy, Save All, Show Library Folder
- Export formats we cannot write: **DXF**, **CSG**, **POV-Ray**
- Export as Image — PNG exists in the CLI (`-o out.png`) but has no GUI item
- The whole Python submenu (venv select/create, revoke trusted files);
  upstream OpenSCAD now ships Python scripting

### Edit menu

- Move Line Up / Move Line Down
- Convert Tabs to Spaces
- Bookmarks: toggle, jump to next, jump to previous (nothing in the source)
- Jump to next error
- Use Selection for Find
- Insert Template
- Increase / Decrease Font Size as commands (size is preference-only today)
- Show Next Tab / Show Previous Tab as menu items
- Copy viewport image / translation / rotation / distance / FOV to the
  clipboard — this is how people paste a `$vpt`/`$vpr` back into a script

### Design menu

- 3D Print, Check Validity, Display AST, Display CSG Products
  (`Display CSG Tree` is covered by `Dump CSG Tree to Console`)

### View and viewport

- Thrown Together as a **view mode** (preview-adjacent but distinct:
  backfaces in magenta for CSG debugging). Note the docs renderer *does*
  honour the `ThrownTogether` example flag as of #527 (issue #524), and the
  viewport already draws backfaces magenta and shades them (#520, issue
  #519) — so what is missing is only the View-menu toggle that forces that
  display on for an otherwise-closed model, not the rendering behind it.
- Center, Reset View, an explicit Orthogonal item (we have a Perspective toggle)
- Separate Hide Editor toolbar / Hide 3D View toolbar (we have one Show Toolbar)
- Show Warnings and Errors in 3D View
- Viewport info overlays: Camera, Bounding Box, Measurement Area

### Editor behaviour

- Highlight current line
- Number scroll via mouse wheel — scroll over a numeric literal to increment it
- Ctrl/Cmd-mouse-wheel zooms text
- Backspace unindents

(Line numbers and brace matching are already present.)

### Help, Window and preferences

- Cheat Sheet, online and offline; Offline Documentation; Library Info dialog
- Next Window / Previous Window cycling, and the Jump To window list
- Preference toggles with no equivalent: bring window to front after automatic
  reload; play a sound on render complete; clear console before render; check
  parameter range for builtin modules; UI localization; docking/undocking helper
  widgets into separate windows; always show welcome / export / print dialogs

Preferences has four tabs (Editor, Viewport, Render, AI) against OpenSCAD's seven
pages, which is where most of that last group comes from.


## Input devices: 3D mice and game controllers

Designed 2026-09-18 against OpenSCAD `master` @ 4c1d47946. Nothing below is
hardware-verified — there is no SpaceMouse and no gamepad on the dev machine.

### Shape

Two readers feeding one Qt-free binding layer, **not** OpenSCAD's five drivers:

- `hidapi` on a QThread for the SpaceMouse (blocking `hid_read`), driven by a
  **data** table of 21 devices — channel / byte offsets / sign per axis, and
  button bit positions. OpenSCAD hardcodes two decoders for every device it
  lists and therefore gets several of them wrong; it is also missing SpaceMouse
  Enterprise (`256f:c633`), Pro Wireless BT (`c638`) and SpaceMouse Module
  (`c641`) entirely.
- `pygame-ce`'s SDL GameController polled from a **QTimer on the Qt main
  thread** for gamepads. SDL's controller database is the reason not to do pads
  over raw HID: Xbox pads over USB on Windows are XInput and are not readable
  as HID at all, and DualSense sends different reports over USB and Bluetooth.
- `Preferences ▸ Controls` — an **action-centric** scrollable list of commands
  each with a binding cell, plus per-device profiles and presets. Deliberately
  not OpenSCAD's axis-centric grid, which is why theirs is unusable.

**DBus is not needed.** OpenSCAD's `DBusInputDriver` is not a device driver at
all — it registers `org.openscad.OpenSCAD` on the session bus so other programs
can drive the camera. Remote control, Linux-only, unrelated.

### Prerequisite, gates everything else

BelfrySCAD's QActions have no object names — `main_window.py` builds them inline
through `_add_action` / `_add_checkable`, so there is no stable id to bind to.
Add an id argument to both and register into a dict. Mechanical, but it touches
menu construction in a 246 KB file, so land it on its own.

### Packaging chores

- `hidapi` has **no `win_arm64` wheel** and `windows-11-arm` is a release
  target — needs the `PIP_FIND_LINKS wheelhouse` build-from-source path
  `release.yml` already uses for arm64. `pygame-ce` does ship win_arm64.
- Flatpak gets **no device access** by default: briefcase's finish-args stop at
  `device=dri`. Needs `finish_arg."device=all" = true` under
  `[tool.briefcase.app.belfryscad.linux.flatpak]`. Granular `device=input`
  requires flatpak >= 1.15.6, so `device=all` is the portable answer.
- Ship a Linux udev rule for vendors `256f` and `046d`
  (`SUBSYSTEM=="hidraw" ... TAG+="uaccess"`, priority below 73), or HIDAPI
  needs root.
- Import both dependencies **lazily**, inside the readers. 17.5 MB of binary
  wheels for a feature most users never touch; if one fails to load the
  Controls pane says "no devices" and nothing else notices.

### Gotchas that will otherwise be rediscovered the hard way

- **`SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1` before SDL init.** SDL suppresses
  joystick input when its window is unfocused, and ours is a dummy that is
  never focused. Without it the pad is silently dead, with no error.
- **SDL needs `SDL_VIDEODRIVER=dummy` + `pygame.display.init()`**:
  `pygame.event.pump()` otherwise raises "video system not initialized", and
  pump is what refreshes joystick state.
- **On macOS a denied HID open is a bare `open failed`** with no permission
  dialog — it looks exactly like a broken cable. The gate is narrow (keyboards,
  mice, trackpads; not usage `0x08` multi-axis or `0x05` gamepad), so a puck
  will probably open ungranted, but call `IOHIDCheckAccess` /
  `IOHIDRequestAccess` (plain ctypes against IOKit.framework, 0/1/2 =
  granted/denied/unknown) regardless, so the failure explains itself.
- **The vendor driver must be out of the way**: 3DxWare disabled entirely on
  macOS, its service stopped on Windows. Accepted cost of raw HID — see the
  decision below — but it means a user's SpaceMouse stops working in Fusion and
  SolidWorks while they use ours. Say so in the UI.
- **Universal Receiver (`256f:c652`) does not work on macOS or Windows.** Five
  HID interfaces, opens fine, reads return nothing; unresolved upstream since
  2016 (libusb/hidapi#136). Detect it and say so rather than chasing it.

### Decision: raw HID, not 3Dconnexion's SDK

The SDK (`3DconnexionClient` on macOS, TDx on Windows) coexists with the vendor
driver and inherits the user's own 3DxWare settings — but it is proprietary,
needs registration, and has nothing for Linux, so it would mean three code
paths instead of one. Raw HID covers all three release platforms with one
implementation. Revisit only if the driver-conflict support burden proves worse
than a third code path.

### Dominance filter

A dropdown, not a checkbox: `off` / `single-axis` / `pan-zoom-orbit` (default).
3Dconnexion's shipped "Dominant" is `single-axis` — greatest of six, other five
zeroed. `pan-zoom-orbit` splits into three gesture groups — pan (Tx, Ty), zoom
(Tz), orbit (Rx, Ry, Rz) — and passes the strongest group through whole, which
keeps diagonals working while stopping a diagonal pan from also dollying.
Compare groups by **Euclidean norm, not max**: a 45° diagonal reads `0.71·m`
under max and loses to a smaller pure Tz, defeating the entire point. Needs two
calibration constants and hysteresis on all three crossovers. SpaceMouse only —
never shown for a gamepad.

### Still open, needs hardware

- Does SDL enumerate a SpaceMouse as a plain 6-axis joystick on all three
  platforms? **If yes the `hidapi` dependency and its win_arm64 problem both
  disappear.** Cheap to check first; do not bet the design on it, since
  per-device scaling and printed button legends are why the dedicated path
  exists.
- Do the 8 device entries marked `verified = false` decode correctly? They
  inherit a sibling's layout because OpenSCAD lists the device but nobody here
  has held one.
- Does `SDL_VIDEODRIVER=dummy` disturb Qt's event loop or NSApplication on
  macOS in a real window?
- Does SDL's internal hidapi contend with ours over the SpaceMouse?
