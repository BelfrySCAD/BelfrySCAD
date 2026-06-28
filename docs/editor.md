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

## Editor Context Menu

Right-clicking in the editor builds a standard Qt context menu, then appends identifier-aware and debug-aware actions.

**Debug variable inspection** (when debugger is paused and the word under the cursor is a known variable — locals, globals, constants, or `$`-specials):
- **`Variable: x`** / **`Value: <value>`** — two disabled (grayed-out) header items: the variable name and its value formatted by `_fmt()` and truncated to 30 characters with `…` if longer. Appear before the standard cut/copy/paste items, followed by a separator.
- **Print 'x' to Console** — emits `CodeEditor.print_value_to_console(name, value)`, connected to `MainWindow._on_debug_print_value`, which calls `self._console.append_value(name, value, _pretty_assignment(name, value))`. The original Python value is stored for the console right-click viewer menu.
- **View 'x'…** submenu — populated by `build_viewer_menu()` from `data_viewers.py`; only appears when the value type supports a viewer (list, VNF, path, grid).

The available variables come from the innermost debug frame: `{**outer_scope, **local_scope}` (local overrides outer on collision), which covers locals, globals, constants, and `$`-specials. `Qt.WordUnderCursor` excludes `$`, so `contextMenuEvent` manually checks whether the character immediately before the selection is `$` and prepends it — allowing `$fn`, `$t`, etc. to match.

`MainWindow._on_debug_paused` and `_on_debug_error_break` call `_set_debug_locals_on_visible(locals_dict)`, which first clears locals from all editors via `_clear_all_debug_locals()`, then sets them on `_current_tab().editor` — the editor that is actually visible after `_show_debug_line` may have switched to an included file's tab. All resume/step/stop/finish/restart handlers call `_clear_all_debug_locals()` to sweep every open editor clean.

**Go to Definition** (for any identifier, always shown):

Right-click an identifier shows "Go to Definition of 'name'", only for words matching `\$?[A-Za-z_][A-Za-z0-9_]*` (plain identifiers and `$`-prefixed specials).

`CodeEditor.go_to_definition_requested` (emits the word) connects to `MainWindow._go_to_definition(tab, word)` per tab.

`_go_to_definition` requires a cached `root_scope` on the tab (set after every successful `build_scopes()`, both in the render worker and `_start_debug()`); if absent, logs a message asking the user to render first.

Lookup order: `scope.lookup_variable(word)` → `scope.lookup_function(word)` → `scope.lookup_module(word)`, first non-None wins. Built-in modules return `None` from `lookup_module` and are skipped.

The definition node's `.position.origin` gives the source file path, `.position.line` the 1-indexed line. Navigation uses `scroll_to_line(line)` which ensures at least 5 lines of context above and below:
- Same file (or origin `None` / untitled tab): scroll current editor to the line
- Different file: switch to a matching open tab by `file_path`, or open via `_create_and_add_tab()` (view-only, no render)

`_create_and_add_tab(path, text) -> FileTab` creates a `FileTab` (editor + file metadata) and adds it to `_tabs` in the editor dock. Also registers the editor with `DocumentManager` for cross-window sync. If the only existing tab is an empty, unmodified Untitled tab, it is replaced rather than kept alongside. Used by `_open_file`, `_open_recent`, `_go_to_definition`, `_find_or_open_tab`; not by `_new_document` (different setup path for blank tabs).

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

The console is `ConsoleWidget` (`window/console.py`), a `QTextBrowser` subclass. One instance exists per `MainWindow` (window-level singleton `self._console`) — there is no per-tab console. Plain log output goes through `MainWindow.log(text)` or `MainWindow.log_to_tab(tab, text)` (tab argument is ignored) → `self._console.append_output(text)`. Value-bearing output (from "Print to Console" or debugger return values) goes through `MainWindow.log_value_to_tab(tab, name, value)` or directly in `_on_debug_print_value` → `self._console.append_value(name, value, _pretty_assignment(name, value))`.

`QTextBrowser` is used (not `QPlainTextEdit`) because it handles cursor shapes natively: `PointingHandCursor` over anchor links, `IBeamCursor` over selectable text. `setOpenLinks(False)` prevents navigation; `anchorClicked` handles fold toggles.

