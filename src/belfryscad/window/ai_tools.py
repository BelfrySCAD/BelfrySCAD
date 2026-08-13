"""Tools the AI chat pane exposes to the model, plus the frozen context
snapshot they operate on.

Everything here is deliberately Qt-free and runs on the background worker
thread (see ai_chat._AIWorker). Tool handlers therefore never touch a live
CodeEditor/QTextDocument -- those aren't thread-safe. Instead MainWindow
builds an AIToolContext of plain data on the GUI thread before each turn,
exactly as _RenderWorker already receives `source = tab.editor.toPlainText()`
as a plain string rather than reaching into the widget from its own thread.

Two independent guards apply to every path-accepting tool, both required,
each with its own error message so the model can tell them apart:
  1. .scad extension only -- the model can't read or write anything else.
  2. containment -- library reads can't escape the library directory.
"""
from __future__ import annotations

import base64
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Permission modes, mirroring Claude Code's. Only proposals are gated --
# reads, renders and measurements can't damage anything.
MODE_PLAN = "plan"          # no changes at all; describe them instead
MODE_MANUAL = "manual"      # every change reviewed as a diff (default)
MODE_ACCEPT = "accept"      # changes apply immediately
MODE_AUTO = "auto"          # ...and self-scheduled follow-ups may loop freely
MODES = (MODE_PLAN, MODE_MANUAL, MODE_ACCEPT, MODE_AUTO)
MODE_LABELS = {MODE_PLAN: "Plan", MODE_MANUAL: "Manual",
               MODE_ACCEPT: "Accept Edits", MODE_AUTO: "Auto"}

_MAX_LIBRARY_FILES = 500
_MAX_FILE_BYTES = 400_000
# A search that matches half of BOSL2 is a search worth narrowing, and the
# whole point of the tool is to cost less than reading the file.
_MAX_SEARCH_RESULTS = 200
_MAX_SEARCH_LINE = 200

SYSTEM_PROMPT = """\
You are an assistant embedded in BelfrySCAD, a hybrid OpenSCAD-style CAD \
application. You help the user write and edit OpenSCAD (.scad) scripts.

You have tools to list and read the user's installed OpenSCAD libraries \
(read-only), to list and read the scripts they currently have open, and to \
propose edits to those scripts or propose entirely new ones.

You cannot write to any file directly. propose_script_edit and \
propose_new_script queue a change for the user to review as a diff and \
accept or reject themselves. After calling one, tell the user what you \
proposed and why -- do not claim the change has been applied, and do not \
ask them to confirm again; the review UI already does that.

You can only read and write .scad files. Requests involving any other file \
type must be declined.

When a render fails or looks wrong, read_console shows the errors, warnings and echo() output it produced -- usually naming the problem and its line number.

A script often includes .scad files sitting beside it. Those are neither open in a tab nor in the libraries -- read them with read_project_file, and list_project_files shows what is there. Follow a script's include/use statements rather than guessing at what they define.

evaluate_expression answers what a value actually is, in the script's own scope -- len(pts), a function's result, whether a variable is what you assumed. It runs the script, so it is not free, but it beats reasoning about a value you could simply look at.

The debugger answers questions a value cannot: which branch ran, how deep the recursion went, what a variable was at the moment something went wrong. debug_start runs to the first stop, debug_resume steps or continues, debug_stop ends it. Stepping is expression-level: `into` on an assignment stops within its expression before entering the call, so entering a function from a line like `x = f(y);` takes two steps rather than one. Always stop the session when finished -- a paused one holds the evaluator. Prefer evaluate_expression when you only want a value; the debugger is for questions about control flow.

read_profile shows where a render spent its time, for questions about why something is slow. Only a render started with profile=true is instrumented, so that pairing -- render(profile=true), then a when="render" follow-up, then read_profile -- is how a speed question gets answered.

Use search_library to find things in the installed libraries. Their files run to hundreds of kilobytes, so reading one to find a single module wastes most of what you read; search for the definition, then read only if you need the surrounding code.

check_geometry answers whether the model is a sound closed solid and whether it would print -- holes, seams where three faces meet an edge, pinched vertices, disagreeing winding. describe_geometry measures; check_geometry judges. It writes no file.

You can also look at the rendered 3D viewport with view_viewport when the question is about how the model actually looks -- shape, proportion, orientation -- rather than about the source text, and measure it exactly with describe_geometry.

Rendering is asynchronous and takes anywhere from a moment to a minute. Applying an edit starts a render by itself, and render() starts one for a script that has not been rendered yet -- but in both cases the result does not exist when the tool returns. To see it, call schedule_followup(when="render") and read the geometry, console and viewport in the turn that follows. Reading them before then shows the previous render, not yours; the console's "Rendered successfully at HH:MM:SS" line tells you which render you are looking at.

The user chooses a mode. In Plan mode the propose_* tools refuse and you should describe changes in prose instead; in the other modes a change is either reviewed by the user or applied straight away, which the tool result will tell you.

schedule_followup asks to be prompted again later. Use it only when there is something specific to check after a wait; don't schedule one just to keep talking, and stop once the task is finished.

Many scripts expose Customizer parameters -- top-level values meant to be \
tweaked without editing code. When one already covers what is being asked \
for, change it with propose_parameter_change rather than rewriting the \
code around it; list_parameters shows what a script exposes and what each \
value is limited to.

Prefer reading the relevant script or library file before proposing changes \
to it. To change part of a script use propose_script_replace, quoting the \
passage exactly as it appears -- re-sending a whole file to alter a few \
lines is slow and risks quietly dropping something along the way. \
propose_script_edit takes the complete new contents and is for a rewrite, \
or for edits spread across most of the file."""


@dataclass
class TabSnapshot:
    """One open editor tab, frozen at the start of a turn."""
    id: int
    name: str
    path: str | None
    modified: bool
    text: str


@dataclass
class Proposal:
    """A change the model wants to make, pending the user's review."""
    kind: str                 # "edit" | "new_file"
    summary: str
    new_content: str
    diff_text: str
    tab_id: int | None = None
    filename: str | None = None
    # Set by propose_script_replace: the exact text this change is anchored
    # to, and what replaces it. new_content is still the whole file, for the
    # review diff -- but applying re-finds the anchor in the live buffer and
    # rewrites only that span, so an edit the user made elsewhere while the
    # turn was running survives instead of being overwritten by a file built
    # from the turn-start snapshot.
    anchor: str | None = None
    replacement: str | None = None
    # Set by propose_parameter_change: {name: value}. Carried as the change
    # itself rather than as finished text, for the same reason as `anchor`
    # -- it is re-applied to the live buffer on accept, so the Customizer
    # values the user moved while the turn was running are not rolled back
    # by a file built from the turn-start snapshot.
    param_changes: dict | None = None


TRIGGER_DELAY = "delay"
TRIGGER_RENDER = "render"
TRIGGERS = (TRIGGER_DELAY, TRIGGER_RENDER)


@dataclass
class Followup:
    """A request from the model to be prompted again later -- after a delay,
    or as soon as the next render finishes (so the viewport image and the
    geometry measurements reflect the change that was just made)."""
    prompt: str
    delay_s: float = 0.0
    trigger: str = TRIGGER_DELAY


