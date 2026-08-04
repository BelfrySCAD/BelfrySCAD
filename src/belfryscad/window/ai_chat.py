"""The AI chat dock pane: transcript, input box, and the review queue for
changes the model proposes.

Threading follows library_manager.py's worker pattern -- one _AIWorker per
user turn, moved to its own QThread, reporting back only through signals.
The worker runs the whole agentic loop (stream, run any tool calls, feed
results back, repeat) without touching a Qt widget: it operates on the
frozen AIToolContext snapshot MainWindow builds on the GUI thread before
each turn (see ai_tools' module docstring).
"""
from __future__ import annotations

import threading
import time
from html import escape

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from belfryscad.window.ai_providers import (
    PROVIDERS, ChatMessage, ToolCall,
)
from belfryscad.window.ai_secrets import get_api_key
from belfryscad.window.ai_tools import (
    SYSTEM_PROMPT, TOOLS, AIToolContext, Proposal, run_tool,
)
from belfryscad.window.console import ConsoleWidget

# Stops a runaway model from looping on tools forever. Generous enough that
# a legitimate read-several-files-then-propose turn never hits it.
_MAX_TOOL_ROUNDS = 12

# What to show in the activity line while each tool runs -- the raw tool
# name is an implementation detail the user shouldn't have to read.
_TOOL_ACTIVITY = {
    "list_library_files": "Looking through your libraries",
    "read_library_file": "Reading a library file",
    "list_open_scripts": "Checking your open scripts",
    "read_open_script": "Reading your script",
    "propose_script_edit": "Preparing an edit",
    "propose_new_script": "Preparing a new script",
}


class _AIWorker(QObject):
    """Runs one user turn -- including as many tool round trips as the model
    asks for -- on a background thread."""

    text_delta = Signal(str)
    thinking = Signal()              # waiting on the model, nothing to show yet
    tool_started = Signal(str)       # tool name; the pane maps it to a phrase
    proposal_ready = Signal(object)  # Proposal
    errored = Signal(str)
    done = Signal()

    def __init__(self, provider: str, base_url: str, api_key: str, model: str,
                 messages: list[ChatMessage], ctx: AIToolContext,
                 cancel: threading.Event):
        super().__init__()
        self._provider = provider
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._messages = list(messages)
        self._ctx = ctx
        self._cancel = cancel

    @Slot()
    def run(self):
        try:
            self._run_loop()
        except Exception as e:  # noqa: BLE001 -- last resort; surfaced in the pane
            self.errored.emit(str(e))
        finally:
            self.done.emit()

    def _run_loop(self):
        stream = PROVIDERS[self._provider]
        for _round in range(_MAX_TOOL_ROUNDS):
            if self._cancel.is_set():
                return
            assistant_text = ""
            pending: list[ToolCall] = []
            self.thinking.emit()
            for ev in stream(self._base_url, self._api_key, self._model,
                             self._messages, TOOLS, SYSTEM_PROMPT, self._cancel):
                if self._cancel.is_set():
                    return
                if ev.kind == "text_delta":
                    assistant_text += ev.text
                    self.text_delta.emit(ev.text)
                elif ev.kind == "tool_calls":
                    pending.extend(ev.tool_calls)
                elif ev.kind == "error":
                    self.errored.emit(ev.error or "unknown error")
                    return

            if not pending:
                return

            self._messages.append(ChatMessage(
                role="assistant", text=assistant_text, tool_calls=pending))
            for call in pending:
                if self._cancel.is_set():
                    return
                self.tool_started.emit(call.name)
                result = run_tool(self._ctx, call.name, call.arguments)
                self._messages.append(ChatMessage(
                    role="tool", text=result,
                    tool_call_id=call.id, tool_name=call.name))
        self.errored.emit(
            f"Stopped after {_MAX_TOOL_ROUNDS} tool rounds without finishing.")


