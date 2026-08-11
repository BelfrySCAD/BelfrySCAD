#!/usr/bin/env python3
"""A follow-up is the model prompting itself, and is not labelled "You:".

schedule_followup is a tool the model calls to be prompted again later.
The prompt it supplies was rendered into the transcript as "**You:**",
putting words the user never wrote in their mouth.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.ai_chat import AIChatPane

    pane = AIChatPane()
    pane.show()
    end = time.monotonic() + 0.6
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    USER = "Make the skirt taller."
    FOLLOW = "Check the render finished and report the new height."
    REPLY = "Raised skirt_h to 72."

    # Drive the real path rather than naming the kind here: the pane itself
    # decides how a due follow-up is recorded, and that decision is the bug.
    # Hardcoding ("followup", ...) would pass against the old code, which
    # labelled it "You:" only because the call site said kind="user".
    from belfryscad.window.ai_tools import Followup

    sent = []
    pane.send_requested.connect(sent.append)

    pane._entries = [("user", USER), ("assistant", REPLY)]
    pane._render_transcript()

    pane._followup = Followup(prompt=FOLLOW, delay_s=0.0)
    pane._followup_due = time.monotonic() - 1          # already due
    pane._streaming = False
    pane._tick_followup()
    app.processEvents()

    check("the due follow-up was actually delivered",
          sent == [FOLLOW], f"send_requested got {sent}")
    check("and it reached the transcript",
          any(t == FOLLOW for _k, t in pane._entries),
          str(pane._entries))

    pane._entries.append(("assistant", "Render finished; the skirt is 72 tall."))
    pane._entries.append(("note", "Applied: skirt height (Dalek.scad)"))
    pane._render_transcript()
    app.processEvents()

    text = pane._transcript.toPlainText()
    print("\n--- transcript ---\n" + text + "------------------\n")

    # --- the point of the exercise ---------------------------------------
    check("the user's own message is attributed to them",
          f"You: {USER}" in text, text[:200])
    check("the follow-up is NOT attributed to the user",
          f"You: {FOLLOW}" not in text,
          "the model's own prompt is labelled 'You:'")
    check("the follow-up is labelled as a follow-up",
          f"Follow-up: {FOLLOW}" in text, text[:300])
    check("exactly one 'You:' for one user message",
          text.count("You:") == 1, f"found {text.count('You:')}")

    # --- and nothing else regressed --------------------------------------
    check("assistant replies stay unlabelled",
          REPLY in text and f"You: {REPLY}" not in text)
    check("notes still render", "Applied: skirt height" in text)

    # A follow-up still opens up space the way a request does, rather than
    # running into the reply above it.
    check("the follow-up is separated from the reply above it",
          text.index(FOLLOW) > text.index(REPLY))

    # --- the wiring: what the pane actually stores for a due follow-up ---
    # Guards the call site, not just the renderer.
    src = (Path(__file__).resolve().parent.parent
           / "src/belfryscad/window/ai_chat.py").read_text()
    check("a due follow-up is recorded as kind='followup'",
          'self._say(followup.prompt, kind="followup")' in src,
          "the call site still says kind='user'")
    check("a typed message is still recorded as kind='user'",
          'self._say(shown, kind="user")' in src)

    pane.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
