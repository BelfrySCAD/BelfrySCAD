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

import os
from pathlib import Path
import threading
import time
from html import escape

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from belfryscad.window.ai_providers import (
    PRESETS, PROVIDERS, ChatMessage, ToolCall, base_url_key, model_key,
    preset_for,
)
from belfryscad.window.ai_cli import ClaudeCliSession, find_claude_cli
from belfryscad.window.ai_copilot_cli import CopilotCliSession, find_copilot_cli
from belfryscad.window.ai_mcp import McpToolServer
from belfryscad.window.ai_secrets import get_api_key
from belfryscad.window.ai_tools import (
    MODE_ACCEPT, MODE_AUTO, MODE_LABELS, MODE_MANUAL, MODE_PLAN, MODES,
    TRIGGER_RENDER,
    SYSTEM_PROMPT, TOOLS, AIToolContext, Followup, Proposal, ToolImage,
    run_tool,
)
from belfryscad.window.console import ConsoleWidget


def escape_md(text: str) -> str:
    """Neutralise Markdown in our own status lines (file names can contain
    underscores and asterisks). Model output is deliberately NOT escaped --
    rendering its Markdown is the point."""
    out = []
    for ch in text:
        out.append("\\" + ch if ch in "\\`*_{}[]()#+-.!" else ch)
    return "".join(out)

# Stops a runaway model from looping on tools forever. Generous enough that
# a legitimate read-several-files-then-propose turn never hits it.
_MAX_TOOL_ROUNDS = 12

# What to show in the activity line while each tool runs -- the raw tool
# name is an implementation detail the user shouldn't have to read.
_TOOL_ACTIVITY = {
    "list_library_files": "Looking through your libraries",
    "read_library_file": "Reading a library file",
    "search_library": "Searching your libraries",
    "check_geometry": "Checking the geometry",
    "list_open_scripts": "Checking your open scripts",
    "read_open_script": "Reading your script",
    "propose_script_edit": "Preparing an edit",
    "propose_script_replace": "Preparing an edit",
    "propose_new_script": "Preparing a new script",
    "view_viewport": "Looking at the model",
    "describe_geometry": "Measuring the geometry",
    "read_console": "Reading the console",
    "render": "Rendering",
    "schedule_followup": "Scheduling a follow-up",
    "ask_user": "Asking you a question",
}

# A model can schedule itself, so cap how many turns can chain
# without the user saying anything -- an unattended loop against a
# paid API is the failure mode worth designing against. Reset by
# any message the user types.
_MAX_CHAINED_FOLLOWUPS = 5

# A render-triggered follow-up that never sees a render would sit
# there forever; give up rather than leave a zombie in the bar.
_RENDER_WAIT_TIMEOUT = 900

# Once the render lands, still pause before firing: it gives the viewport a
# moment to settle and, more importantly, leaves a visible window in which
# the user can cancel rather than the next turn starting the instant a
# render finishes.
_RENDER_SETTLE_DELAY = 5