def diff_to_html(diff_text: str) -> str:
    """Render a unified diff as colour-coded HTML.

    Tinted backgrounds rather than coloured text alone, so the added/removed
    distinction survives both light and dark palettes without querying the
    theme. Order matters: the "---"/"+++" file headers must be matched
    before the single-character "-"/"+" line tests, or they'd render as a
    giant deletion and addition.
    """
    rows = []
    for line in diff_text.splitlines():
        text = escape(line) or "&nbsp;"
        if line.startswith(("---", "+++")):
            style = "color:#57606a;"
        elif line.startswith("@@"):
            style = "color:#0969da;background:rgba(84,174,255,0.12);"
        elif line.startswith("+"):
            style = "color:#116329;background:rgba(46,160,67,0.20);"
        elif line.startswith("-"):
            style = "color:#82071e;background:rgba(207,34,46,0.20);"
        else:
            style = ""
        rows.append(f'<div style="white-space:pre;{style}">{text}</div>')
    return (f'<div style="font-family:Menlo,monospace;font-size:11pt;">'
            f'{"".join(rows)}</div>')


class DiffReviewDialog(QDialog):
    """Accept/Reject a single proposed change, showing its diff in colour.

    Non-modal on purpose: the worker may still be streaming (and may queue
    further proposals) while this is open, and this project has repeatedly
    hit Qt modality suppressing the main window's shortcuts -- see
    feedback_qt_modal_vs_menubar_shortcuts. Closing the window without
    choosing leaves the proposal pending rather than silently discarding it.
    """

    def __init__(self, proposal: Proposal, parent=None):
        super().__init__(parent)
        self.proposal = proposal
        self.result_choice: str | None = None   # "accept" | "reject" | None
        self.setWindowTitle("Review Proposed Change")
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        target = proposal.filename or "script"
        what = "New file" if proposal.kind == "new_file" else "Edit"
        header = QLabel(f"<b>{escape(proposal.summary)}</b><br>"
                        f"{what} — {escape(target)}")
        header.setWordWrap(True)
        layout.addWidget(header)

        view = QTextBrowser()
        view.setHtml(diff_to_html(proposal.diff_text))
        view.setLineWrapMode(QTextBrowser.LineWrapMode.NoWrap)
        layout.addWidget(view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        reject = QPushButton("Reject")
        reject.clicked.connect(self._reject)
        buttons.addWidget(reject)
        accept = QPushButton("Accept")
        accept.setDefault(True)
        accept.clicked.connect(self._accept)
        buttons.addWidget(accept)
        layout.addLayout(buttons)

    def _accept(self):
        self.result_choice = "accept"
        self.close()

    def _reject(self):
        self.result_choice = "reject"
        self.close()


class _ChatInput(QPlainTextEdit):
    """Chat entry box: Cmd/Ctrl+Return sends, plain Return still newlines."""

    def __init__(self, on_send, parent=None):
        super().__init__(parent)
        self._on_send = on_send

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                         | Qt.KeyboardModifier.MetaModifier)):
            self._on_send()
            return
        super().keyPressEvent(event)