@dataclass
class ToolImage:
    """An image result. Handlers return this instead of a string; each
    transport renders it in whatever way its protocol allows."""
    data_b64: str
    mime: str = "image/png"
    caption: str = ""


@dataclass
class AIToolContext:
    library_dir: Path
    open_tabs: list[TabSnapshot] = field(default_factory=list)
    # Called with a Proposal; in the real app this is a Qt Signal's emit,
    # which is safe to call from the worker thread.
    on_proposal: Callable[[Proposal], None] | None = None
    # PNG of the viewport as it looked when this turn started. Captured on
    # the GUI thread with the rest of the snapshot -- grabFramebuffer()
    # can't be called from the worker thread.
    viewport_png: bytes | None = None
    viewport_note: str = ""
    # Measurements of the last render, computed on the GUI thread with the
    # rest of the snapshot (Manifold objects aren't carried across).
    geometry_summary: str = ""
    # Renders one named view on demand and returns (base64 png, note).
    # Blocks until the GUI thread services it -- see MainWindow's
    # _capture_view_threadsafe.
    capture_view: Callable[[str, dict], "tuple[str, str] | None"] | None = None
    # Called with a Followup to schedule one, or None to cancel.
    on_followup: Callable[["Followup | None"], None] | None = None
    mode: str = MODE_MANUAL
    # Tail of the console at the start of this turn: render errors,
    # warnings and echo() output.
    console_text: str = ""
    # Shows the ask_user dialog and returns at once -- it does NOT wait for
    # an answer. The answer arrives later as an ordinary user message, and
    # dismissing the dialog cancels the running turn. Returns False if a
    # question is already on screen.
    ask_user: Callable[[list[dict]], bool] | None = None
    # Reads geometry_summary/console_text/rendering from the GUI thread as
    # they are *now*. The snapshot fields above are frozen at turn start,
    # which is wrong for anything the model changed during the turn:
    # accepting a proposal re-renders, so by the time it looks, the frozen
    # summary describes the model from before its own edit -- or, if
    # nothing had been rendered when the turn began, says nothing has been
    # rendered at all. Falls back to the snapshot when unavailable.
    live_state: Callable[[], dict] | None = None
    # Starts a render of the current tab and returns at once. The result is
    # not available until the render finishes -- pair it with
    # schedule_followup(when="render").
    request_render: Callable[[int | None, bool], bool] | None = None
    # Runs the mesh soundness check over the last render, on the GUI thread.
    # Slower than the other reads -- it is a full topology pass, and on the
    # merged mesh a union -- so it is its own call rather than part of the
    # per-turn snapshot.
    check_geometry: Callable[[], str] | None = None
    # The open scripts as they are now. open_tabs() prefers this over the
    # per-turn snapshot above, so a script the user edited mid-turn is read
    # as it stands rather than as it was when the turn began.
    live_tabs: Callable[[], "list[TabSnapshot]"] | None = None
    # Directories of the user's open scripts -- the only places the project
    # file tools may read. Opening a file is what makes its folder legible.
    project_dirs: Callable[[], "list[Path]"] | None = None
    # Where the last profiled render spent its time, as text. Empty when
    # nothing has been profiled -- that render is explicitly chosen by the
    # user, so the model cannot cause one.
    profile_report: Callable[[], str] | None = None
    # Issues one debugger command and blocks until the session next comes
    # to rest -- a pause, an error, or the end of the run. The session is
    # stateful and a tool call is not, so "step and tell me where I am" has
    # to be a single call.
    debug_control: Callable[[str, object], dict] | None = None


def is_path_within(root: Path, path: Path) -> bool:
    """Whether `path` resolves to somewhere inside `root` -- the traversal
    guard for library reads. Generalizes the same resolve()/is_relative_to()
    check main_window.py already uses to decide if an opened file is a
    read-only library file."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _is_scad(name: str) -> bool:
    return name.lower().endswith(".scad")


def list_library_files(ctx: AIToolContext) -> str:
    root = ctx.library_dir
    if not root.is_dir():
        return "No OpenSCAD libraries are installed."
    # as_posix(), not str(): these become `include <BOSL2/std.scad>`
    # statements, and OpenSCAD wants forward slashes on every platform --
    # str() hands back BOSL2\\std.scad on Windows, which does not resolve.
    paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.scad"))
    if not paths:
        return "No OpenSCAD libraries are installed."
    listed = paths[:_MAX_LIBRARY_FILES]
    out = "\n".join(listed)
    if len(paths) > _MAX_LIBRARY_FILES:
        out += f"\n... ({len(paths) - _MAX_LIBRARY_FILES} more not shown)"
    return out


def read_library_file(ctx: AIToolContext, path: str) -> str:
    if not _is_scad(path):
        return "Error: only .scad files can be read."
    full = ctx.library_dir / path
    if not is_path_within(ctx.library_dir, full):
        return "Error: path is outside the library directory."
    if not full.is_file():
        return f"Error: no such library file: {path}"
    data = full.read_bytes()
    if len(data) > _MAX_FILE_BYTES:
        return (f"Error: {path} is too large to read "
                f"({len(data)} bytes, limit {_MAX_FILE_BYTES}).")
    return data.decode("utf-8", errors="replace")


def search_library(ctx: AIToolContext, pattern: str, path: str = "",
                   max_results: int = 60) -> str:
    """Grep the libraries. Finding one module otherwise means reading a
    whole file, and BOSL2's are 240-330 KB each."""
    root = ctx.library_dir
    if not root.is_dir():
        return "No OpenSCAD libraries are installed."
    if not (pattern or "").strip():
        return "Error: pattern is required."
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regular expression: {e}"

    base = root
    if path:
        base = root / path
        if not is_path_within(root, base):
            return "Error: path is outside the library directory."
        if not base.exists():
            return f"Error: no such library path: {path}"
        if base.is_file() and not _is_scad(str(base)):
            return "Error: only .scad files can be searched."

    files = [base] if base.is_file() else sorted(base.rglob("*.scad"))
    try:
        limit = max(1, min(int(max_results), _MAX_SEARCH_RESULTS))
    except (TypeError, ValueError):
        limit = _MAX_SEARCH_RESULTS

    hits, scanned, truncated = [], 0, False
    for f in files[:_MAX_LIBRARY_FILES]:
        scanned += 1
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f.relative_to(root).as_posix()   # quoted back as an include path
        for n, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                if len(hits) >= limit:
                    truncated = True
                    break
                line = line.strip()
                if len(line) > _MAX_SEARCH_LINE:
                    line = line[:_MAX_SEARCH_LINE] + "..."
                hits.append(f"{rel}:{n}: {line}")
        if truncated:
            break

    if not hits:
        where = f" under {path}" if path else ""
        return f"No matches for {pattern!r}{where} ({scanned} file(s) searched)."
    out = "\n".join(hits)
    if truncated:
        out += (f"\n... (stopped at {limit} matches; narrow the pattern or "
                f"pass a path to see the rest)")
    return out


