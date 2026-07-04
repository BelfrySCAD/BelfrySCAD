# Debugger Reference

The debugger runs the evaluator in a daemon worker thread (`DebugSession`) and surfaces a single shared `DebuggerPane` (owned by `MainWindow`, not per-tab) with call-stack and variables panels. `_debug_session` and `_debug_tab` on `MainWindow` track the active session and the `FileTab` that started it. The viewport, console, and animate pane are all window-level singletons — there is no per-tab routing. See `docs/evaluator.md` for the evaluator internals referenced below.

## DebugSession (`debugger.py`)

Signals (emitted from the worker thread; Qt queues them to main):

| Signal | Args | When |
|---|---|---|
| `paused` | `origin, line, all_frame_locals, call_stack, partial_bodies, partial_error` | Hit a breakpoint or step |
| `error_break` | `origin, line, msg, all_frame_locals, call_stack, partial_bodies, partial_error` | Any runtime error |
| `finished` | `bodies, id_to_node` | Evaluation completed |
| `errored` | `str` | Unhandled exception after error_break resume |
| `logged` | `str` | Echo/print output from the evaluator (thread-safe via signal) |
| `logged_value` | `str, object` | Function return value (name, value) — viewer-aware alternative to `logged` for step-over/step-out return values |

`partial_bodies`/`partial_error` (Phase 3 — see "Live partial-tree rendering" below) are the result of `_generate_partial_render(self._ev)`, called right before every emit of `paused`/`error_break`.

`all_frame_locals` is a list of frame dicts, **innermost first**, with an extra `<toplevel>` entry appended when inside a call. `all_frame_locals[0]` matches row 0 (innermost) of the call-stack list. Each entry:

| Key | Contents |
|---|---|
| `"local_scope"` | Eagerly-assigned vars in the frame's `ctx.dyn`: `__let_*` (params, `for`/`let`, assignments so far) and `$*` specials |
| `"outer_scope"` | Global vars from `_root_ctx.dyn` (innermost frame only, when inside a call; parent frames get `{}`) |
| `"dyn_names"` | `set` of names from `dyn` — the only vars editable via the pane |

**Debug hook** — `_make_hook()` returns a closure passed to `Evaluator(debug_hook=...)`. Signature: `hook(line, depth, *, forced, expr_level, expr_depth, origin, get_frames) → (cmd, mods)`. `origin` is the source file path from the AST node's `position.origin` — `None` for the main file, a path string for included files. `get_frames` is a lazy callback that builds `(locals_dict, all_frame_locals), call_stack` only when called, avoiding the cost on non-pausing hook invocations. On resume, the hook records the current `line`, `depth`, and `resolved_origin` as `_step_line`, `_step_depth`, `_step_origin`. Step logic: **over** pauses when `depth ≤ _step_depth` and `line ≠ _step_line` and same origin; **into** pauses when `line ≠ _step_line` or origin changed; **out** pauses when `depth < _step_depth`. All three skip `expr_level` checkpoints. Break-on-first pauses at the first non-expression statement in the toplevel file. Breakpoints are collected from all open tabs as a `{resolved_path: set(lines)}` dict (`MainWindow._collect_breakpoints()`), and the hook resolves `origin` before lookup. This dict isn't fixed for the session's lifetime: `CodeEditor.breakpoints_changed` (emitted from `toggle_breakpoint()`) is connected, for every tab, to `MainWindow._on_breakpoints_changed()`, which — while a session is running or paused (`DebugSession.is_running()`) — recomputes the full dict and pushes it via `DebugSession.set_breakpoints()`. Without this, a breakpoint added mid-session (e.g. while paused, to catch the *next* iteration of a loop) would silently have no effect until Restart, since the hook only ever read the dict captured at `start()`. When pausing in an included file, `MainWindow._show_debug_line()` opens the file in a new tab (or switches to it if already open) and highlights the execution line via `set_execution_line()`, which uses `scroll_to_line()` to ensure at least 5 lines of context above and below; `_clear_all_execution_lines()` clears stale highlights across all tabs first. The hook builds a **display** call stack with a `("toplevel", "<toplevel>", None)` entry appended before emitting `paused`, blocking on a `threading.Event`.

