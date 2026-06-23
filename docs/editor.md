# Editor & UI Reference

Implementation details for the code editor, layout, preferences, and UI conventions. See `CLAUDE.md` for core architecture.

## Error Display

Parse errors get a squiggly underline at the error location via `QTextCharFormat` with `SpellCheckUnderline` style, applied as an extra selection on the `QPlainTextEdit`. Also reported in the console.

## Find / Replace

`CodeEditor.show_find(replace=False)` opens a `FindBar` overlay parented to the editor, top-right corner. `show_find(replace=True)` also shows the replace row. Triggered by Cmd+F / Cmd+H.

`FindBar` features:
- Plain-text and regex search (`.*` toggle), case-sensitive toggle (`Aa`)
- All matches highlighted pale yellow; current match orange with white text
- Match count label ("N of M"); prev/next navigation (◀ ▶ or Shift+Enter / Enter)
- Replace one (current match) and Replace All (works backwards through matches to preserve positions, wrapped in one `beginEditBlock`/`endEditBlock` for one undo step)
- A single-word selection present when Find opens pre-populates the search field
- Escape closes the bar and returns focus to the editor
- `_find_selections` is a separate extra-selection list on `CodeEditor`, inserted between `_selection_extra` and `_exec_selection` in `_refresh_extra_selections`
- Document changes while open auto-rerun the search via `document().contentsChanged`

## Indent Guides

Faint vertical lines drawn inside each indented line's leading whitespace, every `_indent_size` columns, except at the column of the first non-whitespace character. Implemented as `_IndentGuides(QWidget)`, a transparent overlay on `CodeEditor.viewport()`, created before `_ColumnGuide` so the column guide renders on top. Repainted on `document().contentsChanged` and `set_indent_size()`.

Paint logic: for each visible block, count leading spaces `n`; draw guides at `indent_size, 2*indent_size, …` while `col < n` (strictly less, so the column at `n` is never drawn). Empty/unindented lines skipped. Uses `QFontMetricsF` for sub-pixel accuracy.

## Column Guide

A faint vertical line at column 80, implemented as `_ColumnGuide(QWidget)`, a transparent overlay on `CodeEditor.viewport()`:
- `WA_TransparentForMouseEvents` + `WA_TranslucentBackground` so only the line pixel shows and mouse events pass through
- `update_geometry()` keeps the overlay sized to the full viewport rect; called from `CodeEditor.resizeEvent()`
- x position = `cursorRect(cursor_at_pos_0).x() + QFontMetricsF(font).horizontalAdvance('0' * 80)`. `QFontMetricsF` (not `QFontMetrics`) is required — the integer version rounds character width up by ~0.2px, accumulating to ~2 columns of error over 80 characters.

## Code Folding

Fold markers (▼ unfolded, ▶ folded) appear in the right section of the line-number gutter; clicking calls `toggle_fold(block_number)`.

`_compute_fold_regions(doc)` returns `{open_block: close_block}` via two passes:
1. **Delimiter matching** — `{…}`, `(…)`, `[…]` pairs; a region only forms when opener and closer are on different lines
2. **Indentation continuation** — any non-empty line followed by at least one more-indented non-empty line; covers function bodies, ternary chains, nested list comprehensions, etc.; `setdefault` lets pass-1 delimiter regions take precedence

`_fold_regions` recomputes lazily on first paint after `_fold_dirty` is set by `_on_doc_changed`. `_fold_busy` guards against re-entrant recomputation and against `_on_doc_changed` resetting `_fold_dirty` mid-toggle.

`_set_range_visible(start_bn, end_bn, visible)` sets `QTextBlock.setVisible()` on each hidden block, then a no-op `cursor.beginEditBlock(); cursor.endEditBlock()` forces `QPlainTextDocumentLayout` to recalc block heights (required for visibility changes to take effect).

Fold indicators are drawn with `painter.drawPolygon(QPoint[])` — `QPainterPath.drawPath` was invisible at small sizes on macOS; `drawPolygon` is reliable.

## Go to Definition

Right-click an identifier shows "Go to Definition of 'name'", only for words matching `[A-Za-z_][A-Za-z0-9_]*`.

`CodeEditor.go_to_definition_requested` (emits the word) connects to `MainWindow._go_to_definition(tab, word)` per tab.

`_go_to_definition` requires a cached `root_scope` on the tab (set after every successful `build_scopes()`, both in the render worker and `_start_debug()`); if absent, logs a message asking the user to render first.