def list_open_scripts(ctx: AIToolContext) -> str:
    tabs = open_tabs(ctx)
    if not tabs:
        return "No scripts are currently open."
    return json.dumps([
        {"id": t.id, "name": t.name, "path": t.path, "modified": t.modified}
        for t in tabs
    ], indent=2)


def _project_roots(ctx: AIToolContext) -> list[Path]:
    """Directories the project tools may read. Falls back to the folders of
    open tabs that have a path, so this works without the GUI hook."""
    if ctx.project_dirs is not None:
        try:
            dirs = ctx.project_dirs()
        except Exception:      # noqa: BLE001
            dirs = None
        if dirs:
            return [Path(d) for d in dirs]
    seen, out = set(), []
    for t in ctx.open_tabs:
        if not t.path:
            continue
        d = Path(t.path).resolve().parent
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


_NO_PROJECT = ("Error: no project directory is available -- that needs at "
               "least one open script that has been saved to disk. An "
               "unsaved tab has no folder to read from.")


def _resolve_project_path(ctx: AIToolContext, path: str):
    """(resolved path, error). A path is taken relative to each project
    root in turn, so the model can pass what an include statement says."""
    roots = _project_roots(ctx)
    if not roots:
        return None, _NO_PROJECT
    if not _is_scad(path):
        return None, "Error: only .scad files can be read."
    for root in roots:
        full = root / path
        # Containment is checked after resolving, so "../secrets.scad"
        # cannot walk out of the project even though it names a .scad file.
        if is_path_within(root, full) and full.is_file():
            return full, None
    listed = ", ".join(str(r) for r in roots)
    return None, (f"Error: no such project file: {path}. Searched: {listed}. "
                  f"Use list_project_files to see what is there.")


def list_project_files(ctx: AIToolContext) -> str:
    """The .scad files alongside the user's open scripts."""
    roots = _project_roots(ctx)
    if not roots:
        return _NO_PROJECT
    out = []
    for root in roots:
        try:
            found = sorted(p for p in root.rglob("*.scad") if p.is_file())
        except OSError:
            continue
        listed = found[:_MAX_LIBRARY_FILES]
        out.append(f"{root}:")
        out.extend(f"  {p.relative_to(root).as_posix()}" for p in listed)
        if len(found) > len(listed):
            out.append(f"  ... ({len(found) - len(listed)} more not shown)")
    if len(out) <= len(roots):
        return "No .scad files were found alongside the open scripts."
    return "\n".join(out)


def read_project_file(ctx: AIToolContext, path: str) -> str:
    """Read a .scad file next to one of the open scripts -- the sibling a
    script includes, which is neither open in a tab nor in the libraries."""
    full, err = _resolve_project_path(ctx, path)
    if err:
        return err
    try:
        data = full.read_bytes()
    except OSError as e:
        return f"Error: {path} could not be read ({e})."
    if len(data) > _MAX_FILE_BYTES:
        return (f"Error: {path} is too large to read "
                f"({len(data)} bytes, limit {_MAX_FILE_BYTES}).")
    text = data.decode("utf-8", errors="replace")
    return text if text.strip() else f"({path} is empty)"


def open_tabs(ctx: AIToolContext) -> list[TabSnapshot]:
    """The open scripts as they are now, falling back to the turn-start
    snapshot. Everything that reads or edits a script goes through here, so
    a buffer the user changed mid-turn is seen by all of it at once rather
    than by whichever tool happened to be written last."""
    if ctx.live_tabs is not None:
        try:
            tabs = ctx.live_tabs()
        except Exception:      # noqa: BLE001 -- the snapshot still answers
            tabs = None
        if tabs:
            return tabs
    return ctx.open_tabs


def _find_tab(ctx: AIToolContext, id: int) -> TabSnapshot | None:
    return next((t for t in open_tabs(ctx) if t.id == id), None)


def read_open_script(ctx: AIToolContext, id: int) -> str:
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
    if not tab.text.strip():
        # Said outright: an empty string back from a tool reads as the tool
        # having failed, not as an empty file.
        return f"(script {id} is empty)"
    return tab.text


def _diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{name} (current)", tofile=f"{name} (proposed)",
    ))


_PROPOSED = ("Change proposed to the user for review. Do not tell them it "
             "has been applied, and do not ask them to confirm it again.")


_PLAN_REFUSAL = ("Error: you are in Plan mode, so no changes can be made "
                 "yet. Describe the change you would make instead -- include "
                 "the code -- and let the user switch modes if they want it "
                 "applied.")


MAX_ASK_QUESTIONS = 6
MAX_ASK_OPTIONS = 8


def ask_user(ctx: AIToolContext, questions: list | None = None) -> str:
    """Put questions to the user and wait for the answers."""
    if ctx.ask_user is None:
        return ("Error: this conversation cannot ask the user questions. "
                "Decide with what you have, or say what you would need.")
    if not questions:
        return "Error: ask_user needs at least one question."
    if len(questions) > MAX_ASK_QUESTIONS:
        return (f"Error: at most {MAX_ASK_QUESTIONS} questions at once. Ask "
                f"the most important ones first.")

    cleaned = []
    for q in questions:
        if not isinstance(q, dict) or not (q.get("question") or "").strip():
            return "Error: every question needs a 'question' string."
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict) and o.get("label")]
        if len(opts) < 2:
            return ("Error: every question needs at least two options with a "
                    "'label'. To ask something open-ended, just say it in your "
                    "reply instead.")
        cleaned.append({
            "question": str(q["question"]).strip(),
            "header": str(q.get("header") or "").strip(),
            "multiSelect": bool(q.get("multiSelect")),
            "options": opts[:MAX_ASK_OPTIONS],
        })

    if not ctx.ask_user(cleaned):
        return ("Error: a question is already on screen. Wait for the user to "
                "answer that one.")
    # Deliberately does not wait. Blocking here would hold the tool call --
    # and with it an MCP request -- open for as long as the user takes to
    # think, which outlasts client timeouts and pins a worker thread on
    # something that is not work.
    #
    # The answer comes back as an ordinary user message on a new turn, so
    # the model must stop here rather than pressing on with the guess it was
    # trying to avoid.
    return ("Asked. Stop now and say nothing further -- their answer will "
            "arrive as a new message. Do not guess in the meantime, and do "
            "not ask again.")


def propose_script_edit(ctx: AIToolContext, id: int, new_content: str,
                        summary: str) -> str:
    if ctx.mode == MODE_PLAN:
        return _PLAN_REFUSAL
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
    if tab.text == new_content:
        return "Error: the proposed content is identical to the current content."
    diff_text = _diff(tab.text, new_content, tab.name)
    if ctx.on_proposal:
        ctx.on_proposal(Proposal(
            kind="edit", summary=summary, new_content=new_content,
            diff_text=diff_text, tab_id=id, filename=tab.name,
        ))
    return _PROPOSED


