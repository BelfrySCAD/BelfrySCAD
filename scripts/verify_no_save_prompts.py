#!/usr/bin/env python3
"""--no-save-prompts really suppresses the unsaved-changes dialog.

Asserting that the flag sets an attribute would prove nothing: the point
is that no modal appears. QMessageBox.exec() cannot be monkeypatched, so a
live watchdog timer closes any modal that shows up, and its firing is the
failure signal -- which also makes the no-flag case a built-in negative
control.

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

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    from belfryscad.main import _parse_args

    check("the flag defaults to off", _parse_args(["x.scad"]).no_save_prompts is False)
    ns = _parse_args(["--no-save-prompts", "x.scad"])
    check("and is picked up when given", ns.no_save_prompts is True)
    check("without swallowing the file argument", ns.file == "x.scad", str(ns.file))

    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow

    seen = []

    def watch():
        w = app.activeModalWidget()
        if w is not None:
            seen.append(type(w).__name__)
            w.reject() if hasattr(w, "reject") else w.close()

    timer = QTimer()
    timer.timeout.connect(watch)
    timer.start(50)

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    def modified_window(skip):
        w = MainWindow()
        w.skip_unsaved_prompts = skip
        w.persist_settings = False   # don't overwrite the user's layout
        w.show()
        pump(0.4)
        w._new_document()
        w._current_tab().editor.setPlainText("cube(1);")
        pump(0.3)
        return w

    # Without the flag the dialog must appear -- otherwise the check below
    # would pass against a build where prompting was simply broken.
    seen.clear()
    w = modified_window(False)
    check("the tab really is modified", w._current_tab().is_modified)
    w._confirm_unsaved(w._current_tab())
    pump(0.3)
    check("without the flag, closing a modified tab does prompt",
          seen == ["QMessageBox"], str(seen))
    w.close()
    pump(0.3)

    seen.clear()
    w = modified_window(True)
    ok = w._confirm_unsaved(w._current_tab())
    pump(0.3)
    check("with the flag, no dialog appears", seen == [], str(seen))
    check("and the close is allowed to proceed", ok is True)

    # Quitting goes through the same helper, so it must be silent too.
    seen.clear()
    w.close()
    pump(0.5)
    check("quitting with unsaved changes is silent too", seen == [], str(seen))

    # A verifier that builds a MainWindow must not write the user's real
    # window layout. Checked by watching the actual settings value.
    from PySide6.QtCore import QSettings
    st = QSettings("BelfrySCAD", "BelfrySCAD")
    before = st.value("windowState")

    guarded = MainWindow()
    guarded.skip_unsaved_prompts = True
    guarded.persist_settings = False
    guarded.show()
    pump(0.3)
    guarded.resize(731, 519)      # a layout change worth saving
    guarded.close()
    pump(0.3)
    after = QSettings("BelfrySCAD", "BelfrySCAD").value("windowState")
    check("closing with persist_settings off leaves the layout alone",
          after == before, "the saved windowState changed")

    # The negative control: with it on, the same close does write.
    unguarded = MainWindow()
    unguarded.skip_unsaved_prompts = True
    unguarded.persist_settings = True
    unguarded.show()
    pump(0.3)
    unguarded.resize(732, 520)
    unguarded.close()
    pump(0.3)
    wrote = QSettings("BelfrySCAD", "BelfrySCAD").value("windowGeometry")
    check("and with it on, closing really does save (so the check is live)",
          wrote is not None)
    # Put back whatever was there, so running this verifier is not itself
    # the thing that changes the user's layout.
    st = QSettings("BelfrySCAD", "BelfrySCAD")
    if before is not None:
        st.setValue("windowState", before)
    else:
        st.remove("windowState")
    st.sync()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