class _AIWorker(QObject):
    """Runs one user turn -- including as many tool round trips as the model
    asks for -- on a background thread."""

    text_delta = Signal(str)
    thinking = Signal()              # waiting on the model, nothing to show yet
    tool_started = Signal(str)       # tool name; the pane maps it to a phrase
    proposal_ready = Signal(object)  # Proposal
    followup_ready = Signal(object)  # Followup, or None to cancel
    errored = Signal(str)
    done = Signal()

    def __init__(self, provider: str, base_url: str, api_key: str, model: str,
                 messages: list[ChatMessage], ctx: AIToolContext,
                 cancel: threading.Event, cli_session=None, user_text: str = "",
                 images: "list[tuple[str, str]] | None" = None):
        super().__init__()
        self._provider = provider
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._messages = list(messages)
        self._ctx = ctx
        self._cancel = cancel
        # When set, talk to the claude CLI coprocess instead of an HTTP
        # provider; it keeps its own conversation state, so only this
        # turn's text is sent.
        self._cli_session = cli_session
        self._user_text = user_text
        # Only the CLI transports need these separately; the HTTP path reads
        # them off the ChatMessage it was handed.
        self._images = list(images or [])

    @Slot()
    def run(self):
        try:
            self._run_loop()
        except Exception as e:  # noqa: BLE001 -- last resort; surfaced in the pane
            self.errored.emit(str(e))
        finally:
            self.done.emit()

    def _run_loop(self):
        if self._cli_session is not None:
            self._run_cli_turn()
            return
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
                if isinstance(result, ToolImage):
                    # Neither protocol accepts an image *inside* a tool
                    # result, so acknowledge the call as text and deliver
                    # the picture as the following user message.
                    self._messages.append(ChatMessage(
                        role="tool", text=f"{result.caption} (image attached below)",
                        tool_call_id=call.id, tool_name=call.name))
                    self._messages.append(ChatMessage(
                        role="user", text="",
                        images=[(result.data_b64, result.mime)]))
                else:
                    self._messages.append(ChatMessage(
                        role="tool", text=result,
                        tool_call_id=call.id, tool_name=call.name))
        self.errored.emit(
            f"Stopped after {_MAX_TOOL_ROUNDS} tool rounds without finishing.")

    def _run_cli_turn(self):
        """The claude-CLI transport runs its own tool loop (against our MCP
        server), so there's exactly one pass here and no tool dispatch --
        tool_running events are report-only, for the activity line."""
        self.thinking.emit()
        for ev in self._cli_session.send_turn(self._user_text, self._cancel,
                                               images=self._images):
            if self._cancel.is_set():
                return
            if ev.kind == "text_delta":
                self.text_delta.emit(ev.text)
            elif ev.kind == "tool_running":
                self.tool_started.emit(ev.text)
            elif ev.kind == "error":
                self.errored.emit(ev.error or "unknown error")
                return


