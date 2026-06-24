# Debugger Reference

The debugger runs the evaluator in a daemon worker thread (`DebugSession`) and surfaces a single shared `DebuggerPane` (owned by `MainWindow`, not per-tab) with call-stack and variables panels. `_debug_session` and `_debug_tab` on `MainWindow` track the active session and the tab that started it. See `docs/evaluator.md` for the evaluator internals referenced below.

## DebugSession (`debugger.py`)

Signals (emitted from the worker thread; Qt queues them to main):

| Signal | Args | When |
|---|---|---|
| `paused` | `origin, line, all_frame_locals, call_stack` | Hit a breakpoint or step |
| `error_break` | `origin, line, msg, all_frame_locals, call_stack` | Any runtime error |
| `finished` | `bodies, id_to_node` | Evaluation completed |
| `errored` | `str` | Unhandled exception after error_break resume |
| `logged` | `str` | Echo/print output from the evaluator (thread-safe via signal) |

`all_frame_locals` is a list of frame dicts, **innermost first**, with an extra `<toplevel>` entry appended when inside a call. `all_frame_locals[0]` matches row 0 (innermost) of the call-stack list. Each entry:

| Key | Contents |
|---|---|
| `"local_scope"` | Eagerly-assigned vars in the frame's `ctx.dyn`: `__let_*` (params, `for`/`let`, assignments so far) and `$*` specials |
| `"outer_scope"` | Global vars from `_root_ctx.dyn` (innermost frame only, when inside a call; parent frames get `{}`) |
| `"dyn_names"` | `set` of names from `dyn` — the only vars editable via the pane |

**Debug hook** — `_make_hook()` returns a closure passed to `Evaluator(debug_hook=...)`. Signature: `hook(line, depth, *, forced, expr_level, expr_depth, origin, get_frames) → (cmd, mods)`. `origin` is the source file path from the AST node's `position.origin` — `None` for the main file, a path string for included files. `get_frames` is a lazy callback that builds `(locals_dict, all_frame_locals), call_stack` only when called, avoiding the cost on non-pausing hook invocations. Step-into pauses regardless of origin; step-over and step-out track the file where the step was initiated (`_step_origin`) and only pause in that same file. Break-on-first pauses at the first non-expression statement in the toplevel file. Breakpoints are collected from all open tabs as a `{resolved_path: set(lines)}` dict, and the hook resolves `origin` before lookup. When pausing in an included file, `MainWindow._show_debug_line()` opens the file in a new tab (or switches to it if already open) and highlights the execution line via `set_execution_line()`, which uses `scroll_to_line()` to ensure at least 5 lines of context above and below; `_clear_all_execution_lines()` clears stale highlights across all tabs first. The hook builds a **display** call stack with a `("toplevel", "<toplevel>", None)` entry appended before emitting `paused`, blocking on a `threading.Event`.

**Pause during execution** — `DebugSession.pause()` sets `_pause_requested`. The hook checks/consumes this flag at the top of every call, triggering an immediate pause regardless of breakpoints or step state — useful for interrupting a long-running evaluation.

**Error break** — `Evaluator(error_break_fn=self._error_break)` intercepts every `error()` call before raising `EvalError`. `_error_break` emits `error_break` and blocks until the user resumes; afterward `EvalError` propagates normally (caught by `_run`, triggers `errored`).

## Call stack display

Displayed as a top-down call chain: `<toplevel>` at the top, then outermost callee, down to the innermost (currently executing) frame at the bottom. `_call_stack` in the evaluator is outermost-first; the display stack is `[("toplevel", ...)] + list(call_stack)` (no reversal), built in both `_make_hook()` and `_error_break()`. `_all_frame_locals` is reordered to match: `list(reversed(all_frame_locals))` = `[toplevel, outermost, ..., innermost]`. Each non-toplevel entry shows `name()  file:line` using the declaration position (`decl_pos`). The stack list initially selects the innermost frame (`_innermost_row`, the last row).

**Frame navigation** — clicking a call-stack entry navigates to where that frame calls the next one down. `_populate_stack` stores `(file_path, line)` per row in `_stack_positions`: each frame's position is the `call_pos` of its callee (the next entry in the stack), except the innermost frame (last row) which stores the current pause point. Clicking emits `frame_selected(file_path, line)`, handled by `MainWindow._on_debug_frame_selected` which opens/switches to the target file's tab and highlights the line. Display labels use `decl_pos` (definition site) for `name()  file:line`.

When inside a call, a `<toplevel>` frame (`local_scope` = global scope vars) is at row 0. Clicking `<toplevel>` → Locals shows the file's global declarations. Variable editing is only enabled in the innermost frame's Locals view.

## Per-frame variable inspection

The evaluator maintains `_frame_ctxs` (an `EvalContext` list parallel to `_call_stack`), pushed/popped in `_eval_user_module`/`_eval_user_function`. At each `_check_debug`, `local_scope` reads directly from `ctx.dyn` (all `__let_*`/`$*` entries) — no scope walk needed since assignments are eager. When inside a call, `outer_scope` comes from `_root_ctx.dyn` (Globals view). A `<toplevel>` frame (`local_scope = outer_scope`) is appended when `_call_stack` is non-empty.

**Step Into for functions**: function bodies are expressions, so `_eval_statement`'s `_check_debug` never fires for them. `_eval_user_function` explicitly calls `self._check_debug(decl.expr, child_ctx)` after pushing the call frame, before `_eval_expr(decl.expr, child_ctx)` — giving Step Into a pause point at the start of every function body.

