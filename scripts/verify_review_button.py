#!/usr/bin/env python3
"""The chat pane's "Review…" button always gets you to the review window.

The bar is visible for as long as a proposal is unresolved -- whether or
not its window is open. So the button is exactly what you press when the
window is open but not in front of you: behind the main window, on
another Space, or minimised. It used to return early in that case and do
nothing at all, which reads as a dead button.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.ai_chat import AIChatPane, MODE_MANUAL
    from belfryscad.window.ai_tools import Proposal

    pane = AIChatPane()
    pane.show()
    app.processEvents()
    pane._mode.setCurrentIndex(pane._mode.findData(MODE_MANUAL))
    app.processEvents()
    check("the pane is in the mode that reviews rather than auto-applies",
          pane.mode() == MODE_MANUAL, pane.mode())

    def propose(summary):
        p = Proposal(kind="edit", summary=summary, new_content="x\n",
                     diff_text="--- a\n+++ b\n@@ -1 +1,2 @@\n a\n+b\n",
                     filename="belfryNoodling.scad")
        pane._on_proposal(p)
        app.processEvents()
        return p

    first = propose("Include BOSL2's nurbs library")
    check("the proposal is pending", pane._pending == [first], str(pane._pending))
    check("the review bar is up", pane._review_bar.isVisible())
    check("and the window opened by itself", pane._review_dialog is not None)
    check("visibly", pane._review_dialog.isVisible())

    # --- the reported case: the window is open but out of sight ----------
    dlg = pane._review_dialog
    dlg.hide()                      # stands in for "behind the main window"
    app.processEvents()
    check("the window is out of sight but still open",
          not dlg.isVisible() and pane._review_dialog is dlg)

    pane._review_btn.click()
    app.processEvents()
    check("Review brings that same window back, rather than doing nothing",
          pane._review_dialog is dlg and dlg.isVisible(),
          f"dialog={pane._review_dialog is dlg} visible={dlg.isVisible()}")
    check("and does not open a second one for the same proposal",
          len(pane.findChildren(type(dlg))) == 1,
          str(len(pane.findChildren(type(dlg)))))
    check("the proposal is still pending, undecided",
          pane._pending == [first], str(pane._pending))

    # --- closing it without deciding, then reopening ---------------------
    dlg.close()
    app.processEvents()
    check("closing without choosing keeps it pending",
          pane._pending == [first] and pane._review_dialog is None,
          f"pending={len(pane._pending)} dialog={pane._review_dialog}")
    check("and leaves the bar up as the way back", pane._review_bar.isVisible())

    pane._review_btn.click()
    app.processEvents()
    check("Review opens it again", pane._review_dialog is not None
          and pane._review_dialog.isVisible())

    # --- deciding resolves it -------------------------------------------
    accepted = []
    pane.proposal_accepted.connect(accepted.append)
    pane._review_dialog._accept()
    app.processEvents()
    check("accepting emits the proposal", accepted == [first], str(accepted))
    check("clears it from the queue", pane._pending == [], str(pane._pending))
    check("and takes the bar down with it", not pane._review_bar.isVisible())

    pane._review_btn.click()
    app.processEvents()
    check("with nothing pending, Review opens nothing",
          pane._review_dialog is None, str(pane._review_dialog))

    # --- a queue of two --------------------------------------------------
    a = propose("first")
    b = propose("second")
    check("both are queued", pane._pending == [a, b], str(pane._pending))
    check("only the first has a window open",
          pane._review_dialog is not None
          and pane._review_dialog.proposal is a,
          str(pane._review_dialog.proposal.summary if pane._review_dialog else None))
    pane._review_dialog._reject()
    app.processEvents()
    check("rejecting the first brings up the second",
          pane._review_dialog is not None and pane._review_dialog.proposal is b,
          str(pane._review_dialog.proposal.summary if pane._review_dialog else None))

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