def resolve_anthropic_transport() -> tuple[str, str]:
    """How to reach Claude, in the order the user asked for:

    1. ANTHROPIC_API_KEY in the environment -> direct HTTP with that key.
    2. a `claude` CLI on PATH -> coprocess, which carries its own
       subscription auth and needs no key at all.
    3. a key entered in Preferences (the OS keychain) -> direct HTTP.

    Returns (transport, api_key) where transport is "http", "cli" or
    "none"; api_key is empty for "cli".
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env_key:
        return "http", env_key
    if find_claude_cli():
        return "cli", ""
    stored = get_api_key("anthropic") or ""
    if stored:
        return "http", stored
    return "none", ""


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


# Text-ish files are inlined into the message rather than attached: no
# provider takes arbitrary bytes, but every one of them reads text. The
# extension decides how the fence is labelled, nothing more.
_TEXT_SUFFIXES = {
    ".scad": "openscad", ".txt": "", ".md": "markdown", ".csv": "csv",
    ".json": "json", ".log": "", ".py": "python", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".ini": "", ".cfg": "", ".sh": "bash",
    ".dat": "", ".xml": "xml", ".html": "html", ".css": "css", ".js": "javascript",
}
# A dropped file bigger than this is truncated rather than sent whole: a
# multi-megabyte log would swallow the context window and the answer with it.
_TEXT_MAX_CHARS = 60_000

# What a dropped or pasted image is re-encoded to. Anthropic resizes
# anything larger than this anyway, and an unscaled phone photo is several
# megabytes of request for no extra detail.
_IMAGE_MAX_EDGE = 1568


def _encode_image(img) -> "tuple[str, str] | None":
    """QImage -> (base64 png, mime), scaled down if oversized."""
    import base64
    from PySide6.QtCore import QBuffer
    if img.isNull():
        return None
    if max(img.width(), img.height()) > _IMAGE_MAX_EDGE:
        img = img.scaled(_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    # QBuffer() with no argument, using its own internal QByteArray. Passing
    # one in -- QBuffer(QByteArray()) -- hands it a temporary that Python
    # frees the moment the call returns, and the buffer then writes into
    # freed memory. That segfaults, though not reliably enough to notice by
    # hand: it survived every isolated try and only fell over in the test.
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        return None
    return base64.b64encode(buf.data().data()).decode("ascii"), "image/png"


class _ChatInput(QPlainTextEdit):
    """Chat entry box: Return sends, Shift+Return inserts a newline.

    Also takes images, by drop or paste. Without this the default handling
    inserts a file:// URL, which reads like an attachment and is nothing of
    the sort -- the model cannot open it.
    """

    images_added = Signal(list)   # [(name, b64, mime)]
    text_added = Signal(list)     # [(name, language, text)]

    def __init__(self, on_send, parent=None):
        super().__init__(parent)
        self._on_send = on_send
        self.setAcceptDrops(True)

    def _harvest(self, mime):
        """(images, texts) found in `mime`.

        images: (name, b64, mime-type)   texts: (name, language, content)
        """
        from PySide6.QtGui import QImage
        images, texts = [], []
        if mime.hasImage():
            # A drag straight from a browser or screenshot tool: pixels, no
            # file behind them.
            enc = _encode_image(QImage(mime.imageData()))
            if enc:
                images.append(("pasted image", *enc))
        for url in (mime.urls() if mime.hasUrls() else []):
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            img = QImage(str(path))
            if not img.isNull():
                enc = _encode_image(img)
                if enc:
                    images.append((path.name, *enc))
                continue
            lang = _TEXT_SUFFIXES.get(path.suffix.lower())
            if lang is None:
                continue        # binary, or unknown: left for the caller to report
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(body) > _TEXT_MAX_CHARS:
                body = body[:_TEXT_MAX_CHARS] + "\n… (truncated)"
            texts.append((path.name, lang, body))
        return images, texts

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasImage() or (md.hasUrls() and any(u.isLocalFile() for u in md.urls())):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasImage() or (md.hasUrls() and any(u.isLocalFile() for u in md.urls())):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def _take(self, mime) -> bool:
        images, texts = self._harvest(mime)
        if images:
            self.images_added.emit(images)
        if texts:
            self.text_added.emit(texts)
        return bool(images or texts)

    def dropEvent(self, event):
        if self._take(event.mimeData()):
            event.acceptProposedAction()
            return
        # Nothing we can send. Falls through to the default, which drops the
        # path in as text -- still the most useful thing to do with a file
        # the model cannot read.
        super().dropEvent(event)

    def insertFromMimeData(self, source):
        if not self._take(source):
            super().insertFromMimeData(source)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Fall through to the default newline insertion, but strip
                # the modifier: QPlainTextEdit ignores Shift+Return.
                event = QKeyEvent(event.type(), event.key(),
                                  Qt.KeyboardModifier.NoModifier, "\n")
            else:
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
        self._entries: list[tuple[str, str]] = []
        self._pending: list[Proposal] = []
        self._thread: QThread | None = None
        self._worker: _AIWorker | None = None
        self._cancel = threading.Event()
        self._streaming = False
        # An ask_user answer that arrived while a turn was still running.
        self._pending_user_text = ""
        # Images attached to the message being sent, handed to start_turn.
        self._pending_images: list[tuple[str, str]] = []
        self._inline_open = False
        self._reply_text = ""
        self._awaiting_first_token = False
        self._review_dialog: DiffReviewDialog | None = None
        self._followup: Followup | None = None
        self._followup_due = 0.0
        self._followup_chain = 0
        self._followup_released = False
        # When the last render finished, and when the current turn began.
        # A render-triggered follow-up compares the two: see _on_followup.
        self._last_render_at = 0.0
        self._turn_started_at = 0.0
        self._followup_timer = QTimer(self)
        self._followup_timer.setInterval(1000)
        self._followup_timer.timeout.connect(self._tick_followup)
        self._cli_session: ClaudeCliSession | None = None
        self._mcp_server: McpToolServer | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        self._provider = QComboBox()
        for _p in PRESETS:
            self._provider.addItem(_p.label, _p.id)
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        idx = self._provider.findData(s.value("ai/activeProvider", "openai"))
        self._provider.setCurrentIndex(idx if idx >= 0 else 0)
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        top.addWidget(self._provider)

        self._mode = QComboBox()
        for m in MODES:
            self._mode.addItem(MODE_LABELS[m], m)
        mi = self._mode.findData(s.value("ai/mode", MODE_MANUAL))
        self._mode.setCurrentIndex(mi if mi >= 0 else 1)
        self._mode.setToolTip(
            "Plan: describe changes only.\n"
            "Manual: review every change as a diff.\n"
            "Accept Edits: apply changes immediately.\n"
            "Auto: apply changes and let follow-ups keep looping.")
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(self._mode)

        top.addStretch()
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear_conversation)
        top.addWidget(self._clear_btn)
        layout.addLayout(top)

        self._transcript = QTextBrowser()
        self._transcript.setReadOnly(True)
        self._transcript.setOpenExternalLinks(True)
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

        # Pending follow-up: always visible and always cancellable, so a
        # model that schedules itself can never do so behind the user's back.
        self._followup_bar = QWidget()
        fu = QHBoxLayout(self._followup_bar)
        fu.setContentsMargins(0, 0, 0, 0)
        fu.setSpacing(6)
        self._followup_label = QLabel()
        self._followup_label.setWordWrap(True)
        fu.addWidget(self._followup_label, 1)
        cancel_fu = QPushButton("Cancel")
        cancel_fu.clicked.connect(lambda: self._cancel_followup(say=True))
        fu.addWidget(cancel_fu)
        self._followup_bar.hide()
        layout.addWidget(self._followup_bar)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        # Attachment bar: hidden until something is dropped or pasted.
        # Above the input so a chip cannot be mistaken for part of the
        # message being typed.
        self._attach_bar = QWidget()
        self._attach_row = QHBoxLayout(self._attach_bar)
        self._attach_row.setContentsMargins(0, 0, 0, 0)
        self._attach_row.setSpacing(4)
        self._attach_row.addStretch()
        self._attach_bar.hide()
        layout.addWidget(self._attach_bar)
        # [(name, kind, payload)] -- kind "image": (b64, mime); "text": (lang, body)
        self._attachments: list[tuple] = []

        self._input = _ChatInput(self._on_send)
        self._input.images_added.connect(self._on_images_added)
        self._input.text_added.connect(self._on_text_added)
        self._input.setPlaceholderText("Ask about your model…  (Return to send, Shift+Return for a new line)")
        self._input.setMaximumHeight(72)
        bottom.addWidget(self._input, 1)
        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        bottom.addWidget(self._send_btn)
        layout.addLayout(bottom)

    # -- transcript helpers -------------------------------------------------

    def _say(self, text: str, kind: str = "note"):
        self._entries.append((kind, text))
        self._render_transcript()

    def _render_transcript(self):
        """Rebuild the whole transcript as Markdown.

        Markdown can't be rendered a token at a time, so a reply streams in
        as plain text (see _append_inline) and only becomes formatted once
        it's complete -- or when anything else forces a rebuild, which is
        why the in-progress reply is included here too.
        """
        parts = []
        for kind, text in self._entries:
            if kind == "user":
                if parts:
                    # A paragraph holding only a non-breaking space: extra
                    # blank lines collapse in Markdown, so this is what
                    # actually opens up space before each new request.
                    parts.append(" ")
                parts.append(f"**You:** {text}")
            elif kind == "assistant":
                parts.append(text)
            else:
                parts.append(f"*{escape_md(text)}*")
        if self._reply_text:
            parts.append(self._reply_text)
        bar = self._transcript.verticalScrollBar()
        follow, parked = self._at_end(), bar.value()
        self._transcript.setMarkdown("\n\n".join(parts))
        self._inline_open = bool(self._reply_text)
        if follow:
            self._scroll_to_end()
        else:
            # setMarkdown rebuilds the document and resets the scrollbar to
            # 0. Entries are only ever appended, so earlier content keeps
            # its position and the old offset still points at whatever the
            # user was reading.
            bar.setValue(parked)

    def _at_end(self) -> bool:
        """Whether the transcript is scrolled to the bottom. A couple of
        pixels of slack because the scrollbar's maximum shifts as content
        is added and rounding can leave it a hair short."""
        bar = self._transcript.verticalScrollBar()
        return bar.value() >= bar.maximum() - 2

    def _scroll_to_end(self):
        bar = self._transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_inline(self, text: str):
        """Append a streamed chunk as plain text, without re-rendering the
        document -- doing a full Markdown pass per token would be far too
        slow. _render_transcript formats it properly once the reply ends."""
        # Only follow the output if the user was already at the bottom --
        # otherwise streaming text would drag them away from whatever they
        # scrolled back to read.
        follow = self._at_end()
        cursor = QTextCursor(self._transcript.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self._inline_open:
            cursor.insertBlock()
            self._inline_open = True
        cursor.insertText(text)
        if follow:
            self._scroll_to_end()

    # -- sending ------------------------------------------------------------

    # -- attachments --------------------------------------------------------

    def _on_images_added(self, items):
        for name, b64, mime in items:
            self._attachments.append((name, "image", (b64, mime)))
        self._refresh_attachments()

    def _on_text_added(self, items):
        for name, lang, body in items:
            self._attachments.append((name, "text", (lang, body)))
        self._refresh_attachments()

    def _refresh_attachments(self):
        while self._attach_row.count() > 1:      # keep the trailing stretch
            item = self._attach_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, (name, kind, _payload) in enumerate(self._attachments):
            chip = QPushButton(f"{'🖼' if kind == 'image' else '📄'} {name}  ✕")
            chip.setFlat(True)
            chip.setToolTip("Remove this attachment")
            chip.clicked.connect(lambda _c=False, n=i: self._drop_attachment(n))
            self._attach_row.insertWidget(i, chip)
        self._attach_bar.setVisible(bool(self._attachments))

    def _drop_attachment(self, i: int):
        if 0 <= i < len(self._attachments):
            del self._attachments[i]
        self._refresh_attachments()

    def _take_attachments(self):
        items, self._attachments = self._attachments, []
        self._refresh_attachments()
        return items

    # -- sending ------------------------------------------------------------

    def _on_send(self):
        if self._streaming:
            self._cancel_turn()
            return
        text = self._input.toPlainText().strip()
        if not text and not self._attachments:
            return
        self._input.clear()
        self._followup_chain = 0     # user is engaged again
        shown, sent, images = self._compose(text, self._take_attachments())
        self._pending_images = images
        # The transcript gets the summary, the model the whole thing: a 60k
        # file pasted into the log would bury the conversation it belongs to.
        self._say(shown, kind="user")
        # Sending is an explicit "I'm done reading back there" -- jump to the
        # new message even if they'd scrolled up, unlike arriving output,
        # which only follows when already at the bottom.
        self._scroll_to_end()
        self.send_requested.emit(sent)

    @staticmethod
    def _compose(text: str, attachments: list):
        """(transcript text, text for the model, images).

        Text files are inlined into the message rather than attached. No
        provider takes arbitrary bytes, but all of them read text, so this
        works the same on every transport instead of needing one path each.
        """
        images = [payload for _n, kind, payload in attachments if kind == "image"]
        files = [(n, *payload) for n, kind, payload in attachments if kind == "text"]

        shown = [text] if text else []
        for name, _lang, body in files:
            shown.append(f"📄 {name} ({len(body):,} chars)")
        for name, _p in [(n, p) for n, k, p in attachments if k == "image"]:
            shown.append(f"🖼 {name}")

        sent = [text] if text else []
        for name, lang, body in files:
            sent.append(f"{name}:\n```{lang}\n{body}\n```")
        return "\n".join(shown), "\n\n".join(sent), images

    def start_turn(self, user_text: str, ctx: AIToolContext):
        """Called by MainWindow in response to send_requested, with a context
        snapshot it just built on the GUI thread."""
        if self._streaming:
            return
        self._turn_started_at = time.monotonic()
        preset = preset_for(self._provider.currentData())
        provider = preset.protocol
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        base_url = s.value(base_url_key(preset.id), preset.base_url)
        model = s.value(model_key(preset.id), "")
        cli_session = None
        if preset.id == "copilot":
            # CLI-only: Copilot authenticates through the CLI's own GitHub
            # login, so there is no key path to fall back to.
            if find_copilot_cli() is None:
                self._say("No GitHub Copilot CLI found. Install it with "
                          "`npm install -g @github/copilot`, then run "
                          "`copilot login`. If it is installed but not on "
                          "PATH — or `copilot` is AWS's tool of the same "
                          "name — point at it with Preferences → AI → "
                          "Copilot CLI.")
                return
            cli_session = self._ensure_copilot_session(ctx, model)
            if cli_session is None:
                return
        elif provider == "anthropic":
            transport, api_key = resolve_anthropic_transport()
            if transport == "none":
                self._say("No way to reach Claude: set ANTHROPIC_API_KEY, "
                          "enter an API key in Preferences → AI, or install "
                          "the `claude` CLI. If it is installed but not on "
                          "PATH, point at it with Preferences → AI → "
                          "Claude CLI.")
                return
            if transport == "cli":
                cli_session = self._ensure_cli_session(ctx, model)
                if cli_session is None:
                    return
            elif not model:
                self._say("No model set for this provider — see Preferences → AI.")
                return
        else:
            # Keys are stored per preset, so several services can be set up
            # at once and switching between them keeps each one's key. An
            # empty key is only fatal for Anthropic: local OpenAI-protocol
            # servers (Ollama, LM Studio, llama.cpp) accept requests with no
            # Authorization header at all, and a hosted service answers a
            # keyless request with a 401 we surface verbatim.
            api_key = get_api_key(preset.id) or ""
            if not base_url:
                self._say("No Base URL set for this service — see Preferences → AI.")
                return
            if not model:
                self._say("No model set for this provider — see Preferences → AI.")
                return

        images, self._pending_images = getattr(self, "_pending_images", []), []
        self._messages.append(ChatMessage(role="user", text=user_text, images=images))
        # `images` is used again below, for the CLI transports -- they are
        # handed the turn's text directly and never see self._messages.
        ctx.on_proposal = self._on_worker_proposal
        ctx.on_followup = self._on_worker_followup
        ctx.mode = self.mode()
        if self._mcp_server is not None:
            self._mcp_server.context = ctx   # fresh snapshot for CLI tool calls
        self._cancel = threading.Event()
        self._set_streaming(True)
        self._inline_open = False  # first delta opens its own line

        worker = _AIWorker(provider, base_url, api_key, model,
                           self._messages, ctx, self._cancel,
                           cli_session=cli_session, user_text=user_text,
                           images=images)
        thread = QThread()
        worker.moveToThread(thread)
        worker.text_delta.connect(self._on_text_delta, Qt.ConnectionType.QueuedConnection)
        worker.thinking.connect(self._on_thinking, Qt.ConnectionType.QueuedConnection)
        worker.tool_started.connect(self._on_tool_started, Qt.ConnectionType.QueuedConnection)
        worker.proposal_ready.connect(self._on_proposal, Qt.ConnectionType.QueuedConnection)
        worker.followup_ready.connect(self._on_followup, Qt.ConnectionType.QueuedConnection)
        worker.errored.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)
        self._thread, self._worker = thread, worker
        self._reply_text = ""
        thread.start()

    def _ensure_copilot_session(self, ctx, model: str):
        """Same shape as _ensure_cli_session, for Copilot.

        The session object is reused across turns even though each turn is
        its own process -- it holds the session id that chains them.
        """
        if self._cli_session is not None:
            return self._cli_session
        try:
            if self._mcp_server is None:
                self._mcp_server = McpToolServer()
                self._mcp_server.start()
            self._mcp_server.context = ctx
            self._cli_session = CopilotCliSession(
                SYSTEM_PROMPT, self._mcp_server.url, model=model)
        except OSError as e:
            self._say(f"Could not start the copilot CLI bridge: {e}")
            self._cli_session = None
            return None
        self._say("(using the GitHub Copilot CLI — no API key needed)")
        return self._cli_session

    def _ensure_cli_session(self, ctx, model: str):
        """Spin up (once per conversation) the MCP tool server and the
        `claude` coprocess. The CLI keeps its own conversation state, so
        the session is reused across turns and only torn down on Clear."""
        if self._cli_session is not None:
            return self._cli_session
        try:
            if self._mcp_server is None:
                self._mcp_server = McpToolServer()
                self._mcp_server.start()
            self._mcp_server.context = ctx
            self._cli_session = ClaudeCliSession(
                SYSTEM_PROMPT, self._mcp_server.url, model=model)
        except OSError as e:
            self._say(f"Could not start the claude CLI bridge: {e}")
            self._cli_session = None
            return None
        self._say("(using the claude CLI — no API key needed)")
        return self._cli_session

    def _stop_cli_session(self):
        if self._cli_session is not None:
            self._cli_session.stop()
            self._cli_session = None

    def _cancel_turn(self):
        self._cancel.set()
        self._say("(cancelled)")

    def cancel_turn(self):
        """Stop the running turn, if any. For the ask_user dialog: dismissing
        the question cancels the work that asked it, rather than leaving the
        model to carry on with the guess it was trying to avoid."""
        if self._streaming:
            self._cancel_turn()

    def submit_user_text(self, text: str):
        """Send `text` as though the user had typed it and pressed Send.

        Deferred while a turn is running -- the ask_user answer usually
        arrives after the model has stopped, but nothing guarantees it, and
        start_turn refuses a second turn outright. Held and flushed when the
        current one ends, the same way a due follow-up waits its turn.
        """
        text = (text or "").strip()
        if not text:
            return
        if self._streaming:
            self._pending_user_text = text
            return
        self._followup_chain = 0     # a real answer means the user is engaged
        self._say(text, kind="user")
        self._scroll_to_end()
        self.send_requested.emit(text)

    def _set_streaming(self, on: bool):
        self._streaming = on
        self._send_btn.setText("Stop" if on else "Send")
        if not on:
            self._set_status(None)
            # Flush an answer that arrived mid-turn. Queued rather than sent
            # inline: this runs while the finishing turn is still unwinding.
            pending, self._pending_user_text = self._pending_user_text, ""
            if pending:
                QTimer.singleShot(0, lambda t=pending: self.submit_user_text(t))

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

    def _on_worker_followup(self, followup):
        # Worker thread -- hop to the GUI thread via the signal.
        if self._worker is not None:
            self._worker.followup_ready.emit(followup)

    def _on_worker_proposal(self, proposal: Proposal):
        # Called on the worker thread -- hop to the GUI thread via the signal.
        if self._worker is not None:
            self._worker.proposal_ready.emit(proposal)

    @Slot(object)
    def _on_followup(self, followup):
        if followup is None:
            self._cancel_followup(say=True)
            return
        if (self.mode() != MODE_AUTO
                and self._followup_chain >= _MAX_CHAINED_FOLLOWUPS):
            self._say(f"Follow-up not scheduled: {_MAX_CHAINED_FOLLOWUPS} ran "
                      f"back to back already. Send a message to continue.")
            return
        self._followup = followup
        self._followup_released = False
        if followup.trigger == TRIGGER_RENDER:
            if self._last_render_at > self._turn_started_at:
                # A render already finished during THIS turn -- the one
                # reflecting the edit the model just applied, which is
                # exactly what it asked to be woken for. Release now rather
                # than wait for another that nothing will trigger.
                #
                # The common case in Accept Edits/Auto mode, not an edge
                # case: applying an edit starts a render that finishes in
                # milliseconds, while the schedule_followup tool call only
                # arrives after a round-trip to the model. Waiting would sit
                # at "After the next render" for the full 900s backstop and
                # look exactly like the follow-up never firing.
                self._followup_released = True
                self._followup_due = time.monotonic() + _RENDER_SETTLE_DELAY
            else:
                # No countdown -- on_render_finished() releases it. The
                # deadline is only a backstop against waiting forever for a
                # render that never comes.
                self._followup_due = time.monotonic() + _RENDER_WAIT_TIMEOUT
        else:
            self._followup_due = time.monotonic() + followup.delay_s
        self._tick_followup()
        self._followup_bar.show()
        self._followup_timer.start()

    def _tick_followup(self):
        fu = self._followup
        if fu is None:
            return
        left = self._followup_due - time.monotonic()

        if fu.trigger == TRIGGER_RENDER:
            if not self._followup_released:
                # Still waiting for a render; the deadline is only a backstop.
                if left > 0:
                    self._followup_label.setText(
                        f"After the next render: {fu.prompt}")
                    return
                self._say("Follow-up dropped: no render happened in time.")
                self._cancel_followup()
                return
            if left > 0:
                # Released, now serving the settle delay.
                self._followup_label.setText(
                    f"Render finished; following up in {int(left) + 1}s: "
                    f"{fu.prompt}")
                return
        elif left > 0:
            self._followup_label.setText(
                f"Follow-up in {int(left)}s: {fu.prompt}")
            return

        # Due. Don't interrupt a turn already running -- wait for the next tick.
        if self._streaming:
            return
        followup, self._followup = self._followup, None
        self._followup_timer.stop()
        self._followup_bar.hide()
        self._followup_chain += 1
        self._say(followup.prompt, kind="user")
        self.send_requested.emit(followup.prompt)

    def on_render_finished(self):
        """Called by MainWindow when a render completes. Releases a
        follow-up that was waiting for one -- by which point the viewport
        image and the geometry measurements reflect the new model."""
        # Recorded unconditionally: a follow-up scheduled LATER in the same
        # turn needs to know this render already happened (see _on_followup).
        self._last_render_at = time.monotonic()
        fu = self._followup
        if (fu is None or fu.trigger != TRIGGER_RENDER
                or self._followup_released):
            return
        self._followup_released = True
        self._followup_due = time.monotonic() + _RENDER_SETTLE_DELAY
        self._tick_followup()      # show the countdown straight away

    def _cancel_followup(self, say: bool = False):
        had = self._followup is not None
        self._followup = None
        self._followup_released = False
        self._followup_timer.stop()
        self._followup_bar.hide()
        if had and say:
            self._say("Follow-up cancelled.")

    def mode(self) -> str:
        return self._mode.currentData() or MODE_MANUAL

    def _on_mode_changed(self):
        QSettings("BelfrySCAD", "BelfrySCAD").setValue("ai/mode", self.mode())
        self._say(f"Mode: {MODE_LABELS[self.mode()]}.")

    @Slot(object)
    def _on_proposal(self, proposal: Proposal):
        target = proposal.filename or "script"
        if self.mode() in (MODE_ACCEPT, MODE_AUTO):
            # Applied straight away, but still through the editor's normal
            # edit path -- so Undo takes it back like any other change.
            self._say(f"Applied: {proposal.summary} ({target})")
            self.proposal_accepted.emit(proposal)
            return
        # One transcript line only -- the diff itself goes in the review
        # dialog, not into the conversation.
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
            # Promote the streamed plain text to a real entry, then rebuild
            # so it finally renders as Markdown.
            text, self._reply_text = self._reply_text, ""
            self._entries.append(("assistant", text))
            self._render_transcript()
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
        # The CLI holds the conversation, so clearing here means ending it.
        self._stop_cli_session()
        self._cancel_followup()
        self._followup_chain = 0
        self._messages.clear()
        self._entries.clear()
        self._reply_text = ""
        self._pending.clear()
        self._refresh_review_bar()
        self._transcript.clear()