Lookup order: `scope.lookup_variable(word)` → `scope.lookup_function(word)` → `scope.lookup_module(word)`, first non-None wins. Built-in modules return `None` from `lookup_module` and are skipped.

The definition node's `.position.origin` gives the source file path, `.position.line` the 1-indexed line. Navigation:
- Same file (or origin `None` / untitled tab): scroll current editor to the line
- Different file: switch to a matching open tab by `file_path`, or open via `_create_and_add_tab()` (view-only, no render)

`_create_and_add_tab(path, text) -> DocumentTab` creates a fully-connected tab (viewport/debugger-pane signals, perspective, Go-to-Definition) and adds it to the UI. If the only existing tab is an empty, unmodified Untitled tab, it is replaced rather than kept alongside. Used by `_open_file`, `_open_recent`, `_go_to_definition`; not by `_new_document` (different setup path for blank tabs).

## Code Completion

`QCompleter` with a `QStringListModel` provides prefix-based autocomplete. The popup appears after 2+ identifier characters are typed and hides when there are no matches or an exact match.

**Word list**: 96 built-in names (keywords, modules, functions, constants, and `$`-variables like `$fn`/`$fa`/`$fs`/`$t`/`$children`/`$vpt`/`$vpr`/`$vpd` from `_BUILTIN_WORDS`) plus user-defined names (including user `$`-variables) from the last successful `build_scopes()`. `update_user_names(scope)` extracts names from `scope.variables`, `scope.functions`, and `scope.modules` dicts; called from `_RenderCallback.on_ast_ready` after each successful parse.

**Key handling in `keyPressEvent`**: when the popup is visible, Enter/Tab accept the selected completion and Escape dismisses. Tab for indent only fires when the popup is not visible. Other keys pass through to the editor and then `_update_completer_popup()` refreshes the prefix/popup state.

`_text_under_cursor()` walks backward from the cursor to find the current identifier prefix (alphanumeric + underscore + leading `$`). `_insert_completion()` replaces the prefix with the selected completion.

## Undo/Redo

Code edits and gizmo drags are undo/redo-able via Qt's `QUndoStack`. Each operation is a `QUndoCommand` subclass:

- **Code edits**: `TextEditCommand` stores before/after document state and calls `QPlainTextEdit.setPlainText()` on undo/redo
- **Gizmo ops**: `GizmoCommand` stores before/after source text and re-triggers a render on redo

All Cmd+Z / Cmd+Shift+Z route through `QUndoStack`, which disables `QPlainTextEdit`'s built-in undo (`setUndoRedoEnabled(False)`).

## Console Output

The console displays:
- Parse errors (file/line/col from AST metadata)
- On each render: bounding box of the resulting mesh and current camera position

## Animation

The **Animate** dock (`AnimatePane` in `window/animate.py`, one per `DocumentTab`) implements OpenSCAD's [`$t` animation](https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Animation):

- **Time / FPS / Steps** fields: Time shows the current `$t` (read-only display, but editable — typing a value jumps to the nearest step, clamped to `[0, 1 - 1/steps)`); FPS (1-1000) sets the playback rate; Steps (1-1,000,000) sets the number of frames in one cycle. `$t = step / steps` for `step` in `0..steps-1`. Tab/Shift+Tab move between these three fields, and Enter confirms an edit (`QLineEdit.editingFinished`, which fires on Return as well as focus-out). `AnimatePane` installs an event filter on each field to accept the `ShortcutOverride` event for Tab/Backtab — otherwise the main window's Indent/Undent actions (bound to Tab/Shift+Tab as window-wide shortcuts for the code editor) would consume the key before normal focus-navigation gets it.
- **Big play/pause button** and the **transport row** (First / Previous / Play / Pause / Next / Last) drive playback. Any non-playback transport action pauses playback first.
- **Dump Pictures** checkbox: when checked and Play is pressed, NeuSCAD prompts (once per tab) for a destination folder via a folder picker, then saves each frame of one full animation cycle as `frameNNNN.png` (via `Viewport.grabFramebuffer()`), pausing automatically after frame `steps - 1` rather than looping.

Each frame change re-renders the active tab with `$t` set accordingly — `MainWindow._viewport_params(tab)` includes `"$t": tab.animate_pane.current_t()`, merged into the evaluator's dynamic context alongside `$vpt`/`$vpr`/`$vpd` (see `docs/evaluator.md`). During playback the viewport camera is **not** auto-fit to the model's bounding box on each frame (unlike a normal Render), so the camera stays put across frames. Switching tabs pauses any other tab's animation, since playback re-renders the active tab on every frame.

