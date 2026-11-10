"""File > Play Demo Script...: types a script into the editor at a set
rate and drives the view, for live demos and screen recordings (#1082).

A demo script is plain text. A line starting with `@` is a directive; any
other line is typed into the editor, followed by a newline (`@@` types a
line that starts with `@`). A directive may end in a `//` comment.

    @new                     open a new tab to type into
    @tab 2                   switch to the second tab
    @rate 8                  characters per second (default 8)
    @speed 1.5               play everything after this 1.5x as fast
    @type TEXT               type TEXT exactly, with no newline after it
    @goto 3 5                move the cursor to line 3, column 5
    @goto start              move the cursor to the start of the text
    @goto end                ... or to its end
    @delete 4                delete the 4 characters after the cursor
    @scroll 40               scroll the editor so line 40 is at the top
    @pause 3                 wait 3 seconds
    @pause key               wait for Space (or a presenter remote's "next")
    @preview / @render       F5 / F6, waiting until the result lands
    @view front              a View-menu view: top bottom left right front
                             back iso all center
    @rotate 30 20 over 1.5s  swing to azimuth 30, elevation 20
    @zoom 1.5 over 1s        zoom in 1.5x (below 1 zooms out)
    @zoom 2 at 10 0 5        ... ending centred on the point [10, 0, 5]

During playback Space pauses and resumes wherever the demo is, Up and
Down speed it up and slow it down, and Esc stops it, keeping what it typed.

File > Record Demo Script writes one of these from what you do: see
DemoRecorder below.
"""

import re
import time

