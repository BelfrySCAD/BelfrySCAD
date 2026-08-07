"""The dialog behind the AI's ask_user tool.

One tab per question, so a multi-part ask reads as a sequence rather than a
wall. Left/Right moves between tabs, Up/Down within a question, Space
toggles, Escape cancels -- these arrive mid-conversation with the user's
hands already on the keyboard.

Two layouts per question. Ordinarily the options stack with their
descriptions indented underneath. When the options carry `detail` -- longer
markdown that would not fit under a button -- the options move to a narrow
column and the selected one's text fills a pane beside them, so every
choice stays visible while the user reads about any one of them.

Space and Escape come from Qt: QRadioButton and QCheckBox both toggle on
Space when focused, and QDialog rejects on Escape. The arrow keys needed
writing -- see _QuestionBlock.eventFilter.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QRadioButton, QScrollArea, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

# Refuse to render an ask larger than this. A model that asks twenty
# questions at once has misunderstood the tool, and a dialog that long is
# worse than no answer.
MAX_QUESTIONS = 6
MAX_OPTIONS = 8

# Indent for a description under its option, roughly the width of the
# indicator, so the text lines up with the label rather than the button.
_DESC_INDENT = 22


class _QuestionBlock(QWidget):
    """One question: its options, their descriptions, and a note field."""

    tab_step = Signal(int)   # Left/Right pressed on an option; move tabs by ±1

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.multi = bool(spec.get("multiSelect"))
        self._options = list(spec.get("options") or [])[:MAX_OPTIONS]
        self.buttons: list[QCheckBox | QRadioButton] = []
        # Long-form when any option carries detail: one long text beside the
        # options beats eight of them stacked underneath.
        self.detailed = any(isinstance(o, dict) and (o.get("detail") or "").strip()
                            for o in self._options)
        self.detail_pane: QTextBrowser | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        title = QLabel(spec.get("question") or "")
        title.setWordWrap(True)
        tf = title.font()
        tf.setBold(True)
        title.setFont(tf)
        outer.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(12)
        opts = QVBoxLayout()
        opts.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(not self.multi)

        for i, opt in enumerate(self._options):
            label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            detail = opt.get("detail", "") if isinstance(opt, dict) else ""

            btn = QCheckBox(label) if self.multi else QRadioButton(label)
            self._group.addButton(btn, i)
            self.buttons.append(btn)
            opts.addWidget(btn)
            # Filtered on the button itself: these sit inside a QScrollArea,
            # which eats arrow keys to scroll before they could be caught
            # further out, and an exclusive QButtonGroup consumes Left/Right
            # for its own navigation.
            btn.installEventFilter(self)

            if desc:
                # Its own label rather than part of the button's text: a
                # button does not word-wrap, so a long description ran off
                # the edge instead of flowing.
                d = QLabel(desc)
                d.setWordWrap(True)
                d.setEnabled(False)
                d.setContentsMargins(_DESC_INDENT, 0, 0, 4)
                opts.addWidget(d)

            if detail:
                btn.toggled.connect(
                    lambda on, md=detail: self._show_detail(md) if on else None)

        opts.addStretch()
        if self.detailed:
            holder = QWidget()
            holder.setLayout(opts)
            holder.setMinimumWidth(190)
            body.addWidget(holder, 0)
            pane = QTextBrowser()
            pane.setOpenExternalLinks(False)
            pane.setMinimumWidth(260)
            pane.setMarkdown(self._detail_for(0))
            self.detail_pane = pane
            body.addWidget(pane, 1)
        else:
            body.addLayout(opts, 1)
        outer.addLayout(body, 1)

        note = QLineEdit()
        note.setPlaceholderText("Anything to add? (optional)")
        note.installEventFilter(self)
        outer.addWidget(note)
        self._note = note

    def _detail_for(self, i: int) -> str:
        o = self._options[i] if i < len(self._options) else None
        return (o.get("detail", "") if isinstance(o, dict) else "") or ""

    def _show_detail(self, markdown: str):
        if self.detail_pane is not None:
            self.detail_pane.setMarkdown(markdown)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # Tabs, not options. An exclusive radio group would otherwise
            # take these to move between its own buttons -- which Up/Down
            # already does -- leaving the question navigable two ways and the
            # tabs by none. The note field is exempt: Left/Right there is
            # ordinary text editing.
            if obj is self._note:
                return super().eventFilter(obj, event)
            self.tab_step.emit(-1 if key == Qt.Key.Key_Left else 1)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and obj in self.buttons and self.multi:
            # Exclusive groups get this from Qt; a checkbox list does not.
            i = self.buttons.index(obj)
            step = -1 if key == Qt.Key.Key_Up else 1
            self.buttons[(i + step) % len(self.buttons)].setFocus()
            return True
        return super().eventFilter(obj, event)

    def answer(self) -> dict:
        chosen = [self._options[i].get("label", "") if isinstance(self._options[i], dict)
                  else str(self._options[i])
                  for i, b in enumerate(self.buttons) if b.isChecked()]
        return {"selected": chosen, "note": self._note.text().strip()}


class AIQuestionDialog(QDialog):
    """Ask the user one or more questions. `answers` holds one entry per
    question, in order; `questions` the specs they came from."""

    def __init__(self, questions: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("The assistant has a question")
        # Not modal: answering may mean looking at the viewport or the
        # script the question is about, and a modal dialog would lock both
        # away. Nothing waits on this either -- the answer is delivered as a
        # new message when it comes.
        self.setModal(False)
        self._blocks: list[_QuestionBlock] = []
        self._questions = list(questions[:MAX_QUESTIONS])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        self._tabs = QTabWidget()
        for n, spec in enumerate(self._questions):
            block = _QuestionBlock(spec, self)
            block.tab_step.connect(self._step_tab)
            self._blocks.append(block)

            # Scrolled per page: one question with eight described options
            # can outgrow the screen, and a dialog taller than the display
            # cannot be dismissed on macOS -- its buttons end up off the
            # bottom edge.
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(block)
            area.setFrameShape(QFrame.Shape.NoFrame)
            self._tabs.addTab(area, self._tab_label(spec, n))
        # One question needs no tab bar, but still lives in the tab widget so
        # both cases take the same path from here on.
        self._tabs.tabBar().setVisible(len(self._blocks) > 1)
        outer.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        wide = any(b.detailed for b in self._blocks)
        self.resize(780 if wide else 560, 470)
        if self._blocks and self._blocks[0].buttons:
            self._blocks[0].buttons[0].setFocus()

    @staticmethod
    def _tab_label(spec: dict, n: int) -> str:
        header = (spec.get("header") or "").strip()
        if header:
            return header
        # No header given: a clipped question beats "Question 2", which says
        # nothing about which question it is.
        q = (spec.get("question") or "").strip()
        return (q[:18] + "…") if len(q) > 19 else (q or f"Question {n + 1}")

    def _step_tab(self, delta: int):
        n = self._tabs.count()
        if n < 2:
            return
        i = (self._tabs.currentIndex() + delta) % n
        self._tabs.setCurrentIndex(i)
        block = self._blocks[i]
        if block.buttons:
            # Focus follows the tab, or the next arrow key would act on the
            # page the user just left.
            block.buttons[0].setFocus()

    @property
    def questions(self) -> list[dict]:
        return self._questions

    @property
    def answers(self) -> list[dict]:
        return [b.answer() for b in self._blocks]
