"""File > Play Demo Script...: types a script into the editor at a set
rate and drives the view, for live demos and screen recordings (#1082).

A demo script is plain text. A line starting with `@` is a directive; any
other line is typed into the editor, followed by a newline (`@@` types a
line that starts with `@`). A directive may end in a `//` comment.

    @new                     open a new tab to type into
    @rate 8                  characters per second (default 8)
    @speed 1.5               play everything after this 1.5x as fast
    @type TEXT               type TEXT exactly, with no newline after it
    @goto 3 5                move the cursor to line 3, column 5
    @delete 4                delete the 4 characters after the cursor
    @pause 3                 wait 3 seconds
    @pause key               wait for Space (or a presenter remote's "next")
    @preview / @render       F5 / F6, waiting until the result lands
    @view front              a View-menu view: top bottom left right front
                             back iso all center
    @rotate 30 20 over 1.5s  swing to azimuth 30, elevation 20
    @zoom 1.5 over 1s        zoom in 1.5x (below 1 zooms out)

During playback Space pauses and resumes wherever the demo is, Up and
Down speed it up and slow it down, and Esc stops it, keeping what it typed.

File > Record Demo Script writes one of these from what you do: see
DemoRecorder below.
"""

import re
import time

from PySide6.QtCore import QEasingCurve, QEvent, QObject, Qt, QTimer, QVariantAnimation
from PySide6.QtWidgets import QApplication

VIEWS = ("top", "bottom", "left", "right", "front", "back", "iso", "all", "center")
DEFAULT_RATE = 8.0
#: How long Viewport.animate_camera_to swings to a named view.
VIEW_MS = 350
_OVER = re.compile(r"^(.*?)(?:\s+over\s+([0-9.]+)\s*s?)?$")

#: Keys that pause/resume: Space, and what presenter remotes send for "next".
_GO_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_PageDown, Qt.Key.Key_Right)
#: Up/Down change the speed by this factor, within these limits.
SPEED_STEP = 1.25
SPEED_MIN, SPEED_MAX = 0.25, 4.0


class DemoScriptError(ValueError):
    pass


def parse(text: str) -> list[tuple]:
    """The script as steps, or DemoScriptError naming the first bad line."""
    steps = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("@type "):   # raw: no comment, no strip, no newline
            steps.append(("type", line[6:]))
            continue
        if line.startswith("@@"):
            steps.append(("type", line[1:] + "\n"))
            continue
        if not line.startswith("@"):
            steps.append(("type", line + "\n"))
            continue
        body, ms = _split_over(line[1:].split("//", 1)[0].strip())
        name, *args = body.split() or [""]
        try:
            steps.append(_directive(name.lower(), args, ms))
        except (ValueError, IndexError):
            raise DemoScriptError(f"Line {n}: can't read {line.strip()!r}") from None
    return steps


def _split_over(body):
    m = _OVER.match(body)
    return m.group(1), (int(float(m.group(2)) * 1000) if m.group(2) else None)


def _directive(name, args, ms):
    if name == "new" and not args:
        return ("new",)
    if name == "rate" and len(args) == 1 and float(args[0]) > 0:
        return ("rate", float(args[0]))
    if name == "speed" and len(args) == 1 and float(args[0]) > 0:
        return ("speed", float(args[0]))
    if name == "goto" and len(args) == 2 and int(args[0]) >= 1 and int(args[1]) >= 1:
        return ("goto", int(args[0]), int(args[1]))
    if name == "delete" and len(args) == 1 and int(args[0]) >= 1:
        return ("delete", int(args[0]))
    if name == "pause" and args == ["key"]:
        return ("pause", None)
    if name == "pause" and len(args) == 1 and float(args[0]) >= 0:
        return ("pause", float(args[0]))
    if name in ("preview", "render") and not args:
        return ("render", name == "preview")
    if name == "view" and len(args) == 1 and args[0].lower() in VIEWS:
        return ("view", args[0].lower())
    if name == "rotate" and len(args) == 2:
        return ("rotate", float(args[0]), float(args[1]), 1000 if ms is None else ms)
    if name == "zoom" and len(args) == 1 and float(args[0]) > 0:
        return ("zoom", float(args[0]), 500 if ms is None else ms)
    raise ValueError(name)