def propose_script_replace(ctx: AIToolContext, id: int, old_text: str,
                           new_text: str, summary: str) -> str:
    """Replace one exact passage. Anchored on content rather than on line
    numbers, so a buffer that moved underneath fails the match instead of
    being corrupted silently."""
    if ctx.mode == MODE_PLAN:
        return _PLAN_REFUSAL
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
    if not old_text:
        return ("Error: old_text is required. To create a file use "
                "propose_new_script; to rewrite one wholesale use "
                "propose_script_edit.")
    if old_text == new_text:
        return "Error: old_text and new_text are identical."

    found = tab.text.count(old_text)
    if found == 0:
        return ("Error: old_text does not appear in the script. It must match "
                "exactly, including whitespace and indentation. Read the "
                "script again rather than guessing at it.")
    if found > 1:
        return (f"Error: old_text appears {found} times, so which one is "
                f"meant is ambiguous. Include enough surrounding lines to "
                f"make it unique.")

    new_content = tab.text.replace(old_text, new_text, 1)
    if ctx.on_proposal:
        ctx.on_proposal(Proposal(
            kind="edit", summary=summary, new_content=new_content,
            diff_text=_diff(tab.text, new_content, tab.name), tab_id=id,
            filename=tab.name, anchor=old_text, replacement=new_text,
        ))
    return _PROPOSED


def _customizer():
    """Imported on use: customizer.py pulls in Qt, and this module is
    deliberately importable without it."""
    from belfryscad.window.customizer import describe_parameters, write_back_value
    return describe_parameters, write_back_value


def list_parameters(ctx: AIToolContext, id: int) -> str:
    """The script's customizer parameters, as the Customizer pane shows
    them: current value, type, group, and whatever constrains it."""
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
    describe, _ = _customizer()
    try:
        params = describe(tab.text)
    except Exception as e:      # noqa: BLE001
        return f"Error: the parameters could not be read ({e})."
    if not params:
        return ("This script has no customizer parameters. They are "
                "top-level assignments before the first module or function; "
                "a trailing comment like // [0:100] constrains one.")
    return json.dumps(params, indent=1)


def _check_param_value(spec: dict, value) -> str | None:
    """Why `value` is not allowed for this parameter, or None."""
    name, kind = spec["name"], spec["type"]
    if kind == "boolean":
        if not isinstance(value, bool):
            return f"Error: {name} is a boolean; got {value!r}."
        return None
    # bool is an int in Python, and silently writing back `true` for a
    # number would be a puzzling thing to review.
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"Error: {name} is a number; got {value!r}."
    elif kind == "string":
        if not isinstance(value, str):
            return f"Error: {name} is a string; got {value!r}."
    elif kind == "vector":
        if (not isinstance(value, list)
                or any(isinstance(x, bool) or not isinstance(x, (int, float))
                       for x in value)):
            return f"Error: {name} is a vector of numbers; got {value!r}."
        want = len(spec.get("value") or [])
        if want and len(value) != want:
            return (f"Error: {name} has {want} element(s); got "
                    f"{len(value)}.")

    options = spec.get("options")
    if options:
        allowed = [o["value"] for o in options]
        if value not in allowed:
            return (f"Error: {name} must be one of {allowed!r}; got "
                    f"{value!r}.")
        return None

    rng = spec.get("range")
    if rng:
        vals = value if isinstance(value, list) else [value]
        for v in vals:
            if not (rng["min"] <= v <= rng["max"]):
                return (f"Error: {name} is limited to "
                        f"{rng['min']}..{rng['max']}; got {v!r}.")
    return None


def propose_parameter_change(ctx: AIToolContext, id: int, changes: dict,
                             summary: str) -> str:
    """Change customizer parameter values. This edits the script -- the
    values live in the source as top-level assignments -- so it is reviewed
    like any other proposal."""
    if ctx.mode == MODE_PLAN:
        return _PLAN_REFUSAL
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
    if not isinstance(changes, dict) or not changes:
        return ("Error: changes must be an object of parameter names to new "
                'values, e.g. {"height": 20, "rounded": true}.')

    describe, write_back = _customizer()
    try:
        known = {p["name"]: p for p in describe(tab.text)}
    except Exception as e:      # noqa: BLE001
        return f"Error: the parameters could not be read ({e})."
    if not known:
        return "Error: this script has no customizer parameters to change."

    for name, value in changes.items():
        if name not in known:
            return (f"Error: {name!r} is not a parameter of this script. "
                    f"It has: {', '.join(sorted(known))}.")
        problem = _check_param_value(known[name], value)
        if problem:
            return problem

    new_content = tab.text
    for name, value in changes.items():
        new_content = write_back(new_content, name, value)
    if new_content == tab.text:
        return ("Error: those parameters already have those values; nothing "
                "would change.")

    if ctx.on_proposal:
        ctx.on_proposal(Proposal(
            kind="edit", summary=summary, new_content=new_content,
            diff_text=_diff(tab.text, new_content, tab.name), tab_id=id,
            filename=tab.name, param_changes=dict(changes),
        ))
    return _PROPOSED


def propose_new_script(ctx: AIToolContext, filename: str, content: str,
                       summary: str) -> str:
    if ctx.mode == MODE_PLAN:
        return _PLAN_REFUSAL
    if not _is_scad(filename):
        return "Error: only .scad files can be created."
    if "/" in filename or "\\" in filename:
        return "Error: filename must not contain a path, just a name."
    diff_text = _diff("", content, filename)
    if ctx.on_proposal:
        ctx.on_proposal(Proposal(
            kind="new_file", summary=summary, new_content=content,
            diff_text=diff_text, filename=filename,
        ))
    return _PROPOSED


def image_to_png(image, max_px: int = 1024) -> bytes | None:
    """Scale a QImage down to `max_px` on its long edge and encode it as
    PNG. Kept apart from the grab itself so it's testable without a GL
    context: a retina viewport is far bigger than any model needs and
    costs prompt tokens for nothing."""
    from PySide6.QtCore import QBuffer, Qt
    if image is None or image.isNull():
        return None
    if max(image.width(), image.height()) > max_px:
        image = image.scaled(max_px, max_px,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    # QBuffer() owns its byte array. Constructing it as QBuffer(QByteArray())
    # instead segfaults: the temporary is freed immediately and the buffer is
    # left pointing at it.
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buf, "PNG"):
        return None
    return bytes(buf.data())


VIEWS = ("current", "front", "back", "left", "right", "top", "bottom", "iso")
PROJECTIONS = ("perspective", "orthographic")