class AIChatPane(QWidget):
    """Chat transcript + input + the pending-proposal review bar.

    MainWindow owns the actual applying of an accepted proposal (this pane
    has no access to editor tabs); it just surfaces the decision.
    """

    proposal_accepted = Signal(object)   # Proposal
    proposal_rejected = Signal(object)
    # Emitted when the user sends a message; MainWindow answers by building a
    # fresh AIToolContext on the GUI thread and calling start_turn().
    send_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages: list[ChatMessage] = []
        self._pending: list[Proposal] = []
        self._thread: QThread | None = None
        self._worker: _AIWorker | None = None
        self._cancel = threading.Event()
        self._streaming = False
        self._inline_open = False
        self._reply_text = ""
        self._awaiting_first_token = False
        self._review_dialog: DiffReviewDialog | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        self._provider = QComboBox()
        self._provider.addItem("OpenAI-protocol", "openai")
        self._provider.addItem("Anthropic (Claude)", "anthropic")
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        idx = self._provider.findData(s.value("ai/activeProvider", "openai"))
        self._provider.setCurrentIndex(idx if idx >= 0 else 0)
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        top.addWidget(self._provider)
        top.addStretch()
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear_conversation)
        top.addWidget(self._clear_btn)
        layout.addLayout(top)

        self._transcript = ConsoleWidget()
        self._transcript.setReadOnly(True)
        self._transcript.setFont(QFont("Menlo", 11))
        layout.addWidget(self._transcript, 1)

        # Live activity line. A local model can sit silent for a minute or
        # more between a tool call and its first reply token, so without
        # this the pane looks hung -- and the elapsed count is the only
        # thing distinguishing "slow" from "stuck".
        self._status = QLabel()
        self._status.setEnabled(False)   # greyed: it's chrome, not content
        self._status.hide()
        layout.addWidget(self._status)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._tick_status)
        self._status_text = ""
        self._status_started = 0.0

        # Review bar: hidden until the model proposes something.
        self._review_bar = QWidget()
        review = QHBoxLayout(self._review_bar)
        review.setContentsMargins(0, 0, 0, 0)
        review.setSpacing(6)
        self._review_label = QLabel()
        self._review_label.setWordWrap(True)
        review.addWidget(self._review_label, 1)
        # Accept/Reject live in the review dialog, not here -- this bar only
        # appears if the user dismissed that dialog without deciding, as the
        # way back to it.
        self._review_btn = QPushButton("Review…")
        self._review_btn.clicked.connect(self._show_review_dialog)
        review.addWidget(self._review_btn)
        self._review_bar.hide()
        layout.addWidget(self._review_bar)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self._input = _ChatInput(self._on_send)
        self._input.setPlaceholderText("Ask about your model…  (Cmd+Return to send)")
        self._input.setMaximumHeight(72)
        bottom.addWidget(self._input, 1)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        bottom.addWidget(self._send_btn)
        layout.addLayout(bottom)

    # -- transcript helpers -------------------------------------------------

    def _say(self, text: str):
        self._transcript.append_output(text)
        self._inline_open = False

    def _append_inline(self, text: str):
        """Append without starting a new block -- ConsoleWidget.append_output
        always begins one, which would put every streamed token on its own
        line. The first delta after any non-streamed output (a tool line, a
        diff) does start a new block, so resumed text doesn't run onto the
        end of whatever preceded it."""
        if not getattr(self, "_inline_open", False):
            self._transcript.append_output("")
            self._inline_open = True
        cursor = QTextCursor(self._transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        bar = self._transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- sending ------------------------------------------------------------

    def _on_send(self):
        if self._streaming:
            self._cancel_turn()
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self._say(f"› {text}")
        self.send_requested.emit(text)

    def start_turn(self, user_text: str, ctx: AIToolContext):
        """Called by MainWindow in response to send_requested, with a context
        snapshot it just built on the GUI thread."""
        if self._streaming:
            return
        provider = self._provider.currentData()
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        if provider == "openai":
            base_url = s.value("ai/openaiBaseUrl", "https://api.openai.com/v1")
            model = s.value("ai/openaiModel", "")
        else:
            base_url = "https://api.anthropic.com/v1"
            model = s.value("ai/anthropicModel", "")
        # An empty key is only fatal for Anthropic. OpenAI-protocol servers
        # running locally (Ollama, LM Studio, llama.cpp) accept requests
        # with no Authorization header at all; hosted OpenAI will answer a
        # keyless request with a 401 we surface verbatim.
        api_key = get_api_key(provider) or ""
        if not api_key and provider == "anthropic":
            self._say("No API key set for this provider — see Preferences → AI.")
            return
        if not model:
            self._say("No model set for this provider — see Preferences → AI.")
            return

        self._messages.append(ChatMessage(role="user", text=user_text))
        ctx.on_proposal = self._on_worker_proposal
        self._cancel = threading.Event()
        self._set_streaming(True)
        self._inline_open = False  # first delta opens its own line

        worker = _AIWorker(provider, base_url, api_key, model,
                           self._messages, ctx, self._cancel)
        thread = QThread()
        worker.moveToThread(thread)
        worker.text_delta.connect(self._on_text_delta, Qt.ConnectionType.QueuedConnection)
        worker.thinking.connect(self._on_thinking, Qt.ConnectionType.QueuedConnection)
        worker.tool_started.connect(self._on_tool_started, Qt.ConnectionType.QueuedConnection)
        worker.proposal_ready.connect(self._on_proposal, Qt.ConnectionType.QueuedConnection)
        worker.errored.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)
        self._thread, self._worker = thread, worker
        self._reply_text = ""
        thread.start()

    def _cancel_turn(self):
        self._cancel.set()
        self._say("(cancelled)")

    def _set_streaming(self, on: bool):
        self._streaming = on
        self._send_btn.setText("Stop" if on else "Send")
        if not on:
            self._set_status(None)

    def _set_status(self, text: str | None):
        """Show `text` (plus a live elapsed count) in the activity line, or
        hide it entirely when None."""
        if text is None:
            self._status_timer.stop()
            self._status.hide()
            return
        self._status_text = text
        self._status_started = time.monotonic()
        self._tick_status()
        self._status.show()
        self._status_timer.start()

    def _tick_status(self):
        elapsed = int(time.monotonic() - self._status_started)
        self._status.setText(f"{self._status_text}… {elapsed}s")

    # -- worker signal handlers --------------------------------------------

    @Slot(str)
    def _on_text_delta(self, text: str):
        # First token of *this round* -- the model is no longer thinking,
        # it's answering, and the answer itself is now the progress
        # indicator. Tracked per round, not per turn: a turn can be text,
        # then a tool call, then more text.
        if self._awaiting_first_token:
            self._awaiting_first_token = False
            self._set_status(None)
        self._reply_text += text
        self._append_inline(text)

    @Slot(str)
    def _on_tool_started(self, name: str):
        self._set_status(_TOOL_ACTIVITY.get(name, "Working"))

    @Slot()
    def _on_thinking(self):
        self._awaiting_first_token = True
        self._set_status("Thinking")

    def _on_worker_proposal(self, proposal: Proposal):
        # Called on the worker thread -- hop to the GUI thread via the signal.
        if self._worker is not None:
            self._worker.proposal_ready.emit(proposal)

    @Slot(object)
    def _on_proposal(self, proposal: Proposal):
        # One transcript line only -- the diff itself goes in the review
        # dialog, not into the conversation.
        target = proposal.filename or "script"
        self._say(f"Proposed: {proposal.summary} ({target})")
        self._pending.append(proposal)
        self._refresh_review_bar()
        self._show_review_dialog()

    @Slot(str)
    def _on_error(self, message: str):
        self._say(f"Error: {message}")

    @Slot()
    def _on_done(self):
        if self._reply_text:
            self._messages.append(
                ChatMessage(role="assistant", text=self._reply_text))
        thread, worker = self._thread, self._worker
        self._thread = self._worker = None
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._set_streaming(False)

    # -- proposal review ----------------------------------------------------

    def _refresh_review_bar(self):
        if not self._pending:
            self._review_bar.hide()
            return
        p = self._pending[0]
        extra = f"  (+{len(self._pending) - 1} more)" if len(self._pending) > 1 else ""
        self._review_label.setText(f"{p.summary} — {p.filename or 'script'}{extra}")
        self._review_bar.show()

    def _show_review_dialog(self):
        """Open the review window for the oldest unresolved proposal. Only
        one at a time -- further proposals wait their turn in _pending and
        are shown as each is resolved."""
        if self._review_dialog is not None or not self._pending:
            return
        dlg = DiffReviewDialog(self._pending[0], self)
        self._review_dialog = dlg
        dlg.finished.connect(lambda _=0, d=dlg: self._on_review_closed(d))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_review_closed(self, dlg: "DiffReviewDialog"):
        self._review_dialog = None
        choice = dlg.result_choice
        if choice is None:
            # Dismissed without deciding -- keep it queued; the review bar's
            # Review… button reopens it.
            self._refresh_review_bar()
            return
        if dlg.proposal in self._pending:
            self._pending.remove(dlg.proposal)
        if choice == "accept":
            self.proposal_accepted.emit(dlg.proposal)
            self._say(f"Accepted: {dlg.proposal.summary}")
        else:
            self.proposal_rejected.emit(dlg.proposal)
            self._say(f"Rejected: {dlg.proposal.summary}")
        self._refresh_review_bar()
        self._show_review_dialog()   # next queued proposal, if any

    # -- misc ---------------------------------------------------------------

    def _on_provider_changed(self):
        QSettings("BelfrySCAD", "BelfrySCAD").setValue(
            "ai/activeProvider", self._provider.currentData())

    def _clear_conversation(self):
        if self._review_dialog is not None:
            self._review_dialog.close()   # its finished handler clears the ref
        self._messages.clear()
        self._pending.clear()
        self._refresh_review_bar()
        self._transcript.clear()