class DemoPlayer(QObject):
    """Plays parsed steps against a MainWindow. One step at a time, each
    scheduling the next on a single-shot timer, so pausing anywhere is
    stopping that timer (and any camera animation) and resuming it later."""

    def __init__(self, window, steps):
        super().__init__(window)
        self._win = window
        self._steps = list(steps)
        self._i = 0
        self._rate = DEFAULT_RATE
        self._speed = 1.0
        self._deleting = 0
        self._editor = window._current_editor()
        self._cursor = self._editor.textCursor() if self._editor else None
        self._typing = ""
        self._paused = False
        self._waiting_key = False
        self._anim = None
        self._rot = None
        self._stopped = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._remaining = None

    def start(self):
        QApplication.instance().installEventFilter(self)
        self._win.log("Playing demo: Space pauses and resumes, Up/Down change the speed, Esc stops.")
        self._schedule(0)

    def stop(self, finished=False):
        self._stopped = True
        self._rot = None
        self._timer.stop()
        if self._anim is not None:
            self._anim.stop()
        QApplication.instance().removeEventFilter(self)
        self._win.log("Demo finished." if finished else "Demo stopped.")
        if self._win._demo_player is self:
            self._win._demo_player = None

    # -- keys ------------------------------------------------------------

    def eventFilter(self, obj, event):
        # While a demo plays it owns the keyboard: a plain key either steers
        # it or is dropped, so a presenter remote's other buttons -- F5 for
        # "start slideshow" (Preview here), B or . for a black screen --
        # change nothing (#1082). Cmd/Ctrl/Alt shortcuts still work.
        t = event.type()
        if t not in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            return False
        if event.modifiers() & ~(Qt.KeyboardModifier.KeypadModifier | Qt.KeyboardModifier.ShiftModifier):
            return False
        if t == QEvent.Type.ShortcutOverride:
            event.accept()  # claimed, so no menu shortcut fires; the KeyPress follows
            return True
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.stop()
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.set_speed(self._speed * (SPEED_STEP if key == Qt.Key.Key_Up else 1 / SPEED_STEP))
            self._win.statusBar().showMessage(f"Demo speed {self._speed:g}x", 1500)
            return True
        if key in _GO_KEYS:
            if self._waiting_key:
                self._waiting_key = False
                self._schedule(0)
            else:
                self.toggle_pause()
        return True

    def toggle_pause(self):
        self._paused = not self._paused
        anim = self._anim or getattr(self._viewport(), "_view_anim", None)
        if self._paused:
            if self._timer.isActive():
                self._remaining = self._timer.remainingTime()
                self._timer.stop()
            if anim is not None:
                anim.pause()
        else:
            if anim is not None:
                anim.resume()
            if self._remaining is not None:
                self._schedule(self._remaining, scale=False)
                self._remaining = None

    def set_speed(self, speed):
        """Everything still to come runs `speed` times as fast, including
        what is left of the wait under way. ponytail: a camera move already
        in flight keeps its pace; the next one gets the new speed."""
        speed = min(SPEED_MAX, max(SPEED_MIN, round(speed, 3)))
        ratio, self._speed = self._speed / speed, speed
        if self._timer.isActive():
            self._timer.start(int(self._timer.remainingTime() * ratio))
        elif self._remaining is not None:
            self._remaining *= ratio

    # -- steps -----------------------------------------------------------

    def _viewport(self):
        return self._win._viewport

    def _schedule(self, ms, scale=True):
        if self._stopped:
            return                      # a late render or animation, after Esc
        if scale:
            ms /= self._speed
        if self._paused:
            self._remaining = ms
        else:
            self._timer.start(max(0, int(ms)))

    def _tick(self):
        if self._typing:
            self._type_one()
            return
        if self._deleting:
            self._delete_one()
            return
        if self._i >= len(self._steps):
            self.stop(finished=True)
            return
        step = self._steps[self._i]
        self._i += 1
        getattr(self, "_do_" + step[0])(*step[1:])

    def _type_one(self):
        ch, self._typing = self._typing[0], self._typing[1:]
        if self._cursor is not None:
            self._cursor.insertText(ch)
            self._editor.setTextCursor(self._cursor)
            self._editor.ensureCursorVisible()
        self._schedule(0 if ch == "\n" and not self._typing else 1000 / self._rate)

    def _do_type(self, text):
        self._typing = text
        if text:
            self._type_one()
        else:
            self._schedule(0)

    def _do_goto(self, line, col):
        block = self._editor.document().findBlockByNumber(line - 1)
        if not block.isValid():
            block = self._editor.document().lastBlock()
        self._cursor.setPosition(block.position())
        self._cursor.movePosition(self._cursor.MoveOperation.Right,
                                  n=min(col - 1, len(block.text())))
        self._editor.setTextCursor(self._cursor)
        self._schedule(1000 / self._rate)

    def _do_delete(self, n):
        self._deleting = n
        self._delete_one()

    def _delete_one(self):
        self._deleting -= 1
        self._cursor.deleteChar()
        self._editor.setTextCursor(self._cursor)
        self._schedule(500 / self._rate)

    def _do_speed(self, speed):
        self.set_speed(speed)
        self._schedule(0)

    def _do_new(self):
        self._win._new_document()
        self._editor = self._win._current_editor()
        self._cursor = self._editor.textCursor()
        self._schedule(0)

    def _do_rate(self, cps):
        self._rate = cps
        self._schedule(0)

    def _do_pause(self, seconds):
        if seconds is None:
            self._waiting_key = True
        else:
            self._schedule(seconds * 1000)

    def _do_render(self, preview):
        self._win._render(preview=preview)
        self._wait_render()

    def _wait_render(self):
        if self._win._render_busy():
            QTimer.singleShot(100, self._wait_render)
        else:
            self._schedule(0)

    def _do_view(self, preset):
        self._viewport().set_view_preset(preset)
        self._schedule(VIEW_MS if preset not in ("all", "center") else 0)

    def _do_rotate(self, az, el, ms):
        vp = self._viewport()
        vp.animate_camera_to(az, el, duration_ms=max(1, int(ms / self._speed)))
        self._rot = getattr(vp, "_view_anim", None)
        if self._rot is None:
            self._schedule(0)               # already there
        else:
            # On its last frame, not a timer's guess -- but a drag that
            # stops it early never says "finished", hence the watch.
            self._rot.finished.connect(self._rotate_done)
            QTimer.singleShot(int(ms / self._speed) + 300, self._rotate_watch)

    def _rotate_done(self):
        if self._rot is not None:
            self._rot = None
            self._schedule(0)

    def _rotate_watch(self):
        if self._rot is None:
            return
        if self._rot.state() == QVariantAnimation.State.Stopped:
            self._rotate_done()
        else:
            QTimer.singleShot(300, self._rotate_watch)

    def _do_zoom(self, factor, ms):
        vp = self._viewport()
        start = vp._renderer.camera.distance
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(start / factor)
        anim.setDuration(max(1, int(ms / self._speed)))
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.valueChanged.connect(vp.set_distance)
        anim.finished.connect(self._zoom_done)
        self._anim = anim
        anim.start()

    def _zoom_done(self):
        self._anim = None
        self._schedule(0)


