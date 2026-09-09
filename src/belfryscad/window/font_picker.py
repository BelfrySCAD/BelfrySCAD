"""A picker for `font=` arguments — issue #384.

Right-click a `font=` string in the editor and get two panes: the families
this evaluator can resolve, and the styles *that family actually has*.
Save writes the canonical spec back through the same `replace_span` path
the Path/Grid/Matrix "Edit as…" viewers use.

The style pane lists the family's real style strings rather than a fixed
Bold/Italic pair, because 93 of the 379 families on a plain macOS install
have styles that do not reduce to those four — Helvetica Neue alone has
fourteen, including the Thin and UltraLight faces a curated list would
make unreachable. What is listed is literally what goes after `:style=`.

Nothing here parses OpenSCAD: the finder is lexical, like
`find_editable_literals`, and works on the source text around the cursor.
"""
import re

#: Fallback preview when the call has no string of its own to show.
DEFAULT_PREVIEW = "The quick brown fox jumps over the lazy dog"

#: `font` as an argument name, then `=`, then a quoted string. The name is
#: matched on a word boundary so `subfont=` or `myfont=` is not mistaken
#: for it.
_FONT_ARG = re.compile(r'\bfont\s*=\s*"((?:[^"\\]|\\.)*)"')

#: The first string literal in a text() call -- its own `text=` argument,
#: which is the preview the user actually wants to see set in the font.
_TEXT_CALL = re.compile(r'\btext\s*\(', re.MULTILINE)
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def find_font_argument(source: str, offset: int) -> tuple | None:
    """The `font="…"` nearest the cursor, as (start, end, spec).

    `start`/`end` span the quoted string *including* its quotes, so a
    commit replaces the literal and leaves `font=` alone. Matches when the
    cursor is anywhere in `font="…"` -- on the name, the `=`, or inside
    the string -- since all three are places you would right-click.
    """
    for m in _FONT_ARG.finditer(source):
        if m.start() <= offset <= m.end():
            return m.start(1) - 1, m.end(1) + 1, _unescape(m.group(1))
    return None


def find_preview_text(source: str, offset: int) -> str | None:
    """The string the enclosing `text()` call draws, if there is one.

    Seeing the font set in your own label beats seeing a pangram, so the
    picker opens on it. None when the cursor is not in a text() call, or
    the call's string is a variable rather than a literal.
    """
    for m in _TEXT_CALL.finditer(source):
        end = _matching_paren(source, m.end() - 1)
        if end is None or not (m.start() <= offset <= end):
            continue
        args = source[m.end():end]
        # The FIRST string in the call: text()'s own first parameter is
        # the text, and `font=` comes later.
        hit = _STRING.search(args)
        if hit is None:
            return None
        # ... unless that string is some other argument's value, which
        # means the text itself was not a literal.
        before = args[:hit.start()].rstrip()
        if before.endswith("=") and not before.endswith("=="):
            name = before[:-1].strip().split()[-1] if before[:-1].strip() else ""
            if name != "text":
                return None
        return _unescape(hit.group(1))
    return None


def _matching_paren(source: str, open_at: int) -> int | None:
    """Index of the `)` closing the `(` at `open_at`, skipping strings."""
    depth = 0
    i = open_at
    while i < len(source):
        c = source[i]
        if c == '"':
            i += 1
            while i < len(source) and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_spec(spec: str) -> tuple:
    """`"Family:style=Bold"` -> `("Family", "Bold")`.

    Other fontconfig properties are dropped, matching the evaluator, which
    reads `style=` and ignores the rest rather than rejecting a spec
    fontconfig itself would have accepted.
    """
    family, _, props = spec.partition(":")
    style = ""
    for prop in props.split(":"):
        key, eq, value = prop.partition("=")
        if eq and key.strip().casefold() == "style":
            style = value.strip()
        elif not eq and prop.strip():
            style = prop.strip()  # a bare `:Bold`, which fontconfig allows
    return family.strip(), style


def spec_for(family: str, style: str) -> str:
    """The canonical `font=` string. Regular is the bare family -- writing
    `:style=Regular` would teach a habit nobody needs."""
    if not style or style.casefold() == "regular":
        return family
    return f"{family}:style={style}"


def group_by_family(fonts: list) -> dict:
    """`{family: [style, ...]}`, styles in the order a picker should show
    them: Regular first (it is the default), then the rest alphabetically."""
    families: dict = {}
    for f in fonts:
        families.setdefault(f["family"], set()).add(f["style"] or "Regular")
    return {
        family: sorted(styles, key=lambda s: (s.casefold() != "regular", s.casefold()))
        for family, styles in sorted(families.items(), key=lambda kv: kv[0].casefold())
    }