def view_viewport(ctx: AIToolContext, view: str = "current",
                  azimuth: float | None = None, elevation: float | None = None,
                  projection: str | None = None, axes: bool | None = None,
                  edges: bool | None = None) -> "ToolImage | str":
    """Hand back a rendered view so the model can judge shape and
    proportion, not just source text. Anything but the untouched "current"
    view is rendered fresh -- one image from one arbitrary camera is a poor
    way to read a 3D shape, which is why engineering drawings use several.

    azimuth/elevation aim the camera anywhere; the named views are just
    convenient angles. projection/axes/edges override the user's own
    display settings for this one image without disturbing them.
    """
    view = (view or "current").lower()
    if view not in VIEWS:
        return f"Error: unknown view {view!r}. Choose one of: {', '.join(VIEWS)}."
    if projection is not None:
        projection = str(projection).lower()
        if projection not in PROJECTIONS:
            return (f"Error: projection must be one of: "
                    f"{', '.join(PROJECTIONS)}.")
    for name, angle in (("azimuth", azimuth), ("elevation", elevation)):
        if angle is not None:
            try:
                float(angle)
            except (TypeError, ValueError):
                return f"Error: {name} must be a number of degrees."
    if elevation is not None and not (-90 <= float(elevation) <= 90):
        return "Error: elevation must be between -90 and 90 degrees."

    overrides = {"projection": projection, "axes": axes, "edges": edges}
    custom_angle = azimuth is not None or elevation is not None
    if custom_angle:
        overrides["azimuth"] = None if azimuth is None else float(azimuth)
        overrides["elevation"] = None if elevation is None else float(elevation)
    plain = not custom_angle and all(v is None for v in overrides.values())

    if view == "current" and plain:
        if not ctx.viewport_png:
            return ("Error: no rendered view is available. Ask the user to "
                    "render the model first (Design > Render).")
        return ToolImage(
            data_b64=base64.b64encode(ctx.viewport_png).decode("ascii"),
            mime="image/png",
            caption=ctx.viewport_note or "Current viewport",
        )

    if ctx.capture_view is None:
        return "Error: only the current view is available in this session."
    got = ctx.capture_view(view, overrides)
    if not got:
        return (f"Error: couldn't render the {view} view. The model may not "
                f"have been rendered yet (Design > Render).")
    data_b64, note = got
    return ToolImage(data_b64=data_b64, mime="image/png", caption=note)


MIN_FOLLOWUP_DELAY = 5
MAX_FOLLOWUP_DELAY = 3600


def schedule_followup(ctx: AIToolContext, delay_seconds: float = 0,
                      prompt: str = "", when: str = TRIGGER_DELAY) -> str:
    """Ask to be re-prompted later -- after a delay, or once the next
    render finishes."""
    if ctx.on_followup is None:
        return "Error: follow-ups aren't available in this session."
    when = (when or TRIGGER_DELAY).lower()
    if when not in TRIGGERS:
        return f"Error: when must be one of: {', '.join(TRIGGERS)}."

    try:
        delay = float(delay_seconds or 0)
    except (TypeError, ValueError):
        return "Error: delay_seconds must be a number."

    if when == TRIGGER_DELAY and delay <= 0:
        ctx.on_followup(None)
        return "Any pending follow-up has been cancelled."
    if not prompt.strip():
        return "Error: prompt is required unless delay_seconds is 0 to cancel."

    if when == TRIGGER_RENDER:
        ctx.on_followup(Followup(prompt=prompt.strip(),
                                 trigger=TRIGGER_RENDER))
        return ("Queued until the next render finishes -- you'll then be able "
                "to see the result with view_viewport and measure it with "
                "describe_geometry. If nothing is rendering, ask the user to "
                "render (Design > Render). Say briefly what you'll check; "
                "don't repeat this back at length.")

    if not (MIN_FOLLOWUP_DELAY <= delay <= MAX_FOLLOWUP_DELAY):
        return (f"Error: delay_seconds must be between {MIN_FOLLOWUP_DELAY} "
                f"and {MAX_FOLLOWUP_DELAY}.")
    ctx.on_followup(Followup(prompt=prompt.strip(), delay_s=delay))
    return (f"Follow-up scheduled in {delay:.0f}s. The user can see it "
            f"counting down and can cancel it. Don't repeat this back to "
            f"them at length; just say briefly what you'll check.")


def _live(ctx: AIToolContext) -> dict:
    """Current geometry/console/rendering, or the turn-start snapshot if the
    GUI thread can't be reached."""
    if ctx.live_state is not None:
        try:
            state = ctx.live_state()
        except Exception:      # noqa: BLE001 -- the snapshot still answers
            state = None
        if state:
            return state
    return {"geometry": ctx.geometry_summary, "console": ctx.console_text,
            "rendering": False}


def _wait_for_render(what: str) -> str:
    """What to tell the model when there is nothing to read yet. A render is
    asynchronous, so 'in progress' is the normal answer immediately after
    one is triggered -- the model needs to be told to come back, not to ask
    the user for something already underway."""
    return (f"A render is in progress, so {what} isn't available yet. Call "
            'schedule_followup(when="render") to be prompted again as soon '
            "as it finishes.")


def read_console(ctx: AIToolContext) -> str:
    """The console output from the last render -- errors, warnings and
    echo() results. This is where a failed render explains itself."""
    state = _live(ctx)
    text = (state.get("console") or "")
    if not text.strip():
        if state.get("rendering"):
            return _wait_for_render("its output")
        return ("The console is empty. Nothing has been rendered yet, or the "
                "render produced no output.")
    return text


def describe_geometry(ctx: AIToolContext) -> str:
    """Measurements of the rendered solid -- the questions neither the
    source text nor a picture can answer."""
    state = _live(ctx)
    if not state.get("geometry"):
        if state.get("rendering"):
            return _wait_for_render("measurements")
        return ("Error: nothing has been rendered yet. Call render() to "
                "render the current script, then "
                'schedule_followup(when="render") to be prompted with the '
                "result.")
    return state["geometry"]


def check_geometry(ctx: AIToolContext) -> str:
    """Is the rendered model a sound closed solid -- per part, and as the
    file would be written."""
    if ctx.check_geometry is None:
        return "Error: the geometry check isn't available in this session."
    state = _live(ctx)
    if not state.get("geometry"):
        if state.get("rendering"):
            return _wait_for_render("the check")
        return ("Error: nothing has been rendered yet. Call render() first, "
                'then schedule_followup(when="render").')
    try:
        out = ctx.check_geometry()
    except Exception as e:      # noqa: BLE001
        return f"Error: the check could not be run ({e})."
    return out or "Error: the check returned nothing."


def read_profile(ctx: AIToolContext) -> str:
    """Where the last profiled render spent its time."""
    if ctx.profile_report is None:
        return "Error: profiling isn't available in this session."
    try:
        out = ctx.profile_report()
    except Exception as e:      # noqa: BLE001
        return f"Error: the profile could not be read ({e})."
    if not out:
        return ("Error: nothing has been profiled yet. Call "
                'render(profile=true) and then schedule_followup(when='
                '"render"); the user can also run Design > Render with '
                "Profiling themselves.")
    return out


# Evaluating an expression means running the script, so a runaway one has
# to be stoppable.
_EVAL_TIMEOUT = 60
_EVAL_MARK = "__belfryscad_ai_eval__"


