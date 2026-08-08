#!/usr/bin/env python3
"""View > Reset Panel Layout really puts the panels back.

The failure this covers: the first version cleared the saved layout and
left restoreState to apply defaults on the next launch. closeEvent saves
the current arrangement on the way out, so quitting immediately wrote the
same broken layout back over the cleared one, and the reset appeared to do
nothing at all.

So the check moves a dock, resets, and then closes and reopens -- because
the bug only shows up across a quit.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow
    from PySide6.QtCore import QSettings

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    # The user's real settings are read here, so put back whatever was
    # there: this verifier must not be the thing that changes their layout.
    real = QSettings("BelfrySCAD", "BelfrySCAD")
    saved = (real.value("windowState"), real.value("windowGeometry"),
             real.value("layoutVersion"))

    def restore_real():
        s = QSettings("BelfrySCAD", "BelfrySCAD")
        for key, val in zip(("windowState", "windowGeometry", "layoutVersion"),
                            saved):
            s.remove(key) if val is None else s.setValue(key, val)
        s.sync()

    # Reset asks for confirmation; QMessageBox.question spins its own event
    # loop, so answer it from a timer rather than trying to patch it.
    answered = []

    def answer_dialog():
        """Poll until the modal actually exists. A single delayed shot is a
        guess at when the dialog appears; if it fires early the dialog is
        not up yet, nothing is clicked, and the reset silently returns
        Cancel -- which reads exactly like a reset that does not work."""
        timer = QTimer()
        timer.setInterval(50)

        def click():
            dlg = app.activeModalWidget()
            if isinstance(dlg, QMessageBox):
                # click(), not done(): QMessageBox maps its result from the
                # button that was clicked, so done(Reset) makes question()
                # return 0 and the caller reads it as Cancel.
                btn = dlg.button(QMessageBox.StandardButton.Reset)
                if btn is None:
                    return
                answered.append(True)
                timer.stop()
                btn.click()
        timer.timeout.connect(click)
        timer.start()
        return timer

    try:
        def layout_of(win):
            """Where each dock sits, and whether it shows. saveState() bytes
            are no good for this -- they carry transient sizing, so two
            visually identical layouts compare unequal."""
            return {name: (win.dockWidgetArea(dock), dock.isVisible())
                    for name, dock in (("ai", win._ai_chat_dock),
                                       ("debug", win._debugger_dock))}

        w = MainWindow()
        w.skip_unsaved_prompts = True
        w.show()
        pump(0.6)

        check("the default layout was captured before restoring",
              getattr(w, "_default_window_state", None) is not None)

        w.restoreState(w._default_window_state)
        pump(0.4)
        default = layout_of(w)

        # Put a dock somewhere it does not belong, as a bad saved layout would.
        w.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, w._ai_chat_dock)
        w._ai_chat_dock.show()
        pump(0.4)
        check("moving a dock really does change the layout",
              layout_of(w) != default, str(layout_of(w)))

        answered.clear()
        timer = answer_dialog()
        w._reset_panel_layout()
        timer.stop()
        pump(0.5)
        check("the confirmation dialog was actually answered", bool(answered),
              "no QMessageBox appeared, so the reset was never confirmed")
        check("resetting restores the defaults there and then",
              layout_of(w) == default,
              f"{layout_of(w)} != {default}")

        # The part that was broken: closeEvent saves the layout on the way
        # out, so a reset that only cleared the settings was undone by
        # quitting. persist_settings stays on here deliberately -- that is
        # the path the user takes.
        w.close()
        pump(0.4)

        w2 = MainWindow()
        w2.persist_settings = False
        w2.skip_unsaved_prompts = True
        w2.show()
        pump(0.6)
        check("and the next launch still comes up with the defaults",
              layout_of(w2) == default,
              f"{layout_of(w2)} != {default}")
        w2.close()
        pump(0.3)
    finally:
        restore_real()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