`append_output(text)` and `append_value(name, value, text)` both route:
- **Single-line text** → `_append_plain(text)` — inserts a plain text paragraph via `QTextCursor`.
- **Multi-line text** → `_append_foldable(lines[0], '\n'.join(lines[1:]))` — inserts the first line as an HTML anchor `<a href="fold:N">▼ summary</a>` and the remaining lines as plain paragraphs. Clicking the anchor collapses or expands the block; `QTextBrowser` shows a hand cursor over it automatically.

`append_value` additionally stores `(name, value)` in `_fold_values: dict[int, tuple[str, object]]` keyed by fold_id, so right-clicking on any line of that block can launch a viewer.

Fold state is tracked in `_fold_headers: dict[int, tuple[int, int, int]]` (fold_id → (header_bn, first_body_bn, last_body_bn)) and `_folded: set[int]` (fold_ids currently collapsed). `clear()` resets all three dicts. Block visibility is toggled with `QTextBlock.setVisible()`; layout recalculation is triggered by updating the arrow character via `QTextCursor.insertText` (done after setting visibility so the `documentChanged` signal sees the correct state). The fold_id is a sequential integer assigned at append time and embedded in the anchor href.

**Console right-click context menu** (`MainWindow._console_context_menu`): `ConsoleWidget.value_at(pos)` checks whether the right-clicked position falls inside a fold header or body block that has a stored value (by scanning `_fold_headers` for block number, then looking up `_fold_values`). If found, a **View 'name'…** submenu is added via `build_viewer_menu()` before the **Clear Console** action.

The console displays:
- Parse errors (file/line/col from AST metadata)
- On each render: bounding box of the resulting mesh and current camera position

## Animation

The **Animate** dock (`AnimatePane` in `window/animate.py`, one per `MainWindow`) implements OpenSCAD's [`$t` animation](https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Animation):

- **Time / FPS / Steps** fields: Time shows the current `$t` (read-only display, but editable — typing a value jumps to the nearest step, clamped to `[0, 1 - 1/steps)`); FPS (1-1000) sets the playback rate; Steps (1-1,000,000) sets the number of frames in one cycle. `$t = step / steps` for `step` in `0..steps-1`. Tab/Shift+Tab move between these three fields, and Enter confirms an edit (`QLineEdit.editingFinished`, which fires on Return as well as focus-out). `AnimatePane` installs an event filter on each field to accept the `ShortcutOverride` event for Tab/Backtab — otherwise the main window's Indent/Undent actions (bound to Tab/Shift+Tab as window-wide shortcuts for the code editor) would consume the key before normal focus-navigation gets it.
- **Big play/pause button** and the **transport row** (First / Previous / Play / Pause / Next / Last) drive playback. Any non-playback transport action pauses playback first.
- **Dump Pictures** checkbox: when checked and Play is pressed, BelfrySCAD prompts (once per tab) for a destination folder via a folder picker, then saves each frame of one full animation cycle as `frameNNNN.png` (via `Viewport.grabFramebuffer()`), pausing automatically after frame `steps - 1` rather than looping.

Each frame change re-renders the current file with `$t` set accordingly — `MainWindow._viewport_params()` includes `"$t": self._animate_pane.current_t()`, merged into the evaluator's dynamic context alongside `$vpt`/`$vpr`/`$vpd` (see `docs/evaluator.md`). During playback the viewport camera is **not** auto-fit to the model's bounding box on each frame (unlike a normal Render), so the camera stays put across frames. Animation re-renders whichever file tab is currently visible in the editor.

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
| Shift+Cmd+1 | Toggle Spin |
| Shift+Cmd+2 | Toggle Perspective |
| Shift+Cmd+3 | Toggle Stereo (Cross-eye) |
| Shift+Cmd+V | View All |
| Cmd++ | Increase editor font size |
| Cmd+- | Decrease editor font size |
| Cmd+[ | Zoom Out |
| Cmd+] | Zoom In |
| Tab | Indent line/selection |
| Shift+Tab | Unindent line/selection |
| F5 | Debug: Continue / Pause |
| Shift+F5 | Debug: Stop |
| Shift+Cmd+F5 | Debug: Restart |
| F6 | Render |
| Shift+F6 | Debug |
| F7 | Animate |
| F10 | Debug: Step Over |
| F11 | Debug: Step Into |
| Shift+F11 | Debug: Step Out |
| Escape | Cancel in-progress render |

## Application Preferences