**Pause during execution** — `DebugSession.pause()` sets `_pause_requested`. The hook checks/consumes this flag at the top of every call, triggering an immediate pause regardless of breakpoints or step state — useful for interrupting a long-running evaluation.

**Error break** — `Evaluator(error_break_fn=self._error_break)` intercepts every `error()` call before raising `EvalError`. `_error_break` emits `error_break` and blocks until the user resumes; afterward `EvalError` propagates normally (caught by `_run`, triggers `errored`).

## Live partial-tree rendering (Phase 3)

`_run()` assigns `self._ev = ev` right after constructing the `Evaluator`, before calling `ev.evaluate(...)` — the hook closure (already a bound method's inner function) can then reach `self._ev._tree_stack`/`self._ev.generate_tree()`. See `docs/evaluator.md` § "CSG tree" for the two-pass resolve/generate design this depends on: `generate_tree()` can be called on any (possibly partial) tree at any point, since resolve never depends on a node's generated bodies.

`_generate_partial_render(ev)` (module-level function in `debugger.py`) does a best-effort `generate_tree()` over whatever's been resolved so far, converts the result via `to_renderable_bodies()`, and returns `(bodies, None)` on success or `(None, str(e))` if any `generate_fn` raises. Called from both `hook()` and `_error_break()`, right before every `paused`/`error_break` emit — so **every** pause (breakpoint hit, Step Into/Over/Out, the Pause button, and error breaks) triggers a live regeneration, not just step commands that cross a module-call boundary. `generate_tree()` always recomputes from scratch (no caching, matching the project's "full Manifold rebuild on every render trigger" convention), so this cost grows with the tree across a debug session — single-stepping through a large script gets progressively slower as more of the tree accumulates. Known, accepted trade-off for the chosen trigger scope, not a bug.

**Generates from every level of `ev._tree_stack` flattened together, not just `ev.csg_tree`.** A `CSGNode` only gets appended to its parent's accumulator once that parent's own `resolve_fn` returns — so for a script whose whole geometry is one deeply-nested top-level statement (e.g. `difference(){union(){cube();sphere();}cylinder();}`), `ev.csg_tree` (the top-level list) stays completely empty for the *entire* time spent stepping through `cube()`/`sphere()`/`cylinder()`, since `difference()`'s own `CSGNode` isn't appended anywhere until every child (transitively) has finished resolving. Using only `ev.csg_tree` here would mean the live preview never shows anything until the whole script finishes — exactly backwards from the point of this feature. Flattening every level of `ev._tree_stack` (`[node for level in ev._tree_stack for node in level]`) picks up already-resolved leaves (e.g. `cube()`'s `CSGNode` sitting in `union()`'s still-in-progress accumulator) regardless of how many enclosing statements haven't finished yet.

`MainWindow._on_debug_paused`/`_on_debug_error_break` receive the two new trailing args: if `partial_bodies is not None`, `self._viewport.load_geometry(partial_bodies)` runs before `set_paused`/`set_error_break`, so the viewport shows a growing partial model at each pause instead of staying blank for the whole session (previously: `_start_debug()` cleared the viewport and nothing repainted it until `finished`). `partial_error`, when set, is forwarded to `DebuggerPane.set_paused`/`set_error_break`'s new `partial_error` keyword, which shows a small warning label (`⚠ live view stale: {msg}`) below the status label — the viewport keeps showing the last successfully rendered geometry, but the warning flags that it's now potentially stale. The warning clears on the next successful partial render, or when `set_running()`/`set_idle()` runs (a fresh run or leaving debug mode makes a warning about the *previous* pause moot).

A partial-render failure is not expected to come from mere incompleteness — every `generate_fn`'s empty/partial-child handling (Phase 2) already tolerates a tree with fewer children than its final form will have (e.g. `union`/`difference`/`intersection` are no-ops that return their one child unaltered with only one operand resolved so far). A real failure here is more likely a genuine script problem (e.g. malformed `polygon()` points) surfacing earlier than usual, via the live preview, rather than only at the end of evaluation.

## Call stack display

