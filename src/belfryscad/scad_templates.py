"""Editor templates: named snippets for Edit > Insert Template.

OpenSCAD's format and OpenSCAD's user folder, so templates written for
OpenSCAD work here unchanged and the reverse. One JSON file per template:

    {"key": "for", "content": "for (i = [^~^ : ]) {\\n\\t\\n}"}

`key` is the name listed, `content` the text inserted, `^~^` where the
cursor is left (the end, without one), and a tab is one indent level. An
optional integer `offset` places the cursor instead of the marker; it is
turned into a marker on load, so the rest of this module sees one form.

Built-ins ship in resources/templates; the user's are read from
`<OpenSCAD user config>/templates` and replace a built-in of the same key.
Qt-free, so all of this is testable without a widget.
"""
from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

MARKER = "^~^"
BUILTIN_DIR = Path(__file__).parent / "resources" / "templates"


def user_dir() -> Path:
    """OpenSCAD's PlatformUtils::userConfigPath() + /templates."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "OpenSCAD" / "templates"


@dataclass
class Template:
    key: str
    content: str            # with at most one MARKER; tabs are indent levels
    path: Path | None       # the file it came from; None for one not saved yet
    builtin: bool           # from resources/templates, never written to


def read(path: Path, builtin: bool = False) -> Template:
    """One template file. Raises ValueError (json's included) or OSError."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("key"), str) \
            or not isinstance(data.get("content"), str):
        raise ValueError("needs a string \"key\" and a string \"content\"")
    content = data["content"]
    offset = data.get("offset", -1)
    if isinstance(offset, int) and 0 <= offset <= len(content):
        content = content[:offset] + MARKER + content[offset:]
    return Template(data["key"], content, path, builtin)


def load(builtin_dir: Path = BUILTIN_DIR, user: Path | None = None
         ) -> tuple[dict[str, Template], dict[str, Template], list[str]]:
    """(effective templates by key, built-ins by key, errors). A user
    template replaces the built-in of the same key; a file that cannot be
    read is skipped and named in `errors`, as OpenSCAD logs it."""
    errors: list[str] = []

    def scan(d: Path, builtin: bool) -> dict[str, Template]:
        out: dict[str, Template] = {}
        if not d.is_dir():
            return out
        for p in sorted(d.glob("*.json")):
            try:
                t = read(p, builtin)
            except (OSError, ValueError) as e:
                errors.append(f"Error reading template file '{p}': {e}")
                continue
            out[t.key] = t
        return out

    builtins = scan(builtin_dir, True)
    templates = {**builtins, **scan(user if user is not None else user_dir(), False)}
    return templates, builtins, errors


def expand(content: str, indent_unit: str, line_indent: str = "") -> tuple[str, int]:
    """The text to insert and the cursor's offset into it. Tabs become
    `indent_unit`; every line after the first also gets `line_indent`, the
    indentation of the line it is inserted into, so a block inserted inside
    a block lines up."""
    at = content.find(MARKER)
    if at < 0:
        at = len(content)
    before = content[:at].replace("\t", indent_unit).replace("\n", "\n" + line_indent)
    after = content[at:].replace(MARKER, "", 1).replace("\t", indent_unit).replace("\n", "\n" + line_indent)
    return before + after, len(before)


def filename_for(key: str, taken: set[Path], folder: Path) -> Path:
    """A new file for `key` in `folder`: the key reduced to filename-safe
    characters, numbered if that file is already there."""
    stem = re.sub(r"[^A-Za-z0-9_+.-]+", "_", key).strip("._") or "template"
    path = folder / f"{stem}.json"
    n = 2
    while path in taken or path.exists():
        path = folder / f"{stem}_{n}.json"
        n += 1
    return path


def save(template: Template, folder: Path | None = None) -> Path:
    """Write a user template, to its own file if it has one, else a new one
    in `folder` (the user folder by default). Never writes a built-in's
    file: saving an edited built-in makes a user copy that replaces it."""
    folder = folder if folder is not None else user_dir()
    path = template.path if template.path and not template.builtin else filename_for(template.key, set(), folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": template.key, "content": template.content}, indent=4) + "\n",
                    encoding="utf-8")
    template.path, template.builtin = path, False
    return path