Preferences live under the `editor/` key group in `QSettings("BelfrySCAD", "BelfrySCAD")`, accessed via `load_preference(key, type)` / `save_preferences(dict)` in `preferences.py`.

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
- **macOS file association** — `.scad` files opened from Finder (double-click or Open With) send a `QFileOpenEvent` to `BelfrySCADApp`, which forwards to `MainWindow.open_file_by_path()`; the Info.plist declares `CFBundleDocumentTypes` for `.scad` via briefcase config in `pyproject.toml`
- **Command-line arguments** — `BelfrySCAD foo.scad bar.scad` opens each file at launch

All paths converge on `open_file_by_path(path)`, which resolves the path and checks for an already-open tab before creating a new one.

## Quit Behavior

When the user quits the app and there are modified editors open, a Save/Discard/Cancel dialog is shown for each unsaved tab. Cancel aborts the quit.

## GUI Layout

```
┌────────────────────────────────────────────────────────────────┐
│  [New][Open][Export] | [Undo][Redo] | [Render][Debug][Animate] | [T][R][S] │  ← toolbar
├───────────────────────────────────────┬────────────────────────┤
│ [file1 ×][file2 ×]                    │                        │
│                                       │                [cube]  │
│            Code Editor                │    3D Viewport         │
│            (central widget)           │    (right dock)        │
├───────────────────────────────────────┴────────────────────────┤
│  Console                              │ Debugger / Animate     │
├───────────────────────────────────────┴────────────────────────┤
│  $vpt = [0.00, …]  $vpr = [55.00, …]  $vpd = 50.00    0 FPS  │  ← status bar
└────────────────────────────────────────────────────────────────┘
```

- **Toolbar**: across the top — New, Open, Export | Undo, Redo | Render, Debug, Animate | T, R, S (gizmo tool buttons)
- **Code editor** (central widget): `QTabWidget` with one tab per open file; tabs at top; always visible
- **Viewport dock** (right): single 3D viewport shared across all editor tabs; always shows the last render result; contains a cube gizmo for view angle control
- **Console** (bottom-left dock): single running log per window; not per-tab; side-by-side with Debugger
- **Debugger** (bottom-right dock): visible by default; side-by-side with Console
- **Animate** (bottom dock): hidden by default; open via Animate toolbar button (F7) or View ▸ Show Animate
- **Status bar**: bottom strip; camera position + FPS counter

The viewport, console, debugger, and animate pane are `QDockWidget` instances — dockable to any side or floatable, with position/visibility persisted via `QSettings("BelfrySCAD", "BelfrySCAD")` (`saveState()`/`restoreState()`). Object names: "ViewportDock", "ConsoleDock", "DebuggerDock", "AnimateDock". `setCorner(BottomLeft/RightCorner, BottomDockWidgetArea)` makes the bottom row span the full window width. On first launch (no saved `windowState`), `showEvent` fires a deferred `_set_default_layout` call: viewport dock = 50% window width, bottom docks = 25% window height, console and debugger split evenly. The Debugger pane is a single shared widget on `MainWindow` (not per-tab).

Scale markers are tick marks along the viewport axes showing distance units (Show Scale Markers), each labeled with its distance value. Labels are rendered in 3D as camera-facing textured billboards: each tick's number is rasterized to an RGBA texture (cached by string) and drawn on a small transparent quad positioned just past the tick, so labels respect depth (occluded by geometry in front of them) and scale with zoom like the tick marks themselves. An axis whose line is nearly end-on to the camera has its tick labels suppressed (its ticks would otherwise overlap near the origin). Show Edges renders the full triangulation wireframe via `GL_POLYGON_OFFSET_FILL` on the solid pass (pushes fill surfaces away from camera), then draws edges at true depth in a second pass — avoids z-fighting on coplanar faces while keeping hidden edges correctly occluded. Show Crosshairs draws four white diagonal lines (the four space diagonals of a unit cube) crossing at the camera target, each extending `camera.distance * 2.5 / 12`. Perspective/orthographic toggle uses `camera.orthographic`, persisted in QSettings.

**Restoring settings to checkable actions**: wrap `setChecked()` in `blockSignals(True/False)`, then call the handler explicitly — avoids double-invocation (signal + explicit call) and ensures the handler fires even if the stored value matches the default. Pattern used in `_restore_settings`:
```python
self._act_perspective.blockSignals(True)
self._act_perspective.setChecked(perspective)
self._act_perspective.blockSignals(False)
self._toggle_perspective(perspective)
```