Displayed as a top-down call chain: `<toplevel>` at the top, then outermost callee, down to the innermost (currently executing) frame at the bottom. `_call_stack` in the evaluator is outermost-first; the display stack is `[("toplevel", ...)] + list(call_stack)` (no reversal), built in both `_make_hook()` and `_error_break()`. `_all_frame_locals` is reordered to match: `list(reversed(all_frame_locals))` = `[toplevel, outermost, ..., innermost]`. The stack is a 3-column `QTableWidget` (`self._stack_list`, Name/File/Line, Line right-aligned) — each non-toplevel row shows `name()` / the declaration file (`decl_pos`) / its line. The stack list initially selects the innermost frame (`_innermost_row`, the last row).

`<toplevel>`'s File/Line columns show where it's "at": the `call_pos` of the first real call in the stack if one exists, otherwise the current pause line — same position `_stack_positions` already computed for navigation (see below). A blank `origin` (the main file's top-level nodes have `position.origin is None`) falls back to `main_file`/`os.path.basename(main_file)` — the file path of the tab that started the session, passed into `set_paused`/`set_error_break` as `main_file` by `MainWindow._on_debug_paused`/`_on_debug_error_break` (`self._debug_tab.file_path or ""`) — so both `<toplevel>` and any frame declared in the main (saved) file show its name instead of a blank File column.

**Frame navigation** — clicking a call-stack entry navigates to where that frame calls the next one down. `_populate_stack` stores `(file_path, line)` per row in `_stack_positions`: each frame's position is the `call_pos` of its callee (the next entry in the stack), except the innermost frame (last row) which stores the current pause point. Clicking emits `frame_selected(file_path, line)`, handled by `MainWindow._on_debug_frame_selected` which opens/switches to the target file's tab and highlights the line. Display labels use `decl_pos` (definition site) for the Name/File/Line columns.

When inside a call, a `<toplevel>` frame (`local_scope` = global scope vars) is at row 0. Clicking `<toplevel>` → Locals shows the file's global declarations. Variable editing is only enabled in the innermost frame's Locals view.

## Per-frame variable inspection

The evaluator maintains `_frame_ctxs` (an `EvalContext` list parallel to `_call_stack`), pushed/popped in `_eval_user_module`/`_eval_user_function`. At each `_check_debug`, `local_scope` reads directly from `ctx.dyn` (all `__let_*`/`$*` entries) — no scope walk needed since assignments are eager. When inside a call, `outer_scope` comes from `_root_ctx.dyn` (Globals view). A `<toplevel>` frame (`local_scope = outer_scope`) is appended when `_call_stack` is non-empty.

**Call-site debug stops**: `_eval_function_call` fires `_check_debug(node, ctx)` at the `PrimaryCall` node (the call site in the caller's file) before entering `_eval_user_function` or `_eval_function_literal`. This is a statement-level checkpoint, so Step Over pauses at user-defined function call sites within expressions (e.g., `repeat()` inside a list literal). Built-in function calls do not get call-site checkpoints.

**Step Into for functions**: function bodies are expressions, so `_eval_statement`'s `_check_debug` never fires for them. `_eval_user_function` explicitly calls `self._check_debug(decl.expr, child_ctx)` after pushing the call frame, before `_eval_expr(decl.expr, child_ctx)` — giving Step Into a pause point at the start of every function body.

**Statement-level debug stops in expressions**: these fire without `expr_level`, so step-over/step-out pause on them:
- **`EchoOp`** — before the expression-form `echo(…) body` executes (modular `echo(…);` is already covered by `_eval_statement`)
- **`AssertOp`** — before the expression-form `assert(…) body` executes (modular `assert(…);` is already covered by `_eval_statement`)
- **`TernaryOp`** — before condition evaluation (the chosen-branch pause is `expr_level=True`)
- **`LetOp`** — before each assignment; `ModularLet` skips the `let(` node and steps through assignments individually
- **`ListCompLet`** — before each assignment, in both `_eval_list_comp` and `_eval_list_comp_body`
- **`ListCompFor`** — at each variable binding assignment as the loop iterates (statement-level stop per variable per value, in nested order so the rightmost variable cycles fastest); then at the body expression (`expr_level=True`) on each iteration
- **`ListCompCFor`** — four distinct stop points per C-style `for (inits; cond; incrs) body`: (1) statement-level stop at each init assignment before the loop starts; (2) `expr_level=True` stop at the condition expression each time it is tested, including the final false-check that exits the loop; (3) statement-level stop at the `for` node at each body entry; (4) statement-level stop at each incr assignment after the body, per iteration
- **`ListCompIf` / `ListCompIfElse`** — at the `if` node before condition evaluation; the chosen branch is `expr_level=True` (in both `_eval_list_comp` and `_eval_list_comp_body`)

**Expression-level step points**: `_check_debug` accepts `expr_level=True` for sub-expression pauses. All step commands (`into`, `over`, `out`) skip these checkpoints. Nodes calling `_check_debug(…, expr_level=True)`:
- **`TernaryOp`** — at the chosen branch after condition resolution
- **`ModularIf` / `ModularIfElse`** — `_eval_statement` already pauses at the `if` node; a second `expr_level=True` pause fires at the first statement of the chosen branch (falls back to `node` if the branch is empty)
- **`ModularFor`** — at each variable binding assignment as the loop iterates (statement-level stop per variable per value, in nested order so the rightmost variable cycles fastest); then at the first body statement (`expr_level=True`) on each iteration (body statements also get their own statement-level stops from `_eval_statement`)
- **`ModularIntersectionFor`** — at the first body statement of each iteration, after loop variables bind into `loop_ctx` (body statements already get their own statement-level stops from `_eval_statement`)
- **`ListCompEach`** — before the body expression, in both `_eval_list_comp` and `_eval_list_comp_body`
- **List element expressions** — before each element-producing expression: the `else` branch in `_eval_list_comp` and the fallthrough in `_eval_list_comp_body`

The Variables panel has:
- A **filter dropdown**: Local / Global / $special / CONST
- A **search box**: filters the currently-visible rows by case-insensitive substring match on name; re-applied after every repopulate (frame switch, filter/category change, step)
- A **Hiddens checkbox**: when unchecked, names starting with `_` or `$_` are hidden from all filters

Categorization (after the hidden check):
- `$`-prefix → $special
- ALL_UPPERCASE with at least one letter → CONST
- Name in `local_scope` → Local
- Otherwise → Global

`_filtered_vars(frame_data, category, show_hidden)` computes the display dict for the selected category; `DebuggerPane._apply_search_filter()` then hides/shows rows in the resulting table by name substring — it does not affect `_filtered_vars` or category membership. Only vars in `dyn_names` are editable, and only in the Local filter of the innermost frame. `get_modifications()` skips non-editable rows.

Right-clicking a variable in the **DebuggerPane** variable table opens a context menu with **Print to Console** and **View as…** options via `build_viewer_menu()` (from `data_viewers.py`): ListViewer for lists/objects, VNFViewer for `[vertices, faces]` structures, GridViewer for lists of lists of points, PathViewer for point sequences. **Print to Console** emits `DebuggerPane.print_value_to_console(name, value)` (not `print_to_console`) — connected to `MainWindow._on_debug_print_value` → `self._console.append_value(name, value, ...)`, so the value is stored for the console right-click viewer menu. Output routes to the window-level `self._console` (a singleton). See `docs/editor.md § Data Viewers` for viewer details.

Right-clicking a variable name **in the code editor** while the debugger is paused provides the same **Print** and **View** actions. See `docs/editor.md § Editor Context Menu`.

## DebuggerPane states

Toolbar button order: Continue/Pause · Step Over · Step Into · Step to Child · Step Out · Restart · Stop

Keyboard shortcuts (window-scoped `QShortcut` objects on `MainWindow`, connected to `btn.click`):

| Key | Action |
|---|---|
| F5 | Continue / Pause |
| F10 | Step Over |
| F11 | Step Into |
| ⌃F11 (Ctrl+F11) | Step to Child |
| Shift+F11 | Step Out |
| Shift+Cmd+F5 | Restart |
| Shift+F5 | Stop |

### Button behaviors

**Continue** — Resume execution until the next breakpoint is reached.

**Pause** — Pause execution at the currently executing line. (Continue and Pause share a single button that toggles based on state.)

**Step Over** — Resume execution until code at this call stack level (or shallower, if the function returns) is on another line in the current file, or a breakpoint is reached. User-defined function calls at exactly `_step_depth + 1` (direct calls on the stepped-over line) emit `DebugSession.logged_value(display_name, value)`, routed through `MainWindow._on_debug_print_value` → `log_value_to_tab` → `tab.console.append_value`. This stores the original Python value for the console right-click viewer menu. Nested calls within those functions do not print.

**Step Into** — Resume execution until code is on another line or in another file, at any call stack level, or a breakpoint is reached.

**Step to Child** — From a paused module call site (e.g. `foo(bar) {`), resume execution and stop the first time control reaches one of that call's own `{ ... }` children statements — wherever the module body's `children()`/`children(N)` ends up invoking them — regardless of how much of the module's own internal logic (tag filtering, BOSL2 attachment math, etc.) runs first. Unlike Step Over (skips the whole call as one unit) or Step Into (enters the module's own *definition* body, forcing you to step through its internals line by line), this jumps straight to the geometry the *caller* actually wrote. If the module never calls `children()` at all, falls back to stopping when the call returns — the same safety net Step Out relies on, so it can never hang.

Implementation: `Evaluator._check_debug` stashes `self._last_children_positions` — the `(origin, line)` pairs of the paused `ModularCall` node's own top-level, non-declaration children — right before invoking the debug hook, via `Evaluator._child_statement_positions(node)`. This is deliberately *not* threaded through the `debug_hook` callback's own signature (which would require updating every hand-rolled test hook in `tests/test_debugger.py`); instead, `DebugSession`'s hook reads `self._ev._last_children_positions` directly at resume time, capturing it into `self._step_to_child_targets` — the same pattern already used for `_ev.csg_tree`/`generate_tree()` in the live partial-render feature above. `hook()`'s step dispatch gains a `"to_child"` branch: `step_hit = (resolved_origin, line) in self._step_to_child_targets or depth < self._step_depth` (both gated by `not expr_level`) — the first clause catches whichever child is reached first in execution order (e.g. a module calling `children(1)` before `children(0)`), the second is the call-return fallback.

**Step Out** — Resume execution until the call stack level is less than the current level, or a breakpoint is reached. The return value of the function being exited is emitted via `DebugSession.logged_value` (same viewer-aware path as Step Over).

**Restart** — Restart the program from the beginning and pause at the first line of code (break-on-first).

**Stop** — Stop execution and terminate the debugger session.

### Pane state table

| Method | Status label | Continue/Pause btn | Step buttons | Stop | Restart |
|---|---|---|---|---|---|
| `set_running()` | "Running…" | **Pause** (enabled) | Disabled | Enabled | Enabled |
| `set_paused(line, frames, stack, origin, partial_error)` | "Paused at line N" | **Continue** (enabled) | All enabled | Enabled | Enabled |
| `set_error_break(line, msg, frames, stack, origin, partial_error)` | "Line N: \<error\>" | **Continue** (enabled) | Disabled | Enabled | Enabled |
| `set_idle()` | "Not debugging" | **Continue** (disabled) | Disabled | Disabled | Disabled |

The Continue/Pause button is a single `_btn_continue` widget whose icon/behavior depends on state: running → pause icon, emits `pause_requested`; otherwise → continue icon, emits `continue_requested`. `_set_continue_mode()` restores the continue icon and clears `_is_running`; called at the start of `set_paused`, `set_error_break`, and `set_idle`.

`set_paused`/`set_error_break`'s `partial_error` (Phase 3, default `None`) is forwarded to `_set_partial_warning()`, which shows/hides the `_partial_warn_label` described above.

**Restart** — `_on_debug_restart()` in `main_window.py` stops the current session (`self._debug_session.stop()`, sets `self._debug_session = None`), clears execution line highlights across all tabs, then calls `_start_debug()`. Since `_debug_session` is already `None`, the "already running → continue" guard doesn't fire and a fresh parse + session starts from the top.