# -- recording -------------------------------------------------------------

#: An idle gap at least this long becomes a @pause, rounded to half seconds.
PAUSE_MIN_S = 1.0
#: A camera that has not moved for this long has finished moving.
SETTLE_MS = 400


def _line_col(text, pos):
    before = text[:pos]
    return before.count("\n") + 1, pos - (before.rfind("\n") + 1) + 1


def _typed_lines(text):
    """`text` as script lines that type it: a whole line as itself (@@ when
    it starts with @), a last line with no newline as @type."""
    *lines, last = text.split("\n")
    out = ["@" + l if l.startswith("@") else l for l in lines]
    if last:
        out.append("@type " + last)
    return out


def text_steps(old, new, cursor):
    """Script lines turning `old` into `new` with the player's cursor at
    `cursor`, and where the cursor is after them. One edit: what lies
    between the common prefix and suffix is deleted and retyped."""
    p = 0
    limit = min(len(old), len(new))
    while p < limit and old[p] == new[p]:
        p += 1
    q = 0
    while q < limit - p and old[-1 - q] == new[-1 - q]:
        q += 1
    removed, added = old[p:len(old) - q], new[p:len(new) - q]
    out = []
    if not removed and not added:
        return out, cursor
    if p != cursor:
        out.append("@goto %d %d" % _line_col(old, p))
    if removed:
        out.append(f"@delete {len(removed)}")
    return out + _typed_lines(added), p + len(added)