**New tabs only need editor settings**: `FileTab` contains only the `CodeEditor`. There is no per-tab viewport or camera. `_new_document()` and `_create_and_add_tab()` call `_apply_preferences_to_tab(tab, font, indent, ...)` to inherit editor font/indent/guide settings and `_apply_word_wrap_to_tab(tab)` for word wrap. The `hasattr(self, '_act_word_wrap')` guard in `_new_document()` covers the case where it's called during `__init__` before `_setup_menus()` finishes.

**`FileTab` vs `DocumentTab`**: the old `DocumentTab` owned a `Viewport`, `ConsoleWidget`, `AnimatePane`, and tools strip. These are now window-level singletons on `MainWindow`. `FileTab` keeps only: `editor`, `file_path`, `is_modified`, `root_scope`, `_last_text`, `_last_cursor`, `_suppress_text_undo`.

**Cross-window sync**: `DocumentManager` (singleton in `window/document_manager.py`) tracks all open editors keyed by resolved file path. `_create_and_add_tab` calls `register(path, editor)`; `_close_tab` / `_write_file` call `unregister`. `_on_editor_changed` calls `broadcast_change(path, text, source_editor)` after each text change — other editors for the same file have `setPlainText(text)` called with signals blocked to avoid re-entrancy. `open_file_by_path` queries `get_current_text(path)` to seed a newly opened editor with in-memory (potentially unsaved) text from another window.

## Data Viewers

Implemented in `src/belfryscad/window/data_viewers.py`. Three viewer dialogs for inspecting evaluated data, opened from the debugger's variable context menu via `build_viewer_menu()`.

### _SimpleViewport (QOpenGLWidget)

Base class for viewer 3D viewports. Orbit camera (azimuth/elevation/distance/target), own ModernGL shader programs (line, mesh with backface_color, edge, label). Axis rendering with ticks and labels ported from the main `SceneRenderer` (`_nice_spacings`, `_fmt_tick`). `schedule_load(fn)` defers geometry uploads until after `initializeGL()`. `set_view_preset()` for named views (top/bottom/left/right/front/back/isometric). Supports perspective/orthographic toggle, axes toggle, edge toggle. Mouse-centered zoom (wheel shifts target toward cursor). No custom `QSurfaceFormat` — macOS multisampling causes compositing artifacts.

Keyboard shortcuts (Cmd+0–9 views, Cmd+1–3 toggles, Shift+Cmd+1–3 toggles) are set directly on the `QAction` instances in the View menu, so they appear in the menu and work application-wide via the native macOS menu bar.

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

### GridViewer (QDialog)

`QSplitter`: `_GridViewport` on left, Row dropdown + vertex table on right. Dismiss button (20px right padding) below. Detects lists of lists of points where all rows have equal length.

- **Row dropdown**: `QComboBox` selects which grid row to display in the vertex table. Changing the row highlights that row's vertices in the viewport.
- **"Row Points (N)" label**: shows the point count for the selected row.
- **Vertex table**: `QTableWidget`, 0-indexed rows, X/Y/Z columns. Extended multi-select. Selecting rows highlights individual vertices in the viewport.
- **Viewport**: `_GridViewport(_SimpleViewport)`. Always draws row lines (blue) and column lines (orange) via `upload_lines`. In faces mode, also draws triangulated quad mesh faces (yellow both sides) via `upload_mesh`. `show_edges = True` enables `GL_POLYGON_OFFSET_FILL` when a mesh is present, pushing face triangles slightly back so the skeleton lines always render in front. Vertex markers are depth-tested (occluded by mesh). Green octahedrons for unselected, red↔white blink (250ms) for selected. Hovering shows `[row,col]: (x, y, z)` tooltip. Clicking a vertex switches the dropdown to that row and selects the vertex in the table.
- **Faces**: checkbox (checked by default). When on, renders triangulated quad mesh faces behind the skeleton lines. When off, shows skeleton lines only.
- **Col Wrap**: checkbox connects last column back to first, closing the grid horizontally.
- **Row Wrap**: checkbox connects last row back to first, closing the grid vertically. Both wraps together form a torus.
- 2D grids start in top-down orthographic; 3D grids start in perspective orbit.

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