def evaluate_expression(ctx: AIToolContext, id: int, expression: str) -> str:
    """Evaluate an OpenSCAD expression in the script's own top-level scope.

    Runs the real evaluator on the script with an echo() appended, so
    everything the script defines -- variables, functions, included
    libraries -- is in scope and the semantics are exactly OpenSCAD's,
    rather than a second implementation that would drift.
    """
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
    expr = (expression or "").strip().rstrip(";").strip()
    if not expr:
        return "Error: expression is required."

    from belfryscad import scad_temp
    try:
        from openscad_cpp_evaluator import Evaluator
    except ImportError:
        return "Error: the evaluator isn't available in this session."

    lines: list[str] = []
    # Written beside the script when it has one, so its relative use/include
    # statements still resolve -- the same convention _RenderWorker follows.
    tmp = None
    try:
        tmp = scad_temp.write_temp_scad(
            tab.text + f"\necho({_EVAL_MARK} = {expr});\n", near=tab.path)
        Evaluator(echo_fn=lines.append).evaluate(tmp)
    except Exception as e:      # noqa: BLE001 -- reported, not raised
        first = str(e).strip().splitlines()
        return ("Error: " + (first[0] if first else str(e))
                + ("\n" + "\n".join(first[1:6]) if len(first) > 1 else ""))
    finally:
        scad_temp.remove(tmp)

    for line in reversed(lines):
        if _EVAL_MARK in line:
            _, _, value = line.partition(f"{_EVAL_MARK} = ")
            return value.strip() or "undef"
    # The echo never ran: the script returned early, or something above it
    # failed in a way the evaluator reported without raising.
    other = "\n".join(lines[-10:])
    return ("Error: the expression was never reached -- the script did not "
            "run to the end."
            + (f" The script's own output was:\n{other}" if other else ""))


DEBUG_COMMANDS = ("continue", "into", "over", "out", "to_child")


def _format_debug_state(state: dict) -> str:
    """A pause, as the model should read it."""
    if not state:
        return "Error: the debugger did not answer."
    status = state.get("status", "unknown")
    msg = state.get("message") or ""

    if status == "idle":
        return "No debug session is running." + (f" {msg}" if msg else "")
    if status == "stopped":
        return "Debug session stopped."
    if status == "finished":
        return ("The script ran to completion without stopping again. The "
                "session has ended; the render is now the debugged run's "
                "result.")
    if status == "error":
        return f"Error: {msg}"
    if status == "running":
        return (f"Still running -- {msg} Call debug_stop() to give up, or "
                f"debug_state() to look again.")

    head = ("Paused" if status == "paused" else "Stopped by an error")
    where = f"{state.get('file') or '?'}:{state.get('line')}"
    out = [f"{head} at {where}." + (f" {msg}" if msg else "")]

    stack = state.get("stack") or []
    if stack:
        out.append("Call stack (innermost first):")
        out.extend(f"  {i}: {s}" for i, s in enumerate(stack))

    for n, frame in enumerate(state.get("frames") or []):
        variables = frame.get("variables") or {}
        if not variables:
            continue
        label = "innermost frame" if n == 0 else f"frame {n}"
        out.append(f"Variables in the {label}:")
        out.extend(f"  {k} = {v}" for k, v in variables.items())
        if frame.get("truncated"):
            out.append(f"  ... ({frame['truncated']} more not shown)")
    return "\n".join(out)


def debug_start(ctx: AIToolContext, id: int, breakpoints: list | None = None) -> str:
    """Start debugging a script and run to the first stop."""
    if ctx.debug_control is None:
        return "Error: the debugger isn't available in this session."
    if _find_tab(ctx, id) is None:
        return f"Error: no open script with id {id}."
    lines = []
    for ln in (breakpoints or []):
        try:
            n = int(ln)
        except (TypeError, ValueError):
            return f"Error: breakpoints must be line numbers; got {ln!r}."
        if n < 1:
            return "Error: line numbers start at 1."
        lines.append(n)

    tab = _find_tab(ctx, id)
    note = ""
    if lines and not tab.path:
        # Breakpoints are collected per saved file; an unsaved buffer has no
        # path to key them under, so they would silently never fire.
        note = ("\nNote: this script has never been saved, so breakpoints "
                "cannot be matched to it. It stopped at the first line "
                "instead; step from there.")
    state = ctx.debug_control("start", {"id": id, "breakpoints": lines})
    passed = ""
    if lines and state.get("status") == "paused" and state.get("line") not in lines:
        # A session always stops at the first statement before honouring any
        # breakpoint. Asking for a breakpoint means asking to get there, so
        # the initial stop is stepped past here rather than costing a whole
        # extra round trip -- but it is reported, since top-level state at
        # that point is sometimes what was actually wanted.
        first = state.get("line")
        state = ctx.debug_control("resume", "continue")
        passed = (f"(A session always pauses at the first statement, line "
                  f"{first}; ran on to your breakpoint.)\n")
    return passed + _format_debug_state(state) + note


def debug_resume(ctx: AIToolContext, command: str = "continue") -> str:
    """Let the paused script run on, and report where it stops next."""
    if ctx.debug_control is None:
        return "Error: the debugger isn't available in this session."
    cmd = (command or "continue").strip().lower()
    if cmd not in DEBUG_COMMANDS:
        return (f"Error: command must be one of "
                f"{', '.join(DEBUG_COMMANDS)}; got {command!r}.")
    return _format_debug_state(ctx.debug_control("resume", cmd))


def debug_state(ctx: AIToolContext) -> str:
    """Where the session is now, without moving it."""
    if ctx.debug_control is None:
        return "Error: the debugger isn't available in this session."
    return _format_debug_state(ctx.debug_control("state", None))


def debug_stop(ctx: AIToolContext) -> str:
    """End the debug session."""
    if ctx.debug_control is None:
        return "Error: the debugger isn't available in this session."
    return _format_debug_state(ctx.debug_control("stop", None))


def render(ctx: AIToolContext, id: int | None = None,
           profile: bool = False) -> str:
    """Render a script. Returns as soon as the render starts."""
    if ctx.request_render is None:
        return "Error: rendering isn't available in this session."
    if id is not None and _find_tab(ctx, id) is None:
        return f"Error: no open script with id {id}."
    if not ctx.request_render(id, bool(profile)):
        return ("Error: there is nothing to render -- no script is open, or "
                "the one asked for is empty.")
    tail = ("only then read the result with describe_geometry, read_console "
            "or view_viewport.")
    if profile:
        tail = ("only then read where the time went with read_profile. "
                "Instrumenting a render makes it slower, so the timings are "
                "useful for comparing call sites against each other, not as "
                "absolute figures.")
    return ("Render started. It is not finished yet: call "
            'schedule_followup(when="render") to be prompted once it is, and '
            + tail)


