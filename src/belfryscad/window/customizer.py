"""Customizer pane — mirrors OpenSCAD's Customizer feature.

Scans the active source file for top-level variable assignments with simple
literal values and generates form widgets for them.  Widget changes rewrite
the source in place; the user still has to press F6 to render.

Annotation syntax (in source comments):
    // Description
    variable = value; // [constraint]

Constraint forms:
    [max]              → slider  0 … max
    [min:max]          → slider  min … max
    [min:step:max]     → slider  min … max  step
    [a, b, c]          → dropdown (numeric or string values)
    [a:Label, b:Label] → dropdown with display labels
    N  (integer)       → text field with max length N  (strings only)

Tab groups:
    /* [TabName] */    → start a named tab
    /* [Hidden] */     → suppress following variables from the pane
    /* [Global] */     → shown at the top of every tab; never its own tab

Parameter sets ("presets"), mirroring OpenSCAD's own Customizer + CLI
-p/-P: named snapshots of every parameter's value, stored as JSON
alongside the .scad file (<name>.json). Values are stored using the same
literal syntax as the .scad source itself (_format_value/_parse_literal),
so the file is plain-text-diffable and round-trips through either tool.

Parameter Editor: "Add Parameter..." (always-visible button) and each
parameter row's right-click "Edit Parameter.../Delete Parameter..." open
ParameterEditorDialog (customizer_param_dialog.py) to create/edit/remove a
parameter *definition* itself -- name, type, description, tab, constraint
-- as opposed to the rest of this pane, which only edits an existing
parameter's *value*. Renaming or moving an existing parameter to a
different tab are both out of scope (see the dialog's own doc comment).
"""

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSlider, QSpinBox, QStyle, QTabWidget, QVBoxLayout, QWidget,
)