import numpy as np

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
    if name == "goto" and [a.lower() for a in args] == ["start"]:
        return ("goto", 1, 1)
    if name == "goto" and [a.lower() for a in args] == ["end"]:
        return ("goto", None, None)
    if name == "goto" and len(args) == 2 and int(args[0]) >= 1 and int(args[1]) >= 1:
        return ("goto", int(args[0]), int(args[1]))
    if name == "delete" and len(args) == 1 and int(args[0]) >= 1:
        return ("delete", int(args[0]))
    if name == "tab" and len(args) == 1 and int(args[0]) >= 1:
        return ("tab", int(args[0]))
    if name == "scroll" and len(args) == 1 and int(args[0]) >= 1:
        return ("scroll", int(args[0]), 300 if ms is None else ms)
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
    if name == "zoom" and len(args) in (1, 5) and float(args[0]) > 0:
        at = None
        if len(args) == 5:
            if args[1].lower() != "at":
                raise ValueError(name)
            at = tuple(float(a) for a in args[2:])
        return ("zoom", float(args[0]), 500 if ms is None else ms, at)
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
        if line is None:                # @goto end
            self._cursor.movePosition(self._cursor.MoveOperation.End)
            self._editor.setTextCursor(self._cursor)
            self._schedule(1000 / self._rate)
            return
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

    def _do_tab(self, n):
        if n <= self._win._tabs.count():
            self._win._tabs.setCurrentIndex(n - 1)
            self._editor = self._win._current_editor()
            self._cursor = self._editor.textCursor()
        else:
            self._win.log(f"Demo: there is no tab {n}; staying on this one.")
        self._schedule(300)

    def _do_scroll(self, line, ms):
        bar = self._editor.verticalScrollBar()
        anim = QVariantAnimation(self)
        anim.setStartValue(bar.value())
        anim.setEndValue(line - 1)
        anim.setDuration(max(1, int(ms / self._speed)))
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.valueChanged.connect(lambda v: bar.setValue(int(v)))
        anim.finished.connect(self._zoom_done)
        self._anim = anim
        anim.start()

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

    def _do_zoom(self, factor, ms, at=None):
        vp = self._viewport()
        cam = vp._renderer.camera
        d0, t0 = cam.distance, cam.target.copy()
        t1 = t0 if at is None else np.array(at, dtype=t0.dtype)

        def step(v):
            cam.target = t0 + (t1 - t0) * v
            vp.set_distance(d0 + (d0 / factor - d0) * v)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(max(1, int(ms / self._speed)))
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.valueChanged.connect(step)
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

    It records what you did, not when you did it: the text typed (as a diff
    between moments, so the script types it at its own @rate), cursor moves,
    editor scrolls, tab switches, File > New, Preview and Render, named
    views, camera moves once they settle, and pauses of a second or more.
    It records in the tab in front, and opens with a @tab naming it. Pans
    are not recorded; the player has none.
    """

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self._editor = None
        self._touched = 0.0
        self._new_tab = False
        self._idle_from = time.monotonic()
        self._cursor_settle = QTimer(self, singleShot=True, interval=SETTLE_MS)
        self._cursor_settle.timeout.connect(self._cursor_settled)
        self._scroll_settle = QTimer(self, singleShot=True, interval=SETTLE_MS)
        self._scroll_settle.timeout.connect(self._scroll_settled)
        self._lines = [self._tab_line(), f"@rate {DEFAULT_RATE:g}   // recorded; edit freely"]
        self._bind(window._current_editor())
        window._tabs.currentChanged.connect(self._on_tab)
        vp = window._viewport
        cam = vp._renderer.camera
        self._cam = (cam.azimuth, cam.elevation, cam.distance, cam.target.copy())
        self._moving_from = None
        self._absorb = False
        self._settle = QTimer(self, singleShot=True, interval=SETTLE_MS)
        self._settle.timeout.connect(self._camera_settled)
        vp.camera_changed.connect(self._on_camera)
        vp.view_preset_chosen.connect(self.note_view)
        window.log("Recording demo: choose File > Record Demo Script again to stop.")

    def finish(self) -> str:
        self._flush_pending()
        self._settle.stop()             # a view's swing still under way
        self._flush_text()
        self._bind(None)
        self._win._tabs.currentChanged.disconnect(self._on_tab)
        vp = self._win._viewport
        vp.camera_changed.disconnect(self._on_camera)
        vp.view_preset_chosen.disconnect(self.note_view)
        return "\n".join(self._lines) + "\n"

    def _tab_line(self):
        tabs = self._win._tabs
        tab = tabs.currentWidget()
        name = tab.display_name().rstrip("*") if tab is not None else ""
        return f"@tab {tabs.currentIndex() + 1}   // {name}"

    def _bind(self, editor):
        """Follow `editor`'s text, cursor and scrolling, and only its."""
        if self._editor is not None:
            self._editor.document().contentsChanged.disconnect(self._on_text)
            self._editor.cursorPositionChanged.disconnect(self._on_cursor)
            self._editor.verticalScrollBar().valueChanged.disconnect(self._on_scroll)
        self._editor = editor
        if editor is None:
            return
        self._snap = self._last = editor.toPlainText()
        self._cursor = editor.textCursor().position()
        self._scroll = editor.verticalScrollBar().value()
        editor.document().contentsChanged.connect(self._on_text)
        editor.cursorPositionChanged.connect(self._on_cursor)
        editor.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # -- what happened ---------------------------------------------------

    def note_new(self):
        """File > New, called before the tab opens."""
        self._event("@new")
        self._new_tab = True

    def _on_tab(self, _index):
        if self._editor is not None:
            self._flush_pending()
            self._flush_text()
        if self._new_tab:
            self._new_tab = False       # File > New's own switch
        else:
            self._event(self._tab_line())
        self._bind(self._win._current_editor())

    def _on_cursor(self):
        self._touched = time.monotonic()
        self._cursor_settle.start()

    def _cursor_settled(self):
        self._cursor_settle.stop()
        self._flush_text()
        cur = self._editor.textCursor().position()
        if cur != self._cursor:         # moved, not just typed at
            self._cursor = cur
            text = self._editor.toPlainText()
            self._event("@goto start" if cur == 0 else "@goto end" if cur == len(text)
                        else "@goto %d %d" % _line_col(text, cur))

    def _on_scroll(self, value):
        if time.monotonic() - self._touched < 0.15:
            self._scroll = value        # typing or the cursor scrolled it; replay does too
        else:
            self._scroll_settle.start()

    def _scroll_settled(self):
        self._scroll_settle.stop()
        value = self._editor.verticalScrollBar().value()
        if value != self._scroll:
            self._scroll = value
            self._event(f"@scroll {value + 1}")

    def _flush_pending(self):
        """Write what is still settling, so events stay in order."""
        for timer, done in ((self._settle, self._camera_settled),
                            (self._cursor_settle, self._cursor_settled),
                            (self._scroll_settle, self._scroll_settled)):
            if timer.isActive():
                timer.stop()
                done()

    def _on_text(self):
        self._touched = time.monotonic()
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
        anim = getattr(self._win._viewport, "_view_anim", None)
        if self._absorb and anim is not None and anim.state() == QVariantAnimation.State.Running:
            self._settle.start()        # a named view still swinging
            return
        started, self._moving_from = self._moving_from, None
        cam = self._win._viewport._renderer.camera
        now = (cam.azimuth, cam.elevation, cam.distance, cam.target.copy())
        before, self._cam = self._cam, now
        if self._absorb:
            self._absorb = False
            return
        secs = max(0.3, min(3.0, round(time.monotonic() - (started or 0) - SETTLE_MS / 1000, 1)))
        lines = []
        if abs(now[0] - before[0]) > 0.5 or abs(now[1] - before[1]) > 0.5:
            lines.append(f"@rotate {now[0] % 360:.0f} {now[1]:.0f} over {secs:g}s")
        if before[2] and abs(now[2] / before[2] - 1) > 0.02:
            # Zooming toward the pointer moves the target: where it ends up
            # is where the zoom was centred.
            at = ""
            if np.linalg.norm(now[3] - before[3]) > 1e-3 * now[2]:
                at = " at " + " ".join(f"{c:.4g}" for c in now[3])
            lines.append(f"@zoom {before[2] / now[2]:.2f}{at} over {secs:g}s")
        if lines:
            self._event(*lines, idle_before=started)

    # -- writing ---------------------------------------------------------

    def _event(self, *lines, idle_before=None):
        self._flush_pending()
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
