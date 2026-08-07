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

You can also look at the rendered 3D viewport with view_viewport when the question is about how the model actually looks -- shape, proportion, orientation -- rather than about the source text, and measure it exactly with describe_geometry.

The user chooses a mode. In Plan mode the propose_* tools refuse and you should describe changes in prose instead; in the other modes a change is either reviewed by the user or applied straight away, which the tool result will tell you.

schedule_followup asks to be prompted again later. Use it only when there is something specific to check after a wait; don't schedule one just to keep talking, and stop once the task is finished.

Prefer reading the relevant script or library file before proposing changes \
to it. When proposing an edit, pass the complete new contents of the file, \
not a fragment."""


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
    # Puts questions to the user and blocks until they answer or cancel.
    # Returns one answer per question, or None if they cancelled.
    ask_user: Callable[[list[dict]], "list[dict] | None"] | None = None


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
    paths = sorted(str(p.relative_to(root)) for p in root.rglob("*.scad"))
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


def list_open_scripts(ctx: AIToolContext) -> str:
    if not ctx.open_tabs:
        return "No scripts are currently open."
    return json.dumps([
        {"id": t.id, "name": t.name, "path": t.path, "modified": t.modified}
        for t in ctx.open_tabs
    ], indent=2)


def _find_tab(ctx: AIToolContext, id: int) -> TabSnapshot | None:
    return next((t for t in ctx.open_tabs if t.id == id), None)


def read_open_script(ctx: AIToolContext, id: int) -> str:
    tab = _find_tab(ctx, id)
    if tab is None:
        return f"Error: no open script with id {id}."
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

    answers = ctx.ask_user(cleaned)
    if answers is None:
        # Cancelled. Said plainly so the model does not treat silence as
        # assent and carry on as though it had an answer.
        return ("The user dismissed the question without answering. Do not "
                "ask again unless they bring it up; proceed only with what "
                "you already know, or stop and say what you are waiting on.")

    lines = []
    for spec, ans in zip(cleaned, answers):
        picked = ans.get("selected") or []
        note = (ans.get("note") or "").strip()
        if not picked and not note:
            lines.append(f"{spec['question']}\n  (no answer given)")
            continue
        lines.append(f"{spec['question']}\n  chose: "
                     + (", ".join(picked) if picked else "(nothing)")
                     + (f"\n  added: {note}" if note else ""))
    return "\n".join(lines)


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


def read_console(ctx: AIToolContext) -> str:
    """The console output from the last render -- errors, warnings and
    echo() results. This is where a failed render explains itself."""
    if not ctx.console_text.strip():
        return ("The console is empty. Nothing has been rendered yet, or the "
                "render produced no output.")
    return ctx.console_text


def describe_geometry(ctx: AIToolContext) -> str:
    """Measurements of the rendered solid -- the questions neither the
    source text nor a picture can answer."""
    if not ctx.geometry_summary:
        return ("Error: nothing has been rendered yet. Ask the user to "
                "render the model first (Design > Render).")
    return ctx.geometry_summary


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
                        "rejects it; it is not applied automatically."),
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
            "combine."),
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
                                                        "description": "What choosing it means."},
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