from belfryscad.scad_literals import (
    parse_literal as _parse_literal, format_value as _format_value,
    preset_path_for, load_presets, save_presets,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParameterDef:
    name: str
    default: Any        # int | float | bool | str | list[int|float]
    description: str
    tab: str
    constraint: str     # raw text after // on the assignment line
    line_num: int       # 0-indexed line of the assignment in source


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------

def _valid_param_name(name: str) -> bool:
    _KEYWORDS = {'true', 'false', 'undef', 'use', 'include',
                 'module', 'function', 'for', 'if', 'else', 'let', 'each'}
    return bool(name) and not name.startswith('$') and name not in _KEYWORDS


def scan_parameters(source: str) -> list[ParameterDef]:
    """Return top-level parameter variable definitions from *source*."""
    params: list[ParameterDef] = []
    lines = source.split('\n')
    current_tab = 'Parameters'
    hidden = False
    depth = 0
    prev_desc = ''

    for i, line in enumerate(lines):
        # Tab-group block comment: /* [TabName] */
        tab_m = re.match(r'^\s*/\*\s*\[([^\]]*)\]\s*\*/\s*$', line)
        if tab_m:
            tab_name = tab_m.group(1).strip()
            if tab_name.lower() == 'hidden':
                hidden = True
            elif tab_name.lower() == 'global':
                current_tab = 'Global'
                hidden = False
            else:
                current_tab = tab_name or 'Parameters'
                hidden = False
            prev_desc = ''
            continue

        if hidden:
            prev_desc = ''
            continue

        # Update brace depth (strip strings and line comments first)
        code = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', line)
        code = re.sub(r'//.*', '', code)
        depth += code.count('{') - code.count('}')
        depth = max(0, depth)

        if depth > 0:
            prev_desc = ''
            continue

        # Description comment: // text
        desc_m = re.match(r'^\s*//\s*(.*?)\s*$', line)
        if desc_m:
            prev_desc = desc_m.group(1).strip()
            continue

        # Blank or block-comment-only line resets description
        if not line.strip() or re.match(r'^\s*/[\*/]', line):
            prev_desc = ''
            continue

        # Assignment: name = literal; // optional constraint
        assign_m = re.match(r'^\s*(\w+)\s*=\s*(.+?);\s*(?://\s*(.*))?$', line)
        if assign_m:
            name = assign_m.group(1)
            val_str = assign_m.group(2).strip()
            constraint = (assign_m.group(3) or '').strip()
            if _valid_param_name(name):
                val = _parse_literal(val_str)
                if val is not None:
                    params.append(ParameterDef(
                        name=name,
                        default=val,
                        description=prev_desc or name,
                        tab=current_tab,
                        constraint=constraint,
                        line_num=i,
                    ))
            prev_desc = ''
            continue

        prev_desc = ''

    return params


def write_back_value(source: str, name: str, new_value: Any) -> str:
    """Replace the value literal of the top-level assignment for *name*."""
    params = scan_parameters(source)
    target = next((p for p in params if p.name == name), None)
    if target is None:
        return source
    lines = source.split('\n')
    line = lines[target.line_num]
    new_val = _format_value(new_value)
    new_line = re.sub(
        r'^(\s*' + re.escape(name) + r'\s*=\s*)(.+?)(;\s*(?://.*)?)?$',
        lambda m: m.group(1) + new_val + (m.group(3) if m.group(3) is not None else ';'),
        line,
    )
    lines[target.line_num] = new_line
    return '\n'.join(lines)


_DESC_LINE_RE = re.compile(r'^\s*//')
_TAB_HEADER_RE = re.compile(r'^\s*/\*\s*\[([^\]]*)\]\s*\*/\s*$')


def insert_parameter(source: str, name: str, default: Any, description: str,
                      tab: str, constraint: str) -> str:
    """Inserts a brand-new top-level parameter (used by the "Add
    Parameter..." dialog) formatted to match what scan_parameters itself
    recognizes. Placement:
      - Right after the last existing parameter already in *tab*, if any.
      - Else, for the default "Parameters" group (never explicitly
        headered -- once a `/* [X] */` header appears, everything after it
        belongs to X until the next header, so a headerless parameter can
        only go before the first header in the file): after the last other
        headerless parameter, else right before the file's first tab-group
        header, else at the very top.
      - Else (a genuinely new named tab, including "Global"): after the
        last parameter overall, with a new `/* [tab] */` header -- always
        a valid place for a header to start a fresh group.
      - Or, if there are no existing parameters at all: at the top of the
        file, with a header if *tab* isn't the default group."""
    params = scan_parameters(source)
    lines = source.split('\n')

    new_lines = []
    if description:
        new_lines.append(f"// {description}")
    val_str = _format_value(default)
    new_lines.append(f"{name} = {val_str}; // {constraint}" if constraint else f"{name} = {val_str};")

    same_tab = [p for p in params if p.tab == tab]
    if same_tab:
        insert_at = max(p.line_num for p in same_tab) + 1
    elif tab == 'Parameters':
        default_params = [p for p in params if p.tab == 'Parameters']
        if default_params:
            insert_at = max(p.line_num for p in default_params) + 1
        else:
            first_header = next((i for i, ln in enumerate(lines) if _TAB_HEADER_RE.match(ln)), None)
            insert_at = first_header if first_header is not None else 0
    else:
        insert_at = max((p.line_num for p in params), default=-1) + 1
        if tab:
            header = ["", f"/* [{tab}] */"] if params else [f"/* [{tab}] */"]
            new_lines = header + new_lines

    lines[insert_at:insert_at] = new_lines
    return '\n'.join(lines)


def replace_parameter(source: str, name: str, new_default: Any,
                       new_description: str, new_constraint: str) -> str:
    """Rewrites an existing parameter's value/description/constraint in
    place -- same line, same tab, same position. Does not rename the
    variable (would require rewriting every use-site in the script) or
    move it between tabs (would require relocating its lines) -- both are
    deliberately out of scope for "Edit Parameter..."."""
    params = scan_parameters(source)
    target = next((p for p in params if p.name == name), None)
    if target is None:
        return source
    lines = source.split('\n')

    val_str = _format_value(new_default)
    lines[target.line_num] = (
        f"{name} = {val_str}; // {new_constraint}" if new_constraint else f"{name} = {val_str};"
    )

    desc_idx = target.line_num - 1
    has_desc_line = desc_idx >= 0 and bool(_DESC_LINE_RE.match(lines[desc_idx]))
    if has_desc_line:
        if new_description:
            lines[desc_idx] = f"// {new_description}"
        else:
            del lines[desc_idx]
    elif new_description:
        lines.insert(target.line_num, f"// {new_description}")

    return '\n'.join(lines)


def delete_parameter(source: str, name: str) -> str:
    """Removes a parameter's assignment line and, if present, its
    directly-preceding description comment line. Leaves any tab-group
    header alone even if this was the header's last remaining parameter
    (an empty tab is harmless -- scan_parameters just produces no
    parameters for it -- and leaving the header is less surprising than
    silently deleting it too)."""
    params = scan_parameters(source)
    target = next((p for p in params if p.name == name), None)
    if target is None:
        return source
    lines = source.split('\n')

    desc_idx = target.line_num - 1
    has_desc_line = desc_idx >= 0 and bool(_DESC_LINE_RE.match(lines[desc_idx]))
    del lines[target.line_num]
    if has_desc_line:
        del lines[desc_idx]

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Constraint parsing
# ---------------------------------------------------------------------------

def _default_step(val: Any) -> float:
    if isinstance(val, float):
        s = f'{val:g}'
        if '.' in s:
            frac = s.split('.')[1].rstrip('0')
            return 10.0 ** -max(1, len(frac))
        return 0.5
    return 1.0


def _parse_constraint(constraint: str, default_val: Any) -> dict:
    c = constraint.strip()

    bracket_m = re.match(r'^\[([^\]]*)\]$', c)
    if bracket_m:
        inner = bracket_m.group(1).strip()

        # Range: [max]  [min:max]  [min:step:max]
        range_m = re.match(
            r'^(-?[\d.]+(?:[eE][+-]?\d+)?)'
            r'(?::(-?[\d.]+(?:[eE][+-]?\d+)?))?'
            r'(?::(-?[\d.]+(?:[eE][+-]?\d+)?))?$',
            inner,
        )
        if range_m:
            a = float(range_m.group(1))
            b = float(range_m.group(2)) if range_m.group(2) is not None else None
            c2 = float(range_m.group(3)) if range_m.group(3) is not None else None
            if b is None:
                return {'type': 'slider', 'min': 0.0, 'max': a,
                        'step': _default_step(default_val)}
            if c2 is None:
                return {'type': 'slider', 'min': a, 'max': b,
                        'step': _default_step(default_val)}
            return {'type': 'slider', 'min': a, 'max': c2, 'step': b}

        # Dropdown: [a, b, c]  or  [a:Label, ...]
        parts = [p.strip() for p in inner.split(',') if p.strip()]
        if parts:
            options = []
            for part in parts:
                kv = part.split(':', 1)
                options.append((kv[0].strip(), kv[1].strip() if len(kv) == 2 else kv[0].strip()))
            return {'type': 'dropdown', 'options': options}

    # Bare integer → string max-length
    if re.match(r'^\d+$', c) and isinstance(default_val, str):
        return {'type': 'string', 'maxlen': int(c)}

    return {'type': 'default'}


def _coerce_option_value(s: str) -> Any:
    """A dropdown option token (e.g. the 'sm' or '10' in '[sm:Small,
    10:Ten]') is always written bare/unquoted regardless of whether it
    represents a number or a string -- unlike _parse_literal, which expects
    quotes for strings. Numeric-looking tokens become int/float; anything
    else stays a string."""
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _build_constraint(kind: str, **opts) -> str:
    """Inverse of _parse_constraint: builds the trailing '// <constraint>'
    text (without the leading //) used by AddParameterDialog/insert_
    parameter/replace_parameter. Returns '' for a kind with no annotation
    (plain number, checkbox, unconstrained string)."""
    if kind in ('slider', 'vector'):
        return f"[{_format_value(opts['min'])}:{_format_value(opts['step'])}:{_format_value(opts['max'])}]"
    if kind == 'dropdown':
        parts = [
            value if label == value else f"{value}:{label}"
            for value, label in opts['options']
        ]
        return f"[{', '.join(parts)}]"
    if kind == 'string' and opts.get('maxlen'):
        return str(opts['maxlen'])
    return ''


# ---------------------------------------------------------------------------
# Per-parameter widgets
# ---------------------------------------------------------------------------

class _BoolWidget(QWidget):
    value_changed = Signal(object)

    def __init__(self, value: bool, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._cb = QCheckBox()
        self._cb.setChecked(bool(value))
        self._cb.toggled.connect(lambda v: self.value_changed.emit(v))
        lay.addWidget(self._cb)
        lay.addStretch()

    def set_value(self, v):
        self._cb.blockSignals(True)
        self._cb.setChecked(bool(v))
        self._cb.blockSignals(False)


class _SliderWidget(QWidget):
    value_changed = Signal(object)

    def __init__(self, value, min_v: float, max_v: float, step: float, parent=None):
        super().__init__(parent)
        self._min = float(min_v)
        self._max = float(max_v)
        self._step = float(step) if float(step) > 0 else 1.0
        self._n = max(1, round((self._max - self._min) / self._step))
        self._is_int = isinstance(value, int) and float(self._step) == int(self._step)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(self._n)
        self._slider.setValue(self._to_tick(value))
        self._slider.valueChanged.connect(self._on_slide)
        lay.addWidget(self._slider, 1)

        self._lbl = QLabel(self._fmt(value))
        self._lbl.setMinimumWidth(52)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._lbl)

    def _to_tick(self, v) -> int:
        return max(0, min(self._n, round((float(v) - self._min) / self._step)))

    def _from_tick(self, t: int):
        v = self._min + t * self._step
        v = max(self._min, min(self._max, v))
        if self._is_int:
            return int(round(v))
        return round(v, 12)

    def _fmt(self, v) -> str:
        if self._is_int:
            return str(int(round(float(v))))
        step_s = f'{self._step:g}'
        if '.' in step_s:
            dec = len(step_s.split('.')[1].rstrip('0') or '0')
        else:
            dec = 0
        return f'{float(v):.{dec}f}'

    def _on_slide(self, t: int):
        v = self._from_tick(t)
        self._lbl.setText(self._fmt(v))
        self.value_changed.emit(v)

    def set_value(self, v):
        self._slider.blockSignals(True)
        self._slider.setValue(self._to_tick(v))
        self._lbl.setText(self._fmt(v))
        self._slider.blockSignals(False)


class _NumberWidget(QWidget):
    value_changed = Signal(object)

    def __init__(self, value, step: float, parent=None):
        super().__init__(parent)
        self._is_int = isinstance(value, int)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if self._is_int:
            self._spin = QSpinBox()
            self._spin.setRange(-(2 ** 30), 2 ** 30)
            self._spin.setSingleStep(max(1, int(step)))
            self._spin.setValue(int(value))
        else:
            self._spin = QDoubleSpinBox()
            self._spin.setRange(-1e9, 1e9)
            step_s = f'{step:g}'
            dec = len(step_s.split('.')[1].rstrip('0')) if '.' in step_s else 0
            self._spin.setDecimals(max(1, dec))
            self._spin.setSingleStep(step)
            self._spin.setValue(float(value))
        self._spin.editingFinished.connect(
            lambda: self.value_changed.emit(self._spin.value())
        )
        lay.addWidget(self._spin)
        lay.addStretch()

    def set_value(self, v):
        self._spin.blockSignals(True)
        self._spin.setValue(int(v) if self._is_int else float(v))
        self._spin.blockSignals(False)


class _ComboWidget(QWidget):
    value_changed = Signal(object)

    def __init__(self, options: list[tuple[str, str]], current, parent=None):
        super().__init__(parent)
        self._options = options
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._combo = QComboBox()
        for val_s, label in options:
            self._combo.addItem(label, val_s)
        cur_s = str(current)
        for i, (v, _) in enumerate(options):
            if v == cur_s:
                self._combo.setCurrentIndex(i)
                break
        self._combo.currentIndexChanged.connect(self._on_changed)
        lay.addWidget(self._combo)
        lay.addStretch()

    def _on_changed(self, idx: int):
        if idx < 0:
            return
        val_s = self._combo.itemData(idx)
        try:
            val = int(val_s)
        except (ValueError, TypeError):
            try:
                val = float(val_s)
            except (ValueError, TypeError):
                val = val_s
        self.value_changed.emit(val)

    def set_value(self, v):
        cur_s = str(v)
        self._combo.blockSignals(True)
        for i, (vs, _) in enumerate(self._options):
            if vs == cur_s:
                self._combo.setCurrentIndex(i)
                break
        self._combo.blockSignals(False)


class _StringWidget(QWidget):
    value_changed = Signal(object)

    def __init__(self, value: str, maxlen: int = 0, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._edit = QLineEdit(str(value))
        if maxlen > 0:
            self._edit.setMaxLength(maxlen)
        self._edit.editingFinished.connect(
            lambda: self.value_changed.emit(self._edit.text())
        )
        lay.addWidget(self._edit)

    def set_value(self, v):
        self._edit.blockSignals(True)
        self._edit.setText(str(v))
        self._edit.blockSignals(False)


class _VectorWidget(QWidget):
    value_changed = Signal(object)

    def __init__(self, value: list, min_v: Optional[float] = None,
                 max_v: Optional[float] = None, step: Optional[float] = None,
                 parent=None):
        super().__init__(parent)
        self._value = list(value)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._spins: list[QDoubleSpinBox] = []
        for i, v in enumerate(value):
            spin = QDoubleSpinBox()
            if min_v is not None and max_v is not None:
                spin.setRange(min_v, max_v)
            else:
                spin.setRange(-1e9, 1e9)
            if step is not None:
                # Same decimals-from-step convention as _NumberWidget, for
                # a [min:step:max] constraint applied to each component.
                step_s = f'{step:g}'
                dec = len(step_s.split('.')[1].rstrip('0')) if '.' in step_s else 0
                spin.setDecimals(max(1, dec))
                spin.setSingleStep(step)
            else:
                spin.setSingleStep(0.1 if isinstance(v, float) else 1.0)
            spin.setValue(float(v))
            spin.setMaximumWidth(75)
            spin.editingFinished.connect(
                lambda idx=i, s=spin: self._on_elem(idx, s.value())
            )
            lay.addWidget(spin)
            self._spins.append(spin)

    def _on_elem(self, idx: int, v: float):
        self._value[idx] = v
        self.value_changed.emit(list(self._value))

    def set_value(self, v: list):
        for spin, val in zip(self._spins, v):
            spin.blockSignals(True)
            spin.setValue(float(val))
            spin.blockSignals(False)
        self._value = list(v)


# ---------------------------------------------------------------------------
# Customizer pane
# ---------------------------------------------------------------------------

class CustomizerPane(QWidget):
    """Form widget that auto-generates controls from top-level parameter
    variables in the active source file.  Emits *source_changed* with the
    updated source text when the user edits a value; does NOT trigger a
    render automatically.
    """

    source_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = ''
        self._params: list[ParameterDef] = []
        self._widgets: dict[str, list[QWidget]] = {}
        self._updating = False
        self._file_path: Optional[str] = None
        self._presets: dict[str, dict[str, Any]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        add_row = QHBoxLayout()
        add_row.setContentsMargins(6, 0, 6, 6)
        self._add_param_btn = QPushButton("+")
        self._add_param_btn.setToolTip("Add a new parameter")
        self._add_param_btn.setFixedWidth(28)
        self._add_param_btn.setFlat(True)
        self._add_param_btn.clicked.connect(self._on_add_parameter)
        add_row.addWidget(self._add_param_btn)
        add_row.addStretch()
        add_row_widget = QWidget()
        add_row_widget.setLayout(add_row)

        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(6, 6, 6, 0)
        preset_row.setSpacing(2)
        preset_row.addWidget(QLabel("Presets"))
        self._preset_combo = QComboBox()
        self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        preset_row.addWidget(self._preset_combo, 1)
        self._preset_save_as_btn = QPushButton("+")
        self._preset_save_as_btn.setToolTip("Save current values as a new preset")
        self._preset_save_as_btn.setFixedWidth(28)
        self._preset_save_as_btn.setFlat(True)
        self._preset_save_as_btn.clicked.connect(self._on_preset_save_as)
        preset_row.addWidget(self._preset_save_as_btn)
        self._preset_delete_btn = QPushButton("−")
        self._preset_delete_btn.setToolTip("Delete the selected preset")
        self._preset_delete_btn.setFixedWidth(28)
        self._preset_delete_btn.setFlat(True)
        self._preset_delete_btn.clicked.connect(self._on_preset_delete)
        preset_row.addWidget(self._preset_delete_btn)
        self._preset_update_btn = QPushButton()
        self._preset_update_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self._preset_update_btn.setToolTip("Overwrite the selected preset with current values")
        self._preset_update_btn.setFixedWidth(28)
        self._preset_update_btn.setFlat(True)
        self._preset_update_btn.clicked.connect(self._on_preset_update)
        preset_row.addWidget(self._preset_update_btn)
        self._preset_row_widget = QWidget()
        self._preset_row_widget.setLayout(preset_row)
        outer.addWidget(self._preset_row_widget)
        self._refresh_preset_combo()

        self._tab_widget = QTabWidget()
        outer.addWidget(self._tab_widget)

        self._empty_label = QLabel(
            "No customizable parameters found.\n\n"
            "Add top-level variable assignments\n"
            "with simple values to see them here.\n\n"
            "Example:\n"
            "  // Wall thickness\n"
            "  thickness = 3; // [1:10]"
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setEnabled(False)
        outer.addWidget(self._empty_label)

        outer.addWidget(add_row_widget)

        self._tab_widget.hide()
        self._preset_row_widget.hide()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_file_path(self, path: Optional[str]):
        """Tells the pane which .scad file is active, so presets can be
        loaded from/saved to its <name>.json sidecar. Called whenever the
        active tab or its saved path changes; None (unsaved buffer) just
        disables preset save/update/delete."""
        if path == self._file_path:
            return
        self._file_path = path
        self._presets = load_presets(preset_path_for(path)) if path else {}
        self._refresh_preset_combo()

    def set_source(self, source: str):
        """Re-scan *source* and refresh widgets.  Called on every editor change."""
        if source == self._source:
            return
        self._source = source
        new_params = scan_parameters(source)

        if self._structurally_equal(new_params):
            # Fast path: same parameters, only values may differ
            self._updating = True
            for p in new_params:
                for w in self._widgets.get(p.name, ()):
                    w.set_value(p.default)
            self._updating = False
            self._params = new_params
        else:
            self._params = new_params
            self._rebuild_ui()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _structurally_equal(self, new_params: list[ParameterDef]) -> bool:
        if len(new_params) != len(self._params):
            return False
        return all(
            a.name == b.name and a.tab == b.tab
            and a.description == b.description and a.constraint == b.constraint
            for a, b in zip(new_params, self._params)
        )

    def _rebuild_ui(self):
        self._widgets.clear()
        self._tab_widget.clear()

        if not self._params:
            self._tab_widget.hide()
            self._preset_row_widget.hide()
            self._empty_label.show()
            self._update_preset_buttons()
            return

        self._empty_label.hide()
        self._tab_widget.show()
        self._preset_row_widget.show()
        self._update_preset_buttons()

        tabs: dict[str, list[ParameterDef]] = {}
        for p in self._params:
            tabs.setdefault(p.tab, []).append(p)

        # OpenSCAD Customizer convention: a "Global" group's parameters are
        # shown at the top of every other tab, and never get a tab of their
        # own. If Global is the only group present, it just falls back to
        # the default single tab.
        global_params = tabs.pop('Global', [])
        if global_params:
            if tabs:
                for params in tabs.values():
                    params[:0] = global_params
            else:
                tabs['Parameters'] = global_params

        for tab_name, params in tabs.items():
            container = QWidget()
            form = QFormLayout(container)
            form.setContentsMargins(8, 8, 8, 8)
            form.setSpacing(8)
            form.setLabelAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

            for p in params:
                w = self._make_widget(p)
                if w is None:
                    continue
                self._widgets.setdefault(p.name, []).append(w)
                lbl = QLabel(p.description)
                lbl.setWordWrap(True)
                lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                lbl.customContextMenuRequested.connect(
                    lambda pos, lbl=lbl, name=p.name: self._on_param_context_menu(lbl, name, pos)
                )
                form.addRow(lbl, w)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(container)
            self._tab_widget.addTab(scroll, tab_name or 'Parameters')

        # Hide the tab bar when there is only one group
        self._tab_widget.tabBar().setVisible(self._tab_widget.count() > 1)

    def _make_widget(self, param: ParameterDef) -> Optional[QWidget]:
        spec = _parse_constraint(param.constraint, param.default)
        wtype = spec['type']
        val = param.default

        if isinstance(val, bool):
            w = _BoolWidget(val)
        elif isinstance(val, list):
            if wtype == 'slider':
                w = _VectorWidget(val, spec['min'], spec['max'], spec['step'])
            else:
                w = _VectorWidget(val)
        elif isinstance(val, str):
            if wtype == 'dropdown':
                w = _ComboWidget(spec['options'], val)
            else:
                w = _StringWidget(val, spec.get('maxlen', 0))
        elif isinstance(val, (int, float)):
            if wtype == 'slider':
                w = _SliderWidget(val, spec['min'], spec['max'], spec['step'])
            elif wtype == 'dropdown':
                w = _ComboWidget(spec['options'], val)
            else:
                w = _NumberWidget(val, _default_step(val))
        else:
            return None

        name = param.name
        w.value_changed.connect(lambda v, n=name: self._on_widget_changed(n, v))
        return w

    def _on_widget_changed(self, name: str, new_value: Any):
        if self._updating:
            return
        # A Global param may have one widget instance per tab it's mirrored
        # into (see _rebuild_ui) -- keep every copy in sync immediately,
        # not just the one the user actually edited.
        self._updating = True
        for w in self._widgets.get(name, ()):
            w.set_value(new_value)
        self._updating = False
        new_source = write_back_value(self._source, name, new_value)
        if new_source != self._source:
            self._source = new_source
            self.source_changed.emit(new_source)

    # ------------------------------------------------------------------
    # Parameter Editor (create/edit/delete parameter *definitions*)
    # ------------------------------------------------------------------

    def _current_tab_name(self) -> str:
        idx = self._tab_widget.currentIndex()
        if idx < 0:
            return 'Parameters'
        return self._tab_widget.tabText(idx) or 'Parameters'

    def _apply_new_source(self, new_source: str):
        if new_source == self._source:
            return
        self._source = new_source
        new_params = scan_parameters(new_source)
        self._params = new_params
        self._rebuild_ui()
        self.source_changed.emit(new_source)

    def _on_add_parameter(self):
        from belfryscad.window.customizer_param_dialog import ParameterEditorDialog

        tabs = list(dict.fromkeys(p.tab for p in self._params))
        dlg = ParameterEditorDialog(
            existing_tabs=tabs, existing_names=[p.name for p in self._params],
            default_tab=self._current_tab_name(), parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        result = dlg.result_values()
        if result is None:
            return
        name, default, description, tab, constraint = result
        new_source = insert_parameter(self._source, name, default, description, tab, constraint)
        self._apply_new_source(new_source)

    def _on_param_context_menu(self, label: QLabel, name: str, pos):
        menu = QMenu(self)
        menu.addAction("Edit Parameter…", lambda: self._on_edit_parameter(name))
        menu.addAction("Delete Parameter…", lambda: self._on_delete_parameter(name))
        menu.exec(label.mapToGlobal(pos))

    def _on_edit_parameter(self, name: str):
        from belfryscad.window.customizer_param_dialog import ParameterEditorDialog

        param = next((p for p in self._params if p.name == name), None)
        if param is None:
            return
        tabs = list(dict.fromkeys(p.tab for p in self._params))
        dlg = ParameterEditorDialog(
            existing_tabs=tabs, existing_names=[p.name for p in self._params],
            editing=param, parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        result = dlg.result_values()
        if result is None:
            return
        _name, default, description, _tab, constraint = result
        new_source = replace_parameter(self._source, name, default, description, constraint)
        self._apply_new_source(new_source)

    def _on_delete_parameter(self, name: str):
        reply = QMessageBox.question(
            self, "Delete Parameter", f"Delete parameter {name!r}? This removes it from the source.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        new_source = delete_parameter(self._source, name)
        self._apply_new_source(new_source)

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _current_values(self) -> dict[str, Any]:
        """Re-scans self._source (already up to date with any in-flight
        widget edit -- see _on_widget_changed) rather than trusting
        self._params, which a caller may not have refreshed yet."""
        return {p.name: p.default for p in scan_parameters(self._source)}

    def _refresh_preset_combo(self):
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_combo.addItem('(none)')
        for name in sorted(self._presets):
            self._preset_combo.addItem(name)
        self._preset_combo.setCurrentIndex(0)
        self._preset_combo.blockSignals(False)
        self._update_preset_buttons()

    def _update_preset_buttons(self):
        has_path = self._file_path is not None
        self._preset_save_as_btn.setEnabled(has_path and bool(self._params))
        is_real_selection = self._preset_combo.currentIndex() > 0
        self._preset_update_btn.setEnabled(has_path and is_real_selection)
        self._preset_delete_btn.setEnabled(has_path and is_real_selection)

    def _on_preset_selected(self, idx: int):
        self._update_preset_buttons()
        if idx <= 0:
            return
        values = self._presets.get(self._preset_combo.itemText(idx), {})
        self._apply_preset_values(values)

    def _apply_preset_values(self, values: dict[str, Any]):
        self._updating = True
        for name, val in values.items():
            for w in self._widgets.get(name, ()):
                w.set_value(val)
        self._updating = False
        new_source = self._source
        for name, val in values.items():
            new_source = write_back_value(new_source, name, val)
        if new_source != self._source:
            self._source = new_source
            self.source_changed.emit(new_source)

    def _on_preset_save_as(self):
        if not self._file_path:
            return
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        name = name.strip()
        if not ok or not name:
            return
        self._presets[name] = self._current_values()
        save_presets(preset_path_for(self._file_path), self._presets)
        self._refresh_preset_combo()
        idx = self._preset_combo.findText(name)
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)

    def _on_preset_update(self):
        idx = self._preset_combo.currentIndex()
        if idx <= 0 or not self._file_path:
            return
        name = self._preset_combo.itemText(idx)
        self._presets[name] = self._current_values()
        save_presets(preset_path_for(self._file_path), self._presets)

    def _on_preset_delete(self):
        idx = self._preset_combo.currentIndex()
        if idx <= 0 or not self._file_path:
            return
        name = self._preset_combo.itemText(idx)
        reply = QMessageBox.question(
            self, "Delete Preset", f"Delete preset {name!r}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._presets.pop(name, None)
        save_presets(preset_path_for(self._file_path), self._presets)
        self._refresh_preset_combo()