## Keyboard Shortcuts

Standard platform conventions apply throughout. Custom shortcuts:

| Key | Action |
|---|---|
| Cmd+1 | Toggle Show Edges |
| Cmd+2 | Toggle Show Axes |
| Cmd+3 | Toggle Show Crosshairs |
| Cmd+4 | Top view |
| Cmd+5 | Bottom view |
| Cmd+6 | Left view |
| Cmd+7 | Right view |
| Cmd+8 | Front view |
| Cmd+9 | Back view |
| Cmd+0 | Isometric view |
| Cmd++ | Increase editor font size |
| Cmd+- | Decrease editor font size |
| Cmd+[ | Zoom Out |
| Cmd+] | Zoom In |
| Shift+Cmd+V | View All |
| Tab | Indent line/selection |
| Shift+Tab | Unindent line/selection |
| F5 | Debug |
| F6 | Render |
| F7 | Animate |
| Escape | Cancel in-progress render |

## Application Preferences

Preferences live under the `editor/` key group in `QSettings("NeuSCAD", "NeuSCAD")`, accessed via `load_preference(key, type)` / `save_preferences(dict)` in `preferences.py`.

| Setting | Key | Default |
|---|---|---|
| Font family | `editor/fontFamily` | `"Menlo"` |
| Font size | `editor/fontSize` | `13` |
| Indent size | `editor/indentSize` | `4` |
| Show column guide | `editor/showColumnGuide` | `True` |
| Column guide column | `editor/columnGuide` | `80` |

`MainWindow._apply_preferences()` reads all settings and pushes them to every open tab via `_apply_preferences_to_tab(tab, font, indent, show_guide, guide_col)`. Called on startup (end of `_restore_settings()`) and after the dialog is accepted. New tabs from `_new_document()` and `_create_and_add_tab()` also call it to inherit current settings immediately.

`CodeEditor.set_indent_size(n)` stores `_indent_size` and updates `tabStopDistance`. Indent/unindent logic lives in `CodeEditor._indent_lines()`/`_unindent_lines()`, called both from `keyPressEvent` (Tab/Shift+Tab) and the Edit menu actions. Both handle single-line and multi-line selections.

**Auto-indent on Return**: the new line inherits the current line's indentation. An extra indent level is added when the current line ends with `{`, `[`, `(`, or starts with the keyword `function` or `module`. Typing `}`, `]`, `)` on a whitespace-only line removes one indent level.

**Down arrow on last line**: pressing Down on the last line appends an empty line and moves the cursor to it, so the user doesn't need to move to the end and press Return to extend the file.

The Preferences action uses `QAction.MenuRole.PreferencesRole` so Qt places it in the macOS application menu (Cmd+,).

**Word Wrap** is a checkable Edit menu item stored at the top level in `QSettings` under `"wordWrap"` (default `False`), not under `editor/`. Persisted/restored in `closeEvent`/`_restore_settings` using the same `blockSignals` pattern as `perspective`, and applied via `_apply_word_wrap_to_tab(tab)` (also called for new tabs in `_new_document` and `_create_and_add_tab`).

## Syntax Highlighting

`OpenSCADHighlighter(QSyntaxHighlighter)` applies keyword/type/number/string/comment formatting via regex rules processed per block in `highlightBlock()`.

Multi-line `/* ... */` block comments use `QSyntaxHighlighter` block state tracking (`previousBlockState`/`setCurrentBlockState`) so highlighting persists across lines. Both `//` line comments and `/* */` block comments render in the same green (`#6A9955`).

## Startup Behavior

Opens with a single blank untitled document. When opening a file while the only tab is an empty, unmodified "Untitled" tab, that tab is replaced by the new file tab (in `_create_and_add_tab`).

## File Opening

Files can be opened in several ways:

- **File > Open** — standard file dialog
- **File > Recent Files** — reopens previously opened files; stale entries are auto-pruned
- **Drag and drop** — `.scad` files dropped onto the window open as new tabs
- **macOS file association** — `.scad` files opened from Finder (double-click or Open With) send a `QFileOpenEvent` to `NeuSCADApp`, which forwards to `MainWindow.open_file_by_path()`; the Info.plist declares `CFBundleDocumentTypes` for `.scad` via briefcase config in `pyproject.toml`
- **Command-line arguments** — `NeuSCAD foo.scad bar.scad` opens each file at launch

All paths converge on `open_file_by_path(path)`, which resolves the path and checks for an already-open tab before creating a new one.

## Quit Behavior

