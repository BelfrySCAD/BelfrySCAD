"""Checks that AI Chat scheduled follow-ups actually fire.

A standalone script, not a pytest test: constructing Qt widgets under
pytest crashes the runner (same reason as scripts/verify_gestures.py).

The interesting case is the last one -- a render that completes BEFORE
the follow-up is registered. In Accept Edits/Auto mode that is the norm,
not an edge case: applying an edit starts a render that finishes in
milliseconds, while the schedule_followup tool call only arrives after a
round-trip to the model. on_render_finished() used to drop the
notification when nothing was pending yet, so the follow-up then waited
for a render that had already happened, sitting at "After the next
render" for the full 900s backstop -- indistinguishable from never
firing.
"""
import sys, time
from PySide6.QtGui import QSurfaceFormat
fmt = QSurfaceFormat(); fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QEventLoop

from belfryscad.window.ai_chat import AIChatPane
from belfryscad.window.ai_tools import Followup, TRIGGER_DELAY, TRIGGER_RENDER

app = QApplication(sys.argv)
pane = AIChatPane()
fired = []
pane.send_requested.connect(lambda t: fired.append(t))

def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.02)

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    return cond

ok = True

# --- delay-triggered ---
fired.clear()
pane._on_followup(Followup(prompt="delayed one", delay_s=1.0, trigger=TRIGGER_DELAY))
print("  bar visible after scheduling:", pane._followup_bar.isVisible(),
      "| timer active:", pane._followup_timer.isActive(),
      "| streaming:", pane._streaming)
pump(4.0)
ok &= check("delay follow-up fires within 4s", fired == ["delayed one"], f"fired={fired}")

# --- render-triggered ---
fired.clear()
pane._followup_chain = 0
pane._on_followup(Followup(prompt="render one", trigger=TRIGGER_RENDER))
pump(1.5)
print("  before render: released =", pane._followup_released, "| fired =", fired)
pane.on_render_finished()
pump(8.0)
ok &= check("render follow-up fires after on_render_finished", fired == ["render one"], f"fired={fired}")

# --- fires after a turn that was streaming ---
fired.clear()
pane._followup_chain = 0
pane._set_streaming(True)
pane._on_followup(Followup(prompt="while busy", delay_s=1.0, trigger=TRIGGER_DELAY))
pump(3.0)
was_held = (fired == [])
pane._set_streaming(False)
pump(3.0)
ok &= check("follow-up held during a turn, then fires", was_held and fired == ["while busy"],
            f"held={was_held} fired={fired}")

# --- the regression: render finishes BEFORE the follow-up is registered ---
fired.clear()
pane._followup_chain = 0
pane._turn_started_at = time.monotonic()
pane.on_render_finished()          # render completes first...
pump(0.3)
pane._on_followup(Followup(prompt="look at it", trigger=TRIGGER_RENDER))  # ...then the tool call
pump(9.0)
ok &= check("render that finished before scheduling still releases",
            fired == ["look at it"], f"fired={fired}")

# A render from BEFORE this turn must not release a new follow-up.
fired.clear()
pane._followup_chain = 0
pane._last_render_at = time.monotonic()
pump(0.1)
pane._turn_started_at = time.monotonic()      # new turn starts after that render
pane._on_followup(Followup(prompt="stale", trigger=TRIGGER_RENDER))
pump(7.0)
ok &= check("a render from a previous turn does not release", fired == [], f"fired={fired}")

print()
print("ALL PASS" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
