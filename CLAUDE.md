# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BelfrySCAD is a hybrid procedural CAD application combining OpenSCAD-style script-based modeling with live WYSIWYG 3D interaction. Its defining feature is **bidirectional synchronization** between source code and 3D geometry — editing code or dragging geometry keeps both views in sync.

**Status**: In active development. Core pipeline, rendering, editor, and several WYSIWYG features are implemented. Full design in `PRD.md`.

## Technology Stack

- **UI Framework**: PySide6 (Qt)
- **Code Editor**: `QPlainTextEdit` + `QSyntaxHighlighter` (PySide6 built-ins; text layer only — not semantically aware)
- **Parser**: openscad_cpp_parser (C++, Bison `lalr1.cc`; generates an AST with file/line/col/span metadata; parses full OpenSCAD syntax but has no knowledge of built-in functions/modules — the evaluator implements all built-ins). Not a dependency of this project directly: it is vendored at `external/openscad_cpp_parser` inside openscad_cpp_evaluator and built with it.
- **Evaluator**: openscad_cpp_evaluator ≥0.30.0 (C++ with nanobind bindings; walks the parser's AST and produces Manifold geometry — the two-pass resolve/generate pipeline, built-ins, `ManifoldCache`, profiling; GUI-agnostic, callback-injection API). The only OpenSCAD-side dependency in `pyproject.toml`, fetched from PyPI as a wheel; see its own `CLAUDE.md` for the full architecture reference.
- **CSG Kernel**: Manifold (union, difference, intersection, boolean ops)
- **Renderer**: ModernGL (GPU mesh rendering, camera controls)
- **Language**: Python

## Core Architecture

The pipeline flows strictly one direction during normal operation:

```
Source Code → Code Editor → openscad_cpp_parser (AST) → Evaluator → Manifold (CSG/mesh) → ModernGL → PySide6 UI
```

**The AST is the single source of truth** — not the rendered geometry, not the editor text.

### Critical Constraint: Strict Parser

The parser produces **no partial AST** — it either succeeds fully or fails entirely. Handle the no-AST state gracefully:
- Cache the last valid AST
- Display last valid geometry while code is invalid
- Never block the UI or break the viewport

### Bidirectional Loop (future-critical, v1 groundwork required)

Dragging geometry in the viewport:
```
Drag event → ray cast → pick geometry ID → map ID to AST node (via span) → modify AST parameter → regenerate code + model
```

Requires every AST node to carry both its **source span** (file/line/col) and its **geometry ID(s)** from Manifold output. This mapping is the hardest design problem in the project. See `docs/wysiwyg.md` for the full interaction design and openscad_cpp_evaluator's own `CLAUDE.md` for the AST ↔ geometry ID mapping pattern.

## Key Design Requirements

- **Code ↔ Geometry mapping**: every geometry-producing AST node owns an `originalID`; the `originalID → AST node` table rebuilds on each render trigger.
- **Stability under invalid code**: UI must never crash or go blank.
- **Deterministic regeneration**: AST → geometry must be reproducible with no hidden rendering state. Every render trigger walks the whole tree, but unchanged subtrees skip actual Manifold work via a content-hash cache (`ManifoldCache`, see openscad_cpp_evaluator's `CLAUDE.md`) — a fresh AST/CSG tree is still built every render (no incremental *parsing*), but a node whose resolved content matches a previous render/debug pause reuses that prior result instead of recomputing it.
- **Performance**: <200ms model regeneration for small/medium models; 60 FPS viewport.

## File Format & Export

- **File format**: `.scad` (OpenSCAD-compatible plain text)
- **Language**: Full OpenSCAD language (variables, functions, modules, loops, conditionals, all built-in primitives and transforms)
- **Language extension — `render()` in expression position**: `obj = render() { cube(1); };`
  builds its children's geometry, measures it, and returns an `object()` with `vertices`,
  `faces`, `volume`, `area`, `genus`, `boundingbox` and `dim` — then **discards the geometry**
  (nothing is drawn). This is the only way a script can inspect its own geometry.
  `polyhedron()` and `polygon()` accept the object directly, so the mesh round-trips in one
  call. Two consequences worth knowing: **`render` is a reserved keyword** (it can no longer
  be a variable/module/function/argument/member name — LALR(1) leaves no alternative), and
  **`obj = render() cube(1);` does not parse** — a bare call's `child_statement` swallows the
  `;`, so the braced form is required. Not part of upstream OpenSCAD. Full reference in
  openscad_cpp_evaluator's `CLAUDE.md`.
- **Export**: 3MF (default), STL, OBJ, OFF, PLY, VRML, X3D — all written by openscad_cpp_evaluator's `export.cpp`, which owns the colour pipeline and mesh repair; `exporters.py` is just the interface. STEP under investigation (Manifold produces triangle meshes; STEP is B-rep, so any export would be a faceted solid of limited downstream value)
- **Export object split**: top level is an implicit union, so every format writes the union, never the raw body list. The evaluator's `splitBodiesForExport` cuts it into objects that never share volume — one per colour (later `color()` wins an overlap), then one per connected component — and carries per-triangle colour where a CSG merge produced it. The GUI calls `exporters.export_model(path, evaluator.geometry)` and logs the warnings it returns. See `docs/rendering.md`'s Export section.
- **Export workflow**: if no current render exists, Export triggers a render first

## Render Triggers

No live preview. Full Manifold CSG processing runs when:

- The user selects **Render** (toolbar or Design menu)
- A **gizmo drag commits** (mouse-up)
- An **"Edit as..." literal edit is saved** (Save button in the editable Path/Grid/Matrix/Affine viewers, opened from the code editor's right-click menu)
- A **file is opened** (`open_file_by_path` triggers `_render` after the tab is created)
- A **file is saved** (`_write_file` triggers `_render` after writing)
- The user stops editing **Customizer** fields for 2 seconds (`MainWindow._customizer_render_timer`, a debounced single-shot `QTimer` restarted on every edit — see `docs/editor.md`'s CustomizerPane section)
- An **animation frame advances** (`MainWindow._on_animate_frame` renders per tick; a tick is skipped while a render is still in flight, since overlapping renders invoke the parser concurrently and can segfault)
- A **watched file changes on disk**, with **Design ▸ Automatic Reload and Render** on (`_on_watched_file_changed`; skipped for a tab with unsaved edits, which are never overwritten)
- The user **accepts an AI proposal** in the chat pane (`_on_ai_proposal_accepted` goes through `replace_span` + `source_edited_externally`, the same path "Edit as..." uses)
- The **AI calls its `render` tool** (`AIToolContext.request_render`, wired to `_render_threadsafe`) — for a script it has not itself changed

**"Render with Profiling"** (Design menu) is a separate, explicitly opt-in diagnostic trigger — not part of this automatic/WYSIWYG set — that turns on per-call-site timing instrumentation for that one render. See openscad_cpp_evaluator's `CLAUDE.md` for the profiling instrumentation.

The viewport always shows the last render's result; it stays static while the user edits code.

## V1 Scope Boundaries

**In scope**: Script editing, real-time 3D rendering, basic WYSIWYG drag interaction, CSG operations, graceful invalid-code handling.

**Explicitly out of scope for v1**: Constraint solver, collaborative editing, cloud modeling, incremental/tolerant parsing, node-based visual programming, plugin system.

## Versioning

Every PR bumps the version (`version` in both `[project]` and `[tool.briefcase]` in `pyproject.toml`, kept identical — then run `uv lock` to sync `uv.lock`'s pinned self-version). Patch bump at minimum; use judgment for minor/major on larger changes. Do this as part of preparing the PR, alongside the commit.

### The macOS bundle's Info.plist goes stale on every bump

`briefcase update` and `briefcase build` never rewrite `build/belfryscad/macos/app/BelfrySCAD.app/Contents/Info.plist` — only `briefcase create` generates it, from the `pyproject.toml` values as they stood at scaffold time. So after any version bump the bundle keeps reporting the *old* `CFBundleShortVersionString`, and the same applies to anything else the plist bakes in (`LSMinimumSystemVersion` from `[tool.briefcase.app.belfryscad.macOS] min_os_version`, the bundle identifier, the document-type declarations).

Nothing warns about this. Both values had drifted a long way before anyone looked: the plist still said `0.1.0` and `12.0` while `pyproject.toml` said `0.68.1` and `13.3` — the app itself reported 0.68.1 correctly the whole time, since that comes from the installed package, not the plist. The stale `LSMinimumSystemVersion` was the real problem: it advertised macOS 12 support for a bundle whose `openscad_cpp_evaluator` wheel needs 13.3.

To refresh it, regenerate the scaffold rather than hand-editing the plist (a hand edit is silently discarded the next time anyone runs `create`):

```
mv build/belfryscad/macos/app build/belfryscad/macos/app.bak   # ~950MB, keep until verified
uv run briefcase create --no-input
uv run briefcase build
/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
    build/belfryscad/macos/app/BelfrySCAD.app/Contents/Info.plist
rm -rf build/belfryscad/macos/app.bak
```

`create` re-downloads every wheel (PySide6 alone is ~440MB), so this is a few minutes — worth doing before cutting any real release or notarized build, not on every routine local rebuild. `CFBundleVersion` stays at `1`; that is briefcase's build-number default and is unrelated to the version string.

## Further Documentation

Detailed implementation notes live in `docs/`. AST Evaluator internals (scope processing, assignment order, built-ins reference, 2D/3D geometry handling, error format, `$variables` scoping, `include`/`use`, implementation quirks, and the Manifold provenance / AST ↔ geometry ID mapping API) now live in the separate `openscad_cpp_evaluator` package's own `CLAUDE.md`, not here.

- **`docs/wysiwyg.md`** — Viewport camera controls, selection model, transform gizmos, value overlay, and source rewrite rules for drag-to-edit.
- **`docs/debugger.md`** — `DebugSession` signals, call stack display, per-frame variable inspection, expression-level stepping, and `DebuggerPane` states.
- **`docs/rendering.md`** — Threaded rendering (`_RenderWorker`/`_RenderCallback`), cancellation, and progress indicator.
- **`docs/editor.md`** — Code editor features (Find/Replace, Indent Guides, Column Guide, Code Folding, Go to Definition), Undo/Redo, console output, keyboard shortcuts, preferences, GUI layout, menu structure, and data viewers (ListViewer, VNFViewer, PathViewer, GridViewer, ProfileViewer).