# Tool specs. json_schema is plain JSON Schema, which is what both OpenAI's
# function.parameters and Anthropic's input_schema want -- each provider's
# request builder wraps these differently, but the schema itself is shared.
TOOLS: list[dict] = [
    {
        "name": "list_library_files",
        "description": "List the .scad files in the user's installed OpenSCAD libraries.",
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": list_library_files,
    },
    {
        "name": "read_library_file",
        "description": ("Read one .scad file from the installed OpenSCAD "
                        "libraries. Path is relative to the library root, as "
                        "returned by list_library_files."),
        "json_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": read_library_file,
    },
    {
        "name": "search_library",
        "description": (
            "Search the installed OpenSCAD libraries with a regular "
            "expression, returning file:line for each match. Prefer this "
            "over read_library_file when looking for where something is "
            "defined or how it is used -- library files run to hundreds of "
            "kilobytes, and reading one to find a single module is wasteful. "
            r"To find a definition, search for something like "
            r"'^\\s*module\\s+cuboid' rather than just the name."),
        "json_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "Python regular expression."},
                "path": {"type": "string",
                         "description": ("Limit the search to this file or "
                                         "directory, relative to the library "
                                         "root. Optional.")},
                "max_results": {"type": "integer",
                                "description": "Default 60."},
            },
            "required": ["pattern"],
        },
        "handler": search_library,
    },
    {
        "name": "list_project_files",
        "description": (
            "List the .scad files sitting alongside the user's open scripts. "
            "These are their own project files -- the siblings a script "
            "includes -- which are neither open in a tab nor part of the "
            "installed libraries, and are otherwise unreadable."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": list_project_files,
    },
    {
        "name": "read_project_file",
        "description": (
            "Read a .scad file from the folder of one of the open scripts. "
            "Pass the path as an include statement writes it, relative to "
            "the script's own folder. Only that folder tree is readable, "
            "and only .scad files."),
        "json_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": ("Relative to an open script's "
                                         "folder, e.g. common.scad or "
                                         "parts/bracket.scad.")},
            },
            "required": ["path"],
        },
        "handler": read_project_file,
    },
    {
        "name": "list_open_scripts",
        "description": "List the scripts the user currently has open, with their ids.",
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": list_open_scripts,
    },
    {
        "name": "read_open_script",
        "description": "Read the current contents of one open script, by id.",
        "json_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        "handler": read_open_script,
    },
    {
        "name": "propose_script_edit",
        "description": ("Propose replacing an open script's entire contents. "
                        "The user reviews the change as a diff and accepts or "
                        "rejects it; it is not applied automatically. Prefer "
                        "propose_script_replace for a change to part of a "
                        "script -- use this one for a rewrite, or when the "
                        "edits are spread across most of the file."),
        "json_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "new_content": {"type": "string",
                                "description": "Complete new file contents."},
                "summary": {"type": "string",
                            "description": "One line describing the change."},
            },
            "required": ["id", "new_content", "summary"],
        },
        "handler": propose_script_edit,
    },
    {
        "name": "propose_script_replace",
        "description": (
            "Propose replacing one exact passage of an open script -- the "
            "usual way to change part of a file, rather than re-sending the "
            "whole thing. old_text must appear exactly once and match "
            "character for character, including indentation; include enough "
            "surrounding lines to make it unique. The user reviews it as a "
            "diff like any other proposal. Anchoring on the text rather than "
            "on line numbers means that if the script changed after you read "
            "it, the change is refused rather than applied in the wrong "
            "place."),
        "json_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "old_text": {"type": "string",
                             "description": ("Exact text to replace, unique "
                                             "within the script.")},
                "new_text": {"type": "string",
                             "description": ("What replaces it. Empty to "
                                             "delete the passage.")},
                "summary": {"type": "string",
                            "description": "One line describing the change."},
            },
            "required": ["id", "old_text", "new_text", "summary"],
        },
        "handler": propose_script_replace,
    },
    {
        "name": "list_parameters",
        "description": (
            "The script's Customizer parameters -- the top-level values a "
            "user tweaks without editing code -- with each one's current "
            "value, type, group, description, and any range or list of "
            "options it is limited to. Read this before changing a "
            "parameter, and prefer changing one over rewriting the code "
            "when the script already exposes what you need."),
        "json_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        "handler": list_parameters,
    },
    {
        "name": "propose_parameter_change",
        "description": (
            "Change one or more Customizer parameter values. Pass changes as "
            "an object of name to new value. These values live in the script "
            "as top-level assignments, so this edits the script and is "
            "reviewed as a diff like any other proposal. Values are checked "
            "against the parameter's type and against any range or option "
            "list before being proposed."),
        "json_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "changes": {
                    "type": "object",
                    "description": ('Parameter names to new values, e.g. '
                                    '{"height": 20, "rounded": true}.'),
                },
                "summary": {"type": "string",
                            "description": "One line describing the change."},
            },
            "required": ["id", "changes", "summary"],
        },
        "handler": propose_parameter_change,
    },
    {
        "name": "propose_new_script",
        "description": ("Propose creating a new .scad script. The user reviews "
                        "it and accepts or rejects it; it is not created "
                        "automatically."),
        "json_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "A bare .scad filename, no directories."},
                "content": {"type": "string"},
                "summary": {"type": "string",
                            "description": "One line describing the script."},
            },
            "required": ["filename", "content", "summary"],
        },
        "handler": propose_new_script,
    },
    {
        "name": "view_viewport",
        "description": ("Look at the rendered 3D model as a picture. Use "
                        "this to check shape, proportion or orientation -- "
                        "things the source alone doesn't show. Call it more "
                        "than once with different views to understand a "
                        "shape properly. You can aim the camera anywhere "
                        "with azimuth/elevation and override projection, "
                        "axes and edges for the image without changing the "
                        "user's own settings. Requires a vision-capable "
                        "model."),
        "json_schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string", "enum": list(VIEWS),
                    "description": ("A named angle; 'current' is the user's "
                                    "own camera. Ignored if azimuth or "
                                    "elevation is given."),
                },
                "azimuth": {
                    "type": "number",
                    "description": "Camera azimuth in degrees, any value.",
                },
                "elevation": {
                    "type": "number",
                    "description": "Camera elevation in degrees, -90 to 90.",
                },
                "projection": {
                    "type": "string", "enum": list(PROJECTIONS),
                    "description": ("Orthographic is better for judging "
                                    "true proportions and alignment."),
                },
                "axes": {"type": "boolean",
                         "description": "Show the axis indicator."},
                "edges": {"type": "boolean",
                          "description": "Outline the mesh edges."},
            },
            "required": [],
        },
        "handler": view_viewport,
    },
    {
        "name": "describe_geometry",
        "description": ("Exact measurements of the rendered solid: volume, "
                        "surface area, bounding box, number of separate "
                        "parts, and genus (how many through-holes). Answers "
                        "what neither the source nor a picture can, and "
                        "needs no vision support."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": describe_geometry,
    },
    {
        "name": "read_console",
        "description": ("Read the console: render errors, warnings and "
                        "echo() output from the most recent render. Check "
                        "this when a render fails or behaves unexpectedly -- "
                        "it usually says exactly what went wrong, with a "
                        "line number."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": read_console,
    },
    {
        "name": "check_geometry",
        "description": (
            "Check whether the rendered model is a sound closed manifold "
            "solid -- reported per part, and for the merged mesh an export "
            "would actually write. Names boundary edges (holes), edges "
            "shared by three or more faces, pinched vertices, disagreeing "
            "winding, and degenerate or duplicated faces. Use this to answer "
            "whether a model is printable; it writes no file. Slower than "
            "the other reads, so call it when soundness is the question "
            "rather than routinely."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": check_geometry,
    },
    {
        "name": "debug_start",
        "description": (
            "Start debugging a script, and run until it first stops. "
            "Breakpoints are line numbers; they are added to any the user "
            "has already set and appear in the editor gutter, so the user "
            "can see where you chose to stop. A session always pauses at "
            "the first statement; with breakpoints given, that stop is "
            "stepped past for you and reported. Returns where it stopped, "
            "the call stack "
            "and the variables in each frame. Only one session at a time -- "
            "call debug_stop when finished, since a paused session holds "
            "the evaluator."),
        "json_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "breakpoints": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "1-based line numbers. Optional.",
                },
            },
            "required": ["id"],
        },
        "handler": debug_start,
    },
    {
        "name": "debug_resume",
        "description": (
            "Let a paused script run on, and report where it stops next. "
            "continue runs to the next breakpoint; into steps into a call; "
            "over steps across one; out runs until the current call "
            "returns; to_child steps into a child of the current module "
            "call. Blocks until it stops, so a long stretch between "
            "breakpoints takes as long as the script does. Stepping is "
            "expression-level, so into on a line like `x = f(y);` stops "
            "first inside that line's expression and stays on the same "
            "line -- call into again to enter f. Staying on the same line "
            "is progress, not a failed step."),
        "json_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "enum": list(DEBUG_COMMANDS),
                            "description": "Default continue."},
            },
            "required": [],
        },
        "handler": debug_resume,
    },
    {
        "name": "debug_state",
        "description": ("Where the debug session is now -- location, call "
                        "stack and variables -- without moving it."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": debug_state,
    },
    {
        "name": "debug_stop",
        "description": ("End the debug session. Always do this when "
                        "finished: a paused session holds the evaluator and "
                        "blocks ordinary rendering."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": debug_stop,
    },
    {
        "name": "read_profile",
        "description": (
            "Where the last profiled render spent its time: the call tree "
            "and the slowest call sites, with file and line. Use it when the "
            "question is why a model is slow. Only available after the user "
            "runs Design > Render with Profiling -- an ordinary render is "
            "not instrumented, and you cannot start a profiled one."),
        "json_schema": {"type": "object", "properties": {}, "required": []},
        "handler": read_profile,
    },
    {
        "name": "evaluate_expression",
        "description": (
            "Evaluate an OpenSCAD expression in a script's own top-level "
            "scope and return the value. Everything the script defines is "
            "in scope -- its variables, its functions, anything it includes "
            "-- so this answers questions like len(path) or "
            "bounding_box(pts)[1] without adding an echo() and re-rendering. "
            "It runs the script, so it costs about what a render costs; "
            "prefer it over guessing at a value, not over reading the "
            "source."),
        "json_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "expression": {"type": "string",
                               "description": ("An OpenSCAD expression, "
                                               "without the trailing "
                                               "semicolon.")},
            },
            "required": ["id", "expression"],
        },
        "handler": evaluate_expression,
    },
    {
        "name": "render",
        "description": (
            "Render the current script, so its geometry can then be measured "
            "or looked at. Applying an edit already renders by itself, so "
            "this is for when nothing has been rendered yet or the source "
            "changed some other way. Rendering is asynchronous: this returns "
            "immediately, and the result only exists once the render "
            'finishes -- follow it with schedule_followup(when="render") '
            "rather than reading straight away. Renders the active tab "
            "unless an id is given; note that rendering a different script "
            "makes it the active one, which is what describe_geometry, "
            "read_console and view_viewport then describe. Pass profile=true "
            "to instrument the render so read_profile can then say where the "
            "time went -- that is the only way to produce a profile, and it "
            "makes the render itself slower, so ask for it when the question "
            "is about speed rather than routinely."),
        "json_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer",
                       "description": ("Which open script to render, from "
                                       "list_open_scripts. Defaults to the "
                                       "active tab.")},
                "profile": {"type": "boolean",
                            "description": ("Instrument the render for "
                                            "read_profile. Slower; default "
                                            "false.")},
            },
            "required": [],
        },
        "handler": render,
    },
    {
        "name": "schedule_followup",
        "description": (
            "Ask to be prompted again after a delay, to continue a task in "
            "steps or check on something that takes time. The user sees it "
            "counting down and can cancel. Use sparingly: only schedule a "
            "follow-up when there is something concrete to check later, and "
            "stop scheduling once the task is done. Pass delay_seconds 0 to "
            "cancel a pending follow-up."),
        "json_schema": {
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "number",
                    "description": (f"Between {MIN_FOLLOWUP_DELAY} and "
                                    f"{MAX_FOLLOWUP_DELAY}; 0 cancels."),
                },
                "prompt": {
                    "type": "string",
                    "description": "What to ask yourself when it fires.",
                },
                "when": {
                    "type": "string", "enum": list(TRIGGERS),
                    "description": ("'delay' waits delay_seconds; 'render' "
                                    "waits for the next render to finish."),
                },
            },
            "required": [],
        },
        "handler": schedule_followup,
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the user a question they must answer before you can do the "
            "right thing, offering the choices you would otherwise guess "
            "between. Blocks until they answer or dismiss it.\n\n"
            "Use it when the answer changes what you build and you cannot "
            "settle it from the script, the geometry or the conversation -- "
            "not to confirm something you already have good reason to "
            "believe, and not for choices with an obvious default. One "
            "interruption that prevents building the wrong thing is worth "
            "it; a habit of asking is not.\n\n"
            "Every option needs a label and a short description of what "
            "choosing it means. Set multiSelect when the choices genuinely "
            "combine. Where the difference between options is more than a "
            "line can carry, give each one 'detail' as well -- markdown "
            "shown beside the list.\n\n"
            "Each question gets its own tab, so ask them together rather "
            "than one at a time."),
        "json_schema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": (f"1 to {MAX_ASK_QUESTIONS} questions, "
                                    "asked together on one page."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question, as you would say it aloud.",
                            },
                            "header": {
                                "type": "string",
                                "description": ("Two or three words naming the "
                                                "decision, e.g. 'Units' or "
                                                "'Wall thickness'."),
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": ("true for checkboxes when the "
                                                "options combine; false (the "
                                                "default) for one-of radio "
                                                "buttons."),
                            },
                            "options": {
                                "type": "array",
                                "description": f"2 to {MAX_ASK_OPTIONS} choices.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string",
                                                  "description": "The choice itself, a few words."},
                                        "description": {"type": "string",
                                                        "description": ("One line on what choosing "
                                                                        "it means. Shown under the "
                                                                        "option.")},
                                        "detail": {"type": "string",
                                                   "description": (
                                                       "Optional. Longer markdown -- a sketch of "
                                                       "the result, a code fragment, the "
                                                       "trade-off spelled out. Shown in a pane "
                                                       "beside the options, so the user can read "
                                                       "about one without losing sight of the "
                                                       "others. Give it to every option of a "
                                                       "question or none, since they are read "
                                                       "against each other.")},
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            "required": ["questions"],
        },
        "handler": ask_user,
    },
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


def run_tool(ctx: AIToolContext, name: str, arguments: dict) -> "str | ToolImage":
    """Dispatch one tool call. Returns the string fed back to the model --
    including for errors, which are reported to the model rather than
    raised, so it can correct itself and continue."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Error: unknown tool {name!r}."
    try:
        return handler(ctx, **arguments)
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001 -- surfaced to the model, not swallowed
        return f"Error running {name}: {e}"
