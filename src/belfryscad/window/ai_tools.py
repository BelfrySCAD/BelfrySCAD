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

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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


@dataclass
class AIToolContext:
    library_dir: Path
    open_tabs: list[TabSnapshot] = field(default_factory=list)
    # Called with a Proposal; in the real app this is a Qt Signal's emit,
    # which is safe to call from the worker thread.
    on_proposal: Callable[[Proposal], None] | None = None


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


def propose_script_edit(ctx: AIToolContext, id: int, new_content: str,
                        summary: str) -> str:
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
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


def run_tool(ctx: AIToolContext, name: str, arguments: dict) -> str:
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
