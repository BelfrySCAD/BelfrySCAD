from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QIntValidator, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from belfryscad.window.ui_colors import apply_themed_icon, on_appearance_change, themed_icon

_ICONS_DIR = Path(__file__).parent.parent / "resources" / "icons"


def _anim_icon(name: str) -> QIcon:
    path = _ICONS_DIR / f"anim-{name}.svg"
    return themed_icon(path) if path.exists() else QIcon()


def _set_anim_icon(target, name: str) -> None:
    """As _set_debug_icon, for the anim-* set -- the play/pause button
    swaps its icon at runtime."""
    path = _ICONS_DIR / f"anim-{name}.svg"
    if path.exists():
        apply_themed_icon(target, path)
    else:
        target.setIcon(QIcon())


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
    dump_finished = Signal(bool)    # True: a whole cycle was saved

    def __init__(self, parent=None):
        super().__init__(parent)
        self._steps = 20
        self._fps = 10
        self._step = 0
        self._playing = False
        self._dumping = False
        # The frame a dump started on, and ends on reaching again: 0 from
        # Play, wherever the animation was when the box is ticked mid-play.
        self._dump_start = 0
        self._dump_mid_play = False

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

        self._dump_check = QCheckBox("Dump Pictures")
        self._dump_check.toggled.connect(self._on_dump_toggled)
        layout.addWidget(self._dump_check)

        transport = QHBoxLayout()
        self._btn_first = QPushButton()
        _set_anim_icon(self._btn_first, "first")
        self._btn_first.setToolTip("First Frame")
        self._btn_first.clicked.connect(self.go_first)

        self._btn_prev = QPushButton()
        _set_anim_icon(self._btn_prev, "prev")
        self._btn_prev.setToolTip("Previous Frame")
        self._btn_prev.clicked.connect(self.step_back)

        self._btn_play = QPushButton()
        _set_anim_icon(self._btn_play, "play")
        self._btn_play.setToolTip("Play")
        self._btn_play.clicked.connect(self.play)

        self._btn_pause = QPushButton()
        _set_anim_icon(self._btn_pause, "pause")
        self._btn_pause.setToolTip("Pause")
        self._btn_pause.clicked.connect(self.pause)

        self._btn_next = QPushButton()
        _set_anim_icon(self._btn_next, "next")
        self._btn_next.setToolTip("Next Frame")
        self._btn_next.clicked.connect(self.step_forward)

        self._btn_last = QPushButton()
        _set_anim_icon(self._btn_last, "last")
        self._btn_last.setToolTip("Last Frame")
        self._btn_last.clicked.connect(self.go_last)

        self._transport = (self._btn_first, self._btn_prev, self._btn_play,
                           self._btn_pause, self._btn_next, self._btn_last)
        for b in self._transport:
            b.setFixedSize(32, 32)
            transport.addWidget(b)
        # Play and Pause stay down to show which one is in effect: once a
        # big Play/Pause button above them did that, and read as a second
        # Play (#922).
        for b in (self._btn_play, self._btn_pause):
            b.setCheckable(True)
        self._style_transport()
        on_appearance_change(self, self._style_transport)
        layout.addLayout(transport)

        layout.addStretch()
        self._update_play_icons()

    def _style_transport(self):
        """Drawn by us, not the platform style: a flat checked button is
        drawn down in Fusion and on macOS, but Windows' own styles left it
        looking like the rest, so nothing said which of Play and Pause was
        in effect (#922)."""
        h = self.palette().color(QPalette.ColorRole.Highlight)
        rgb = f"{h.red()}, {h.green()}, {h.blue()}"
        css = ("QPushButton { border: 1px solid transparent; border-radius: 4px;"
               " background: transparent; }"
               f"QPushButton:hover {{ background: rgba({rgb}, 40); }}"
               "QPushButton:checked, QPushButton:pressed {"
               f" background: rgba({rgb}, 90); border-color: rgba({rgb}, 170); }}")
        for b in self._transport:
            b.setStyleSheet(css)

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
            self._update_play_icons()   # a click unchecks it; put it back
            return
        self._playing = True
        self._dumping = self._dump_check.isChecked()
        self._update_play_icons()
        if self._dumping:
            self._step = 0
            self._dump_start = 0
            self._dump_mid_play = False
            # Answered before the first frame is rendered: the host asks for
            # the folder here, and pauses if it is refused. Frame 0 used to
            # start first, finish while the folder dialog was still open,
            # and with nowhere to save it, never ask for frame 1 -- a dump
            # that did nothing and then reported itself complete (#922).
            self.dump_started.emit()
            if not self._playing:
                return
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

    def pause(self):
        if not self._playing:
            self._update_play_icons()
            return
        self._playing = False
        self._pending_advance = None
        self._timer.stop()
        was_dumping = self._dumping
        self._dumping = False
        self._update_play_icons()
        if was_dumping:
            self.dump_finished.emit(False)

    def cancel_dump(self):
        """Stop a dump that never started -- its folder was not given --
        without reporting it finished. One started from Play pauses; one
        started by ticking the box mid-play unticks it and plays on."""
        self._dumping = False
        if self._dump_mid_play:
            self._dump_mid_play = False
            self._dump_check.setChecked(False)
            self._timer.start()
        else:
            self.pause()

    def _on_dump_toggled(self, checked):
        """Ticking Dump Pictures while playing dumps one cycle from the
        current frame, as OpenSCAD does; unticking it mid-dump stops the
        dump and plays on (#938). While paused, the box is only read by
        play()."""
        if not self._playing:
            return
        if checked and not self._dumping:
            # Frames are paced by render completion from here on (see
            # play()), and the host asks for the folder first.
            self._timer.stop()
            self._pending_advance = None
            self._dumping = True
            self._dump_start = self._step
            self._dump_mid_play = True
            self.dump_started.emit()
            if self._dumping:
                self._try_render_now()
        elif not checked and self._dumping:
            self._dumping = False
            self.dump_finished.emit(False)
            self._timer.start()

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

        A dump paces itself by render completion (MainWindow calls
        advance_frame() directly from _on_render_thread_done, never via the
        timer), so while dumping the only request deferred here is its
        FIRST frame ("current"), started while some other render was still
        in flight.
        """
        if not self._playing or not self._pending_advance:
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
        if self._dumping and next_step == self._dump_start:
            # One full cycle is dumped, from wherever it started. As OpenSCAD does, untick
            # Dump Pictures and play on at the FPS set: stopping here left
            # Dump ticked, so every Play after it dumped one more cycle at
            # render speed and stopped again, which looked like an animation
            # that would no longer run (#922).
            self._dumping = False
            self._dump_check.setChecked(False)
            self.dump_finished.emit(True)
            self._timer.start()
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
        self._btn_play.setChecked(self._playing)
        self._btn_pause.setChecked(not self._playing)