class FontPickerDialog:
    """Two panes, a preview, and a Save that writes the spec back.

    Built lazily through `__new__` returning a QDialog so importing this
    module stays free in a headless process -- the finders above are used
    by tests that never construct a widget.
    """

    def __new__(cls, spec: str = "", preview_text: str | None = None, parent=None,
                fonts: list | None = None):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QVBoxLayout,
        )
        from belfryscad.window.font_list import load_fonts

        all_fonts = load_fonts() if fonts is None else list(fonts)
        families = group_by_family(all_fonts)
        by_key = {(f["family"], f["style"] or "Regular"): f for f in all_fonts}

        want_family, want_style = parse_spec(spec)

        dlg = QDialog(parent)
        dlg.setWindowTitle("Choose Font")
        dlg.resize(720, 520)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dlg)

        search = QLineEdit()
        search.setPlaceholderText("Filter families…")
        search.setClearButtonEnabled(True)
        layout.addWidget(search)

        panes = QHBoxLayout()
        family_list = QListWidget()
        style_list = QListWidget()
        panes.addWidget(family_list, 2)
        panes.addWidget(style_list, 1)
        layout.addLayout(panes)

        layout.addWidget(QLabel("Preview:"))
        preview_edit = QLineEdit(preview_text or DEFAULT_PREVIEW)
        layout.addWidget(preview_edit)
        preview = QLabel()
        preview.setMinimumHeight(72)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(preview)

        spec_label = QLabel()
        spec_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(spec_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                    | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        # Qt cannot render a face by the evaluator's name -- that mismatch
        # is the whole reason this dialog exists -- so the actual file is
        # loaded and Qt's own name for it used for the preview only. A
        # bundled face has no file (its bytes live inside the evaluator),
        # and falls back to whatever Qt substitutes.
        loaded: dict = {}

        def qt_family(font: dict) -> str | None:
            path = font.get("path", "")
            if not path or path.startswith("<"):
                return None
            if path not in loaded:
                fid = QFontDatabase.addApplicationFont(path)
                fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
                loaded[path] = fams[0] if fams else None
            return loaded[path]

        def current():
            fam = family_list.currentItem()
            sty = style_list.currentItem()
            if fam is None:
                return None, None
            return fam.text(), (sty.text() if sty is not None else "Regular")

        def refresh_preview():
            family, style = current()
            if family is None:
                return
            spec_label.setText(f"font = \"{_escape(spec_for(family, style))}\"")
            font = by_key.get((family, style))
            qt_name = qt_family(font) if font else None
            f = QFont(qt_name or family)
            f.setPointSize(28)
            # Only as a hint for a substituted face: a real loaded file
            # already IS the weight, and asking for bold on top double-
            # bolds it.
            if qt_name is None and "bold" in style.casefold():
                f.setBold(True)
            if qt_name is None and ("italic" in style.casefold() or "oblique" in style.casefold()):
                f.setItalic(True)
            preview.setFont(f)
            preview.setText(preview_edit.text())

        def fill_styles():
            item = family_list.currentItem()
            style_list.clear()
            if item is None:
                return
            styles = families.get(item.text(), [])
            style_list.addItems(styles)
            wanted = want_style if want_style in styles else (styles[0] if styles else None)
            if wanted is not None:
                style_list.setCurrentRow(styles.index(wanted))
            refresh_preview()

        def fill_families(needle: str = ""):
            needle = needle.casefold()
            family_list.clear()
            names = [f for f in families if needle in f.casefold()]
            family_list.addItems(names)
            if want_family in names:
                family_list.setCurrentRow(names.index(want_family))
            elif names:
                family_list.setCurrentRow(0)

        search.textChanged.connect(fill_families)
        family_list.currentItemChanged.connect(lambda *_: fill_styles())
        style_list.currentItemChanged.connect(lambda *_: refresh_preview())
        preview_edit.textChanged.connect(lambda *_: refresh_preview())
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)

        fill_families()

        def chosen_spec() -> str:
            family, style = current()
            return spec_for(family, style) if family else spec

        dlg.chosen_spec = chosen_spec
        dlg.families = families
        return dlg


def open_font_picker(spec: str, preview_text: str | None, on_commit, parent=None):
    """Show the picker; call `on_commit(new_spec)` if Save is clicked.

    `on_commit` takes the bare spec, not the quoted literal -- the caller
    knows whether it is writing into source (quotes) or a Customizer field
    (no quotes).
    """
    dlg = FontPickerDialog(spec, preview_text, parent)
    if dlg.exec():
        on_commit(dlg.chosen_spec())
    return dlg
