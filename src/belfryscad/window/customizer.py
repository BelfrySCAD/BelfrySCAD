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
"""

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSlider,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
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

def _parse_literal(s: str) -> Optional[Any]:
    s = s.strip()
    if s == 'true':
        return True
    if s == 'false':
        return False
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    try:
        if '.' in s or ('e' in s.lower() and re.match(r'^-?[\d]', s)):
            return float(s)
        return int(s)
    except ValueError:
        pass
    m = re.match(r'^\[([^\[\]]*)\]$', s)
    if m:
        inner = m.group(1).strip()
        if inner:
            parts = [p.strip() for p in inner.split(',')]
            if 1 <= len(parts) <= 4:
                try:
                    return [float(p) if '.' in p else int(p) for p in parts]
                except ValueError:
                    pass
    return None


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        return '[' + ', '.join(_format_value(x) for x in v) + ']'
    if isinstance(v, float):
        s = f'{v:g}'
        if '.' not in s and 'e' not in s.lower():
            s += '.0'
        return s
    return str(v)


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

    def __init__(self, value: list, parent=None):
        super().__init__(parent)
        self._value = list(value)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._spins: list[QDoubleSpinBox] = []
        for i, v in enumerate(value):
            spin = QDoubleSpinBox()
            spin.setRange(-1e9, 1e9)
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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

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

        self._tab_widget.hide()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
            self._empty_label.show()
            return

        self._empty_label.hide()
        self._tab_widget.show()

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