When the user quits the app and there are modified editors open, a Save/Discard/Cancel dialog is shown for each unsaved tab. Cancel aborts the quit.

## GUI Layout

```
┌──────────────────────────────────────────────────────────────┐
│  [New] [Open] [Export] | [Undo] [Redo] | [Render] [Debug] [Animate] │  ← toolbar
├──────────────────────────────────────────────────────────────┤
│  [file1.scad ×]  [file2.scad ×]  [+]                        │  ← tabs
├──────────────────────────┬──────────────────────────┬────────┤
│                          │                          │   T    │
│                          │                  [cube]  │   R    │
│   QPlainTextEdit             │   3D Viewport            │   S    │
│   Code Editor            │                          │   ·    │
│                          │                          │   ·    │
├──────────────────────────┴──────────────────────────┴────────┤
│  Console                                                      │
├──────────────────────────────────────────────────────────────┤
│  $vpt = [0.00, 0.00, 0.00]  $vpr = [55.00, 0.00, 25.00]  $vpd = 50.00      0 FPS  │  ← status bar
└──────────────────────────────────────────────────────────────┘
```

- **Toolbar**: across the top — New, Open, Export | Undo, Redo | Render, Debug, Animate
- **Tabs**: one per open file; can be torn off into separate windows
- **Code editor**: left pane (QPlainTextEdit)
- **3D viewport**: center pane; always visible; contains:
  - Cube gizmo in a corner for view angle control
  - Camera icon next to the cube gizmo; clicking opens a popup with current viewport translation, rotation, distance, and FOV
- **Tools strip**: right of the viewport — Translate, Rotate, Scale, and future tools
- **Console**: bottom pane
- **Status bar**: bottom strip; shows 3D coordinates of the last clicked point on the mesh

The code editor, console, debugger, and animate pane are `QDockWidget` instances — dockable to any side or floatable, with position/visibility persisted via `QSettings("NeuSCAD", "NeuSCAD")` (`saveState()`/`restoreState()`). Object names: "EditorDock", "ConsoleDock", "DebuggerDock", "AnimateDock". The Animate dock starts hidden; open via the Animate toolbar button (F7) or View ▸ Show Animate.

Scale markers are tick marks along the viewport axes showing distance units (Show Scale Markers), each labeled with its distance value. Labels are rendered in 3D as camera-facing textured billboards: each tick's number is rasterized to an RGBA texture (cached by string) and drawn on a small transparent quad positioned just past the tick, so labels respect depth (occluded by geometry in front of them) and scale with zoom like the tick marks themselves. An axis whose line is nearly end-on to the camera has its tick labels suppressed (its ticks would otherwise overlap near the origin). Show Edges renders the full triangulation wireframe via `GL_POLYGON_OFFSET_FILL` on the solid pass (pushes fill surfaces away from camera), then draws edges at true depth in a second pass — avoids z-fighting on coplanar faces while keeping hidden edges correctly occluded. Show Crosshairs draws four white diagonal lines (the four space diagonals of a unit cube) crossing at the camera target, each extending `camera.distance * 2.5 / 12`. Perspective/orthographic toggle uses `camera.orthographic`, persisted in QSettings.

**Restoring settings to checkable actions**: wrap `setChecked()` in `blockSignals(True/False)`, then call the handler explicitly — avoids double-invocation (signal + explicit call) and ensures the handler fires even if the stored value matches the default. Pattern used in `_restore_settings`:
```python
self._act_perspective.blockSignals(True)
self._act_perspective.setChecked(perspective)
self._act_perspective.blockSignals(False)
self._toggle_perspective(perspective)
```

**New tabs must inherit viewport settings**: every new `DocumentTab` gets a fresh default `Viewport`/`Camera`. After connecting signals and before adding to the tab widget, call `_apply_perspective_to_tab(tab)` (and any future per-viewport settings) to match the current UI state. The `hasattr(self, '_act_perspective')` guard covers `_new_document()` being called during `__init__` before `_setup_menus()` finishes — in practice `_setup_menus()` runs first, so it's defensive only.

## Data Viewers

Implemented in `src/neuscad/window/data_viewers.py`. Three viewer dialogs for inspecting evaluated data, opened from the debugger's variable context menu via `build_viewer_menu()`.

### _SimpleViewport (QOpenGLWidget)

