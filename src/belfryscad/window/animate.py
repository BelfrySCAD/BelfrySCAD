from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"


def _anim_icon(name: str) -> QIcon:
    path = _ICONS_DIR / f"anim-{name}.svg"
    return QIcon(str(path)) if path.exists() else QIcon()


class AnimatePane(QWidget):
    """OpenSCAD-style animation controls.

    `$t` cycles through `step / steps` for `step` in `0..steps-1`, so it
    ranges over `[0, 1 - 1/steps)` and never reaches 1 (see
    https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Animation).

    `frame_changed` is emitted with the new `$t` whenever the current frame
    changes (playback tick, transport button, or manual Time edit) — the
    listener is expected to re-render with `$t` set accordingly.
    """

    frame_changed = Signal(float)
    dump_started = Signal()
    dump_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._steps = 20
        self._fps = 10
        self._step = 0
        self._playing = False
        self._dumping = False

        # Set by the host (MainWindow) to `lambda: bool(self._render_jobs)` --
        # lets a tick that lands while the previous frame's render is still
        # in flight DEFER rather than either overlap it (unsafe -- see
        # resume_deferred_advance's own doc comment) or skip it outright
        # (which would silently drop steps from the sequence). None means
        # "never busy" (e.g. in a host that doesn't wire this up).
        self.render_busy_check = None
        self._pending_advance = None  # None / "current" / "next" -- see _try_advance/_try_render_now

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self._setup_ui()
        self._update_timer_interval()
        self._update_time_display()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        form = QFormLayout()
        self._time_edit = QLineEdit()
        self._time_edit.editingFinished.connect(self._on_time_edited)
        form.addRow("Time:", self._time_edit)

        self._fps_edit = QLineEdit(str(self._fps))
        self._fps_edit.setValidator(QIntValidator(1, 1000, self))
        self._fps_edit.editingFinished.connect(self._on_fps_edited)
        form.addRow("FPS:", self._fps_edit)

        self._steps_edit = QLineEdit(str(self._steps))
        self._steps_edit.setValidator(QIntValidator(1, 1_000_000, self))
        self._steps_edit.editingFinished.connect(self._on_steps_edited)
        form.addRow("Steps:", self._steps_edit)

        # The main window binds Tab/Shift+Tab to Indent/Undent as
        # window-wide shortcuts, which otherwise steal Tab key presses
        # before these fields' normal focus-navigation gets a chance.
        for edit in (self._time_edit, self._fps_edit, self._steps_edit):
            edit.installEventFilter(self)

        layout.addLayout(form)

        play_row = QHBoxLayout()
        play_row.addStretch()
        self._btn_play_big = QPushButton()
        self._btn_play_big.setFixedSize(48, 48)
        self._btn_play_big.setToolTip("Play/Pause")
        self._btn_play_big.clicked.connect(self.toggle_play)
        play_row.addWidget(self._btn_play_big)
        play_row.addStretch()
        layout.addLayout(play_row)

        self._dump_check = QCheckBox("Dump Pictures")
        layout.addWidget(self._dump_check)

        transport = QHBoxLayout()
        self._btn_first = QPushButton()
        self._btn_first.setIcon(_anim_icon("first"))
        self._btn_first.setToolTip("First Frame")
        self._btn_first.clicked.connect(self.go_first)

        self._btn_prev = QPushButton()
        self._btn_prev.setIcon(_anim_icon("prev"))
        self._btn_prev.setToolTip("Previous Frame")
        self._btn_prev.clicked.connect(self.step_back)

        self._btn_play = QPushButton()
        self._btn_play.setIcon(_anim_icon("play"))
        self._btn_play.setToolTip("Play")
        self._btn_play.clicked.connect(self.play)

        self._btn_pause = QPushButton()
        self._btn_pause.setIcon(_anim_icon("pause"))
        self._btn_pause.setToolTip("Pause")
        self._btn_pause.clicked.connect(self.pause)

        self._btn_next = QPushButton()
        self._btn_next.setIcon(_anim_icon("next"))
        self._btn_next.setToolTip("Next Frame")
        self._btn_next.clicked.connect(self.step_forward)

        self._btn_last = QPushButton()
        self._btn_last.setIcon(_anim_icon("last"))
        self._btn_last.setToolTip("Last Frame")
        self._btn_last.clicked.connect(self.go_last)

        for b in (self._btn_first, self._btn_prev, self._btn_play,
                  self._btn_pause, self._btn_next, self._btn_last):
            b.setFixedSize(32, 32)
            b.setFlat(True)
            transport.addWidget(b)
        layout.addLayout(transport)

        layout.addStretch()
        self._update_play_icons()

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def current_t(self) -> float:
        return self._step / self._steps

    def is_playing(self) -> bool:
        return self._playing

    def is_dumping(self) -> bool:
        return self._dumping

    def dump_pictures_enabled(self) -> bool:
        return self._dump_check.isChecked()

    def current_step(self) -> int:
        return self._step

    def total_steps(self) -> int:
        return self._steps

    # ------------------------------------------------------------------
    # Transport controls
    # ------------------------------------------------------------------

    def play(self):
        if self._playing:
            return
        self._playing = True
        self._dumping = self._dump_check.isChecked()
        if self._dumping:
            self._step = 0
            self.dump_started.emit()
        self._update_play_icons()
        self._update_time_display()
        if not self._dumping:
            # While dumping, frames are paced by render completion (see
            # advance_frame()/MainWindow._on_render_done), not by the timer —
            # grabFramebuffer() can pump the event loop, and an overlapping
            # timer-driven render can reenter the GL context mid-grab.
            self._timer.start()
            # Route through the same busy-check as every later tick (see
            # _try_render_now's own doc comment) -- Play can land while a
            # render triggered by something else entirely (opening the file,
            # an edit) is still in flight.
            self._try_render_now()
        else:
            self.frame_changed.emit(self.current_t())

    def pause(self):
        if not self._playing:
            return
        self._playing = False
        self._pending_advance = None
        self._timer.stop()
        was_dumping = self._dumping
        self._dumping = False
        self._update_play_icons()
        if was_dumping:
            self.dump_finished.emit()

    def toggle_play(self):
        self.pause() if self._playing else self.play()

    def go_first(self):
        self.pause()
        self._step = 0
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    def go_last(self):
        self.pause()
        self._step = self._steps - 1
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    def step_forward(self):
        self.pause()
        self._step = (self._step + 1) % self._steps
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    def step_back(self):
        self.pause()
        self._step = (self._step - 1) % self._steps
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    # ------------------------------------------------------------------
    # Event filter
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        # Accept Tab/Backtab as a normal key (not a shortcut) so the
        # window's Indent/Undent actions don't consume it, letting
        # QLineEdit's default focus-navigation move between fields.
        if event.type() == QEvent.Type.ShortcutOverride:
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                event.accept()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_tick(self):
        self._try_advance()

    def _try_advance(self):
        """Advance to the next frame now, or defer it if the previous frame's
        render is still in flight.

        openscad_cpp_evaluator's parser isn't safe for concurrent invocation
        from separate threads (confirmed directly -- overlapping renders on
        the same file corrupt each other's parse and can segfault). A model
        slow enough to render across more than one tick at the configured
        FPS must not get a second render started on top of the first, but
        it also must not silently DROP that step from the sequence (unlike
        an earlier version of this fix, which skipped the tick outright) --
        every step 0..steps-1 still gets rendered, in order, just at
        whatever pace the model can actually sustain. See
        resume_deferred_advance's own doc comment for the other half of
        this mechanism.

        Never OVERWRITES an already-pending request: the timer keeps
        ticking at its own interval regardless of render state, so several
        ticks can land while the same render is still in flight. Once one
        of them has queued a request, later ones finding the identical
        busy render must leave it alone -- e.g. a "next" tick clobbering an
        earlier still-undelivered "current" request (queued by play(), see
        _try_render_now()) would skip the very first frame outright, never
        having actually rendered it.
        """
        if self.render_busy_check and self.render_busy_check():
            if self._pending_advance is None:
                self._pending_advance = "next"
            return
        self._pending_advance = None
        self.advance_frame()

    def _try_render_now(self):
        """Emit frame_changed for the CURRENT step, or defer it if a render
        is still in flight -- play()'s own counterpart to _try_advance()
        (which is for a later tick, and always steps forward first). Used
        for the very first frame of a playback run, which can otherwise
        start overlapping a render triggered by something else entirely
        (opening the file, an edit) that just hasn't finished yet.
        """
        if self.render_busy_check and self.render_busy_check():
            if self._pending_advance is None:
                self._pending_advance = "current"
            return
        self._pending_advance = None
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    def resume_deferred_advance(self):
        """Called by the host once an in-flight render has fully finished --
        performs whichever frame trigger (_try_advance/_try_render_now) got
        deferred while it was running, if any.

        Only relevant to ordinary (non-dumping) playback: dumping already
        paces itself by render completion (MainWindow calls advance_frame()
        directly from _on_render_thread_done, never via the timer -- see
        play()'s own `if not self._dumping: self._timer.start()`), so this
        is a no-op whenever _dumping is set.
        """
        if not self._playing or self._dumping or not self._pending_advance:
            return
        pending, self._pending_advance = self._pending_advance, None
        if pending == "next":
            self.advance_frame()
        else:
            self._update_time_display()
            self.frame_changed.emit(self.current_t())

    def advance_frame(self):
        """Move to the next frame and emit frame_changed.

        While dumping, called from MainWindow once the current frame's render
        (and PNG save) has completed, instead of from the timer.
        """
        next_step = (self._step + 1) % self._steps
        if self._dumping and next_step == 0:
            # One full cycle (0..steps-1) has been rendered/dumped — stop
            # before repeating frame 0.
            self.pause()
            return
        self._step = next_step
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    def _on_time_edited(self):
        try:
            t = float(self._time_edit.text())
        except ValueError:
            self._update_time_display()
            return
        t = max(0.0, min(t, 1.0 - 1.0 / self._steps))
        self.pause()
        self._step = int(round(t * self._steps)) % self._steps
        self._update_time_display()
        self.frame_changed.emit(self.current_t())

    def _on_fps_edited(self):
        try:
            self._fps = max(1, int(self._fps_edit.text()))
        except ValueError:
            pass
        self._fps_edit.setText(str(self._fps))
        self._update_timer_interval()

    def _on_steps_edited(self):
        try:
            self._steps = max(1, int(self._steps_edit.text()))
        except ValueError:
            pass
        self._steps_edit.setText(str(self._steps))
        if self._step >= self._steps:
            self._step = self._steps - 1
        self._update_time_display()

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _update_timer_interval(self):
        self._timer.setInterval(max(1, round(1000 / self._fps)))

    def _update_time_display(self):
        self._time_edit.setText(f"{self.current_t():.6f}")

    def _update_play_icons(self):
        icon = _anim_icon("pause") if self._playing else _anim_icon("play")
        self._btn_play_big.setIcon(icon)