class DemoRecorder(QObject):
    """File > Record Demo Script: writes a demo script from what you do.

    It records what you did, not when or where you clicked: the text typed
    (as a diff between moments, so the script types it at its own @rate),
    Preview and Render, named views, camera moves once they settle, and
    pauses of a second or more. It types into a tab of its own, which the
    script's @new recreates. Pans are not recorded; the player has none.
    """

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self._lines = ["@new", f"@rate {DEFAULT_RATE:g}   // recorded; edit freely"]
        tab = window._new_document()
        self._editor = tab.editor
        self._snap = self._last = ""
        self._cursor = 0
        self._idle_from = time.monotonic()
        self._editor.document().contentsChanged.connect(self._on_text)
        vp = window._viewport
        cam = vp._renderer.camera
        self._cam = (cam.azimuth, cam.elevation, cam.distance)
        self._moving_from = None
        self._absorb = False
        self._settle = QTimer(self, singleShot=True, interval=SETTLE_MS)
        self._settle.timeout.connect(self._camera_settled)
        vp.camera_changed.connect(self._on_camera)
        vp.view_preset_chosen.connect(self.note_view)
        window.log("Recording demo: choose File > Record Demo Script again to stop.")

    def finish(self) -> str:
        if self._settle.isActive():
            self._settle.stop()
            self._camera_settled()
        self._flush_text()
        self._editor.document().contentsChanged.disconnect(self._on_text)
        vp = self._win._viewport
        vp.camera_changed.disconnect(self._on_camera)
        vp.view_preset_chosen.disconnect(self.note_view)
        return "\n".join(self._lines) + "\n"

    # -- what happened ---------------------------------------------------

    def _on_text(self):
        if time.monotonic() - self._idle_from >= PAUSE_MIN_S:
            self._flush_text()          # the text before the pause
            self._pause()
        self._last = self._editor.toPlainText()
        self._idle_from = time.monotonic()

    def note_render(self, preview):
        self._event("@preview" if preview else "@render")

    def note_view(self, preset):
        if self._settle.isActive():     # a drag just before the click
            self._settle.stop()
            self._camera_settled()
        self._event(f"@view {preset}")
        self._absorb = True             # its swing is not a move of its own
        self._settle.start()

    def _on_camera(self):
        if self._moving_from is None:
            self._moving_from = time.monotonic()
        self._settle.start()

    def _camera_settled(self):
        started, self._moving_from = self._moving_from, None
        cam = self._win._viewport._renderer.camera
        now = (cam.azimuth, cam.elevation, cam.distance)
        before, self._cam = self._cam, now
        if self._absorb:
            self._absorb = False
            return
        secs = max(0.3, min(3.0, round(time.monotonic() - (started or 0) - SETTLE_MS / 1000, 1)))
        lines = []
        if abs(now[0] - before[0]) > 0.5 or abs(now[1] - before[1]) > 0.5:
            lines.append(f"@rotate {now[0] % 360:.0f} {now[1]:.0f} over {secs:g}s")
        if before[2] and abs(now[2] / before[2] - 1) > 0.02:
            lines.append(f"@zoom {before[2] / now[2]:.2f} over {secs:g}s")
        if lines:
            self._event(*lines, idle_before=started)

    # -- writing ---------------------------------------------------------

    def _event(self, *lines, idle_before=None):
        self._flush_text()
        self._pause(idle_before)
        self._lines += lines
        self._idle_from = time.monotonic()

    def _pause(self, until=None):
        gap = (until or time.monotonic()) - self._idle_from
        if gap >= PAUSE_MIN_S:
            self._lines.append(f"@pause {round(gap * 2) / 2:g}")

    def _flush_text(self):
        lines, self._cursor = text_steps(self._snap, self._last, self._cursor)
        self._lines += lines
        self._snap = self._last