Base class for viewer 3D viewports. Orbit camera (azimuth/elevation/distance/target), own ModernGL shader programs (line, mesh with backface_color, edge, label). Axis rendering with ticks and labels ported from the main `SceneRenderer` (`_nice_spacings`, `_fmt_tick`). `schedule_load(fn)` defers geometry uploads until after `initializeGL()`. `set_view_preset()` for named views (top/bottom/left/right/front/back/isometric). Supports perspective/orthographic toggle, axes toggle, edge toggle. Mouse-centered zoom (wheel shifts target toward cursor). No custom `QSurfaceFormat` — macOS multisampling causes compositing artifacts.

Keyboard shortcuts (Cmd+0–9 views, Cmd+1 wireframe, Cmd+2 axes, Shift+Cmd+2 perspective) are handled by the main window's `ApplicationShortcut`-context QShortcuts, which check `QApplication.activeWindow()` and forward to the active viewer viewport via `_active_viewer_viewport()`.

### ListViewer (QDialog)

`QTableWidget` with key/value columns. Recursive drill-down via context menu for nested lists/dicts.

### VNFViewer (QDialog)

`QSplitter`: `_VNFViewport` on left, `QTabWidget` (Vertices / Faces tabs) on right. Starts in the main window's current perspective mode.

- **Vertices tab**: `QTableWidget`, 0-indexed rows, X/Y/Z columns sized for 6 digits. Multi-select (extended selection). Selected vertices shown as blinking (red↔white, 250ms) axis-aligned octahedron markers in the viewport, ~7px screen size regardless of zoom (rebuilt on wheel). Hovering a highlighted vertex shows a tooltip with index and coordinates.
- **Faces tab**: `QTableWidget`, 0-indexed rows, single "Vertex Indices" column. Selecting a face highlights it green in the viewport (polygon offset overlay), deselects all vertices, and selects the face's referenced vertices.
- **Viewport**: `_VNFViewport(_SimpleViewport)`. Backfaces rendered magenta. Face picking via vectorized Moller–Trumbore ray–triangle intersection. Clicking a face in viewport emits `face_clicked` signal, switches to Faces tab, and selects the row.

### PathViewer (QDialog)

`QSplitter`: `_PathViewport` on left, vertex table on right. "Close Path" checkbox (20px left padding) and Dismiss button (20px right padding) below.

- **Vertex table**: `QTableWidget`, 0-indexed rows, X/Y/Z columns. Extended multi-select. Selecting rows highlights the corresponding vertices in the viewport.
- **Viewport**: `_PathViewport(_SimpleViewport)`. Black lines at 2× width. Axis-aligned octahedron vertex markers (~7px screen size, rebuilt on zoom): green for unselected, red↔white blink (250ms) for selected. Hovering a marker shows a tooltip with index and coordinates. Clicking a marker in the viewport emits `vertex_clicked`, deselects all, and selects the clicked vertex.
- **Close Path**: checkbox toggles whether the last vertex connects back to the first.
- **Bezier**: checkbox switches line rendering from straight segments to cubic Bezier curves. Every 4 points form one cubic segment (P0, C1, C2, P3) with shared endpoints between consecutive segments. Open paths require 3k+1 points; closed paths require 3k points (last segment wraps to first). Each curve is tessellated into 32 line segments.
- 2D paths (all Z=0) start in top-down orthographic; 3D paths start in perspective orbit.

## Menu Structure

**File**: New / Open… / Open Recent ▶ / Close / Save / Save As… / — / Export… / — / Quit

**Edit**: Undo / Redo / — / Cut / Copy / Paste / Select All / — / Expand Selection / Contract Selection / — / Indent / Undent / Comment / Uncomment / — / Find… / Find & Replace… / — / Word Wrap (checkable)

**Design**: Render / — / Flush Caches / — / Insert Primitive ▶ (Cube, Sphere, Cylinder, Cone, …) / Boolean Operation ▶ (Union, Difference, Intersection) *(behavior of Insert Primitive and Boolean Operation deferred)* / — / Use Library ▶ *(lists installed libraries; inserts `use`/`include` statement)* / Manage Libraries…

**View**:
- Show Toolbar / Show Tab Bar / Show Code Editor / Show Tools Strip / Show Console / Show Debugger / Show Animate
- —
- Top / Bottom / Left / Right / Front / Back / Isometric (change azimuth/elevation only; preserve target and distance) / View All (frame geometry at 1.2× extent)
- —
- Perspective (toggle perspective/orthographic projection)
- —
- Show Axes / Show Edges / Show Scale Markers / Show Crosshairs / Show Status Bar

**Window**: Minimize / Zoom / — / Move Tab to New Window / — / *(open document list)* / Bring All to Front
