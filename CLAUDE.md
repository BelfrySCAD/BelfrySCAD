# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BelfrySCAD is a hybrid procedural CAD application combining OpenSCAD-style script-based modeling with live WYSIWYG 3D interaction. Its defining feature is **bidirectional synchronization** between source code and 3D geometry — editing code or dragging geometry keeps both views in sync.

**Status**: In active development. Core pipeline, rendering, editor, and several WYSIWYG features are implemented. Full design in `PRD.md`.

## Technology Stack

- **UI Framework**: PySide6 (Qt)
- **Code Editor**: `QPlainTextEdit` + `QSyntaxHighlighter` (PySide6 built-ins; text layer only — not semantically aware)
- **Parser**: openscad_lalr_parser ≥1.1.0 (LALR-based, generates AST with file/line/col/span metadata; parses full OpenSCAD syntax but has no knowledge of built-in functions/modules — the evaluator implements all built-ins). Fetched from PyPI.
- **Evaluator**: openscad_evaluator ≥1.0.0 (walks the parser's AST and produces Manifold geometry — the two-pass resolve/generate pipeline, built-ins, `ManifoldCache`, profiling; GUI-agnostic, callback-injection API). Fetched from PyPI; see its own `docs/evaluator.md` for the full architecture reference.
- **CSG Kernel**: Manifold (union, difference, intersection, boolean ops)
- **Renderer**: ModernGL (GPU mesh rendering, camera controls)
- **Language**: Python

## Core Architecture

The pipeline flows strictly one direction during normal operation:

```
Source Code → Code Editor → openscad_lalr_parser (AST) → Evaluator → Manifold (CSG/mesh) → ModernGL → PySide6 UI
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

Requires every AST node to carry both its **source span** (file/line/col) and its **geometry ID(s)** from Manifold output. This mapping is the hardest design problem in the project. See `docs/wysiwyg.md` for the full interaction design and openscad_evaluator's own `docs/evaluator.md` for the AST ↔ geometry ID mapping pattern.

## Key Design Requirements

- **Code ↔ Geometry mapping**: every geometry-producing AST node owns an `originalID`; the `originalID → AST node` table rebuilds on each render trigger.
- **Stability under invalid code**: UI must never crash or go blank.
- **Deterministic regeneration**: AST → geometry must be reproducible with no hidden rendering state. Every render trigger walks the whole tree, but unchanged subtrees skip actual Manifold work via a content-hash cache (`ManifoldCache`, see openscad_evaluator's `docs/evaluator.md`) — a fresh AST/CSG tree is still built every render (no incremental *parsing*), but a node whose resolved content matches a previous render/debug pause reuses that prior result instead of recomputing it.
- **Performance**: <200ms model regeneration for small/medium models; 60 FPS viewport.

## File Format & Export

- **File format**: `.scad` (OpenSCAD-compatible plain text)
- **Language**: Full OpenSCAD language (variables, functions, modules, loops, conditionals, all built-in primitives and transforms)
- **Export**: STL, OBJ, 3MF; STEP under investigation (Manifold produces triangle meshes; STEP is B-rep, so any export would be a faceted solid of limited downstream value)
- **Export workflow**: if no current render exists, Export triggers a render first

## Render Triggers

No live preview. Full Manifold CSG processing runs when:

- The user selects **Render** (toolbar or Design menu)
- A **gizmo drag commits** (mouse-up)
- An **"Edit as..." literal edit is saved** (Save button in the editable Path/Grid/Matrix/Affine viewers, opened from the code editor's right-click menu)
- A **file is opened** (`open_file_by_path` triggers `_render` after the tab is created)
- A **file is saved** (`_write_file` triggers `_render` after writing)
- The user stops editing **Customizer** fields for 5 seconds (`MainWindow._customizer_render_timer`, a debounced single-shot `QTimer` restarted on every edit — see `docs/editor.md`'s CustomizerPane section)

**"Render with Profiling"** (Design menu) is a separate, explicitly opt-in diagnostic trigger — not part of this automatic/WYSIWYG set — that turns on per-call-site timing instrumentation for that one render. See openscad_evaluator's `docs/evaluator.md`'s "Profiling" section.

The viewport always shows the last render's result; it stays static while the user edits code.

## V1 Scope Boundaries

**In scope**: Script editing, real-time 3D rendering, basic WYSIWYG drag interaction, CSG operations, graceful invalid-code handling.

**Explicitly out of scope for v1**: Constraint solver, collaborative editing, cloud modeling, incremental/tolerant parsing, node-based visual programming, plugin system.

## Versioning

Every PR bumps the version (`version` in both `[project]` and `[tool.briefcase]` in `pyproject.toml`, kept identical — then run `uv lock` to sync `uv.lock`'s pinned self-version). Patch bump at minimum; use judgment for minor/major on larger changes. Do this as part of preparing the PR, alongside the commit.

## Further Documentation

Detailed implementation notes live in `docs/`. AST Evaluator internals (scope processing, assignment order, built-ins reference, 2D/3D geometry handling, error format, `$variables` scoping, `include`/`use`, implementation quirks, and the Manifold provenance / AST ↔ geometry ID mapping API) now live in the separate `openscad_evaluator` package's own `docs/evaluator.md`, not here.

- **`docs/wysiwyg.md`** — Viewport camera controls, selection model, transform gizmos, value overlay, and source rewrite rules for drag-to-edit.
- **`docs/debugger.md`** — `DebugSession` signals, call stack display, per-frame variable inspection, expression-level stepping, and `DebuggerPane` states.
- **`docs/rendering.md`** — Threaded rendering (`_RenderWorker`/`_RenderCallback`), cancellation, and progress indicator.
- **`docs/editor.md`** — Code editor features (Find/Replace, Indent Guides, Column Guide, Code Folding, Go to Definition), Undo/Redo, console output, keyboard shortcuts, preferences, GUI layout, menu structure, and data viewers (ListViewer, VNFViewer, PathViewer, GridViewer, ProfileViewer).