**Expression-level step points**: `_check_debug` accepts `expr_level=True` for sub-expression pauses. The debug hook only honours these for `step_into` (`_step_mode`) — gutter breakpoints, step-over, and step-out filter them out (`and not expr_level`). Nodes calling `_check_debug(…, expr_level=True)`:
- **`TernaryOp`** — before condition evaluation, then again at the chosen branch after resolution
- **`ModularIf` / `ModularIfElse`** — `_eval_statement` already pauses at the `if` node; a second `expr_level=True` pause fires at the first statement of the chosen branch (falls back to `node` if the branch is empty)
- **`ListCompIf` / `ListCompIfElse`** — at the `if` node before condition, then at the chosen branch after; in both `_eval_list_comp` and `_eval_list_comp_body`
- **`LetOp`** — before each assignment (statement-level, so step-over pauses on them); `ModularLet` skips the `let(` node and steps through assignments individually
- **`ListCompFor`** — at the start of each iteration, after loop variables bind into `loop_ctx`
- **`ListCompLet`** — before each assignment (statement-level), in both `_eval_list_comp` and `_eval_list_comp_body`
- **`ListCompEach`** — before the body expression, in both `_eval_list_comp` and `_eval_list_comp_body`
- **List element expressions** — before each element-producing expression: the `else` branch in `_eval_list_comp` and the fallthrough in `_eval_list_comp_body`

**Expression-level Step Out**: from an `expr_level` checkpoint, Step Out backs out one level of listcomp nesting (`for`, `if`, `each`, or nested `[...]` body). The evaluator tracks `self._expr_depth: int`, incrementing on entering each listcomp body and decrementing on exit; the hook passes `expr_depth` to `DebugSession`. `_current_pause_expr_depth` stores the depth at pause. If `> 0`, Step Out sets `_step_out_expr_depth = _current_pause_expr_depth - 1`; the hook fires on any checkpoint (including `expr_level=True`) where `expr_depth <= _step_out_expr_depth`. If `== 0`, normal call-stack Step Out applies (`_step_out_depth = depth`).

The Variables panel has:
- A **filter dropdown**: Locals / Globals / CONSTANTS / $Specials
- A **Hiddens checkbox**: when unchecked, names starting with `_` or `$_` are hidden from all filters

Categorization (after the hidden check):
- `$`-prefix → $Specials
- ALL_UPPERCASE with at least one letter → CONSTANTS
- Name in `local_scope` → Locals
- Otherwise → Globals

`_filtered_vars(frame_data, category, show_hidden)` computes the display dict. Only vars in `dyn_names` are editable, and only in the Locals filter of the innermost frame. `get_modifications()` skips non-editable rows.

Right-clicking a variable opens a context menu with **View as…** options via `build_viewer_menu()` (from `data_viewers.py`): ListViewer for lists/objects, VNFViewer for `[vertices, faces]` structures, GridViewer for lists of lists of points, PathViewer for point sequences. See `docs/editor.md § Data Viewers` for viewer details.

## DebuggerPane states

Toolbar button order: Continue/Pause · Step Over · Step Into · Step Out · Restart · Stop

Keyboard shortcuts (window-scoped `QShortcut` objects on `MainWindow`, connected to `btn.click`):

| Key | Action |
|---|---|
| F5 | Continue / Pause |
| F10 | Step Over |
| F11 | Step Into |
| Shift+F11 | Step Out |
| Shift+Cmd+F5 | Restart |
| Shift+F5 | Stop |

### Button behaviors

**Continue** — Resume execution until the next breakpoint is reached.

**Pause** — Pause execution at the currently executing line. (Continue and Pause share a single button that toggles based on state.)

**Step Over** — Execute the current line and pause at the next line in the current function, module, or toplevel context in the current file. If the current line contains function or module calls, execute them all without stepping into them.

**Step Into** — If the current line has no module or function calls, behave the same as Step Over. If it has calls, step into the first one and pause at its first line. If a call is split across multiple lines, step to each argument line until all arguments have been evaluated, then step into the call.

**Step Out** — Continue execution until the current function or module returns. Step to the caller, and if it has another function or module call, step into the next one. If no more calls remain in the caller, pause at the next code line after the last call.

**Restart** — Restart the program from the beginning and pause at the first line of code (break-on-first).

**Stop** — Stop execution and terminate the debugger session.

### Pane state table

| Method | Status label | Continue/Pause btn | Step buttons | Stop | Restart |
|---|---|---|---|---|---|
| `set_running()` | "Running…" | **Pause** (enabled) | Disabled | Enabled | Enabled |
| `set_paused(line, frames, stack, origin)` | "Paused at line N" | **Continue** (enabled) | All enabled | Enabled | Enabled |
| `set_error_break(line, msg, frames, stack, origin)` | "Line N: \<error\>" | **Continue** (enabled) | Disabled | Enabled | Enabled |
| `set_idle()` | "Not debugging" | **Continue** (disabled) | Disabled | Disabled | Disabled |

The Continue/Pause button is a single `_btn_continue` widget whose icon/behavior depends on state: running → pause icon, emits `pause_requested`; otherwise → continue icon, emits `continue_requested`. `_set_continue_mode()` restores the continue icon and clears `_is_running`; called at the start of `set_paused`, `set_error_break`, and `set_idle`.

**Restart** — `_on_debug_restart()` in `main_window.py` stops the current session (`self._debug_session.stop()`, sets `self._debug_session = None`), clears execution line highlights across all tabs, then calls `_start_debug()`. Since `_debug_session` is already `None`, the "already running → continue" guard doesn't fire and a fresh parse + session starts from the top.
