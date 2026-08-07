"""The dialog behind the AI's ask_user tool.

One page, every question on it, so the user sees the whole ask before
answering any of it -- a wizard would hide question 2 while they decide
question 1, and the questions an assistant asks together are usually
related.

Keyboard first: the AI asks these mid-conversation, when the user's hands
are on the keyboard. Up/Down moves within a question, Tab moves between
them, Space toggles, Escape cancels. Nothing here needs the mouse.

Space and Escape come from Qt: QRadioButton and QCheckBox both toggle on
Space when focused, and QDialog rejects on Escape. Only Up/Down between
checkboxes needed writing -- see _QuestionBlock.eventFilter.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFrame, QLabel,
    QLineEdit, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

# Refuse to render an ask larger than this. A model that asks twenty
# questions at once has misunderstood the tool, and a dialog that long is
# worse than no answer.
MAX_QUESTIONS = 6
MAX_OPTIONS = 8


class _QuestionBlock(QWidget):
    """One question: its text, its options, and a clarification field."""

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.multi = bool(spec.get("multiSelect"))
        self._options = list(spec.get("options") or [])[:MAX_OPTIONS]
        self.buttons: list[QCheckBox | QRadioButton] = []

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        header = spec.get("header") or ""
        if header:
            chip = QLabel(header.upper())
            f = chip.font()
            f.setPointSizeF(max(8.0, f.pointSizeF() - 2))
            f.setBold(True)
            chip.setFont(f)
            chip.setEnabled(False)
            box.addWidget(chip)

        title = QLabel(spec.get("question") or "")
        title.setWordWrap(True)
        tf = title.font()
        tf.setBold(True)
        title.setFont(tf)
        box.addWidget(title)

        # A radio group is exclusive; checkboxes are not. Qt gives exclusivity
        # through QButtonGroup, which also makes arrow-key navigation work
        # inside the group for free.
        self._group = QButtonGroup(self)
        self._group.setExclusive(not self.multi)

        for i, opt in enumerate(self._options):
            btn = QCheckBox() if self.multi else QRadioButton()
            label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            btn.setText(f"{label}  —  {desc}" if desc else label)
            btn.setToolTip(desc)
            self._group.addButton(btn, i)
            self.buttons.append(btn)
            box.addWidget(btn)
            if self.multi:
                # An exclusive group gets arrow navigation from Qt. A
                # checkbox list does not, and the arrows cannot be picked up
                # further out either: these sit in a QScrollArea, which eats
                # Up/Down to scroll before they reach the dialog. Filtering
                # on the button itself is the only place ahead of it.
                btn.installEventFilter(self)

        note = QLineEdit()
        note.setPlaceholderText("Anything to add? (optional)")
        box.addWidget(note)
        self._note = note

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.KeyPress
                and obj in self.buttons
                and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            i = self.buttons.index(obj)
            step = -1 if event.key() == Qt.Key.Key_Up else 1
            self.buttons[(i + step) % len(self.buttons)].setFocus()
            return True
        return super().eventFilter(obj, event)

    def answer(self) -> dict:
        chosen = [self._options[i].get("label", "") if isinstance(self._options[i], dict)
                  else str(self._options[i])
                  for i, b in enumerate(self.buttons) if b.isChecked()]
        return {"selected": chosen, "note": self._note.text().strip()}


class AIQuestionDialog(QDialog):
    """Ask the user one or more questions. exec() returns Accepted/Rejected;
    `answers` holds one entry per question, in order."""

    def __init__(self, questions: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("The assistant has a question")
        self.setModal(True)
        self._blocks: list[_QuestionBlock] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # Scrolled: several questions with long option descriptions can
        # outgrow the screen, and a dialog taller than the display cannot be
        # dismissed on macOS -- its buttons end up off the bottom edge.
        inner = QWidget()
        stack = QVBoxLayout(inner)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(14)

        for n, spec in enumerate(questions[:MAX_QUESTIONS]):
            if n:
                rule = QFrame()
                rule.setFrameShape(QFrame.Shape.HLine)
                rule.setFrameShadow(QFrame.Shadow.Sunken)
                stack.addWidget(rule)
            block = _QuestionBlock(spec, self)
            self._blocks.append(block)
            stack.addWidget(block)
        stack.addStretch()

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        area.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(area, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        # Escape reaches this through QDialog's own handling; wiring rejected
        # as well keeps the button and the key on one path.
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self.resize(560, min(620, 180 + 150 * len(self._blocks)))
        if self._blocks and self._blocks[0].buttons:
            self._blocks[0].buttons[0].setFocus()

    @property
    def answers(self) -> list[dict]:
        return [b.answer() for b in self._blocks]
