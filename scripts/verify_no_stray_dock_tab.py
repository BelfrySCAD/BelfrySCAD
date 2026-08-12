#!/usr/bin/env python3
"""No stray dock tab sits on top of the toolbar.

restoreState() rebuilds the dock area layout, and the QTabBars the
previous layout owned are left as children of the QMainWindow with
nothing referencing them -- unpositioned at (0, 0), which is directly
over the toolbar, showing as a stray one-tab "AI Chat" tab in the
top-left corner. It only appears once a layout has been saved, so it
turns up for everyone after their first session and never on a fresh
profile, which is why it survived to reach the wiki screenshots.

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

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabBar  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    import belfryscad.window.main_window as mw

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    # A saved layout is what triggers it, so the test supplies one rather
    # than depending on whatever this machine happens to have stored.
    real_settings = mw.QSettings

    probe = mw.MainWindow()
    probe.skip_unsaved_prompts = True
    probe.persist_settings = False
    probe.resize(1400, 900)
    probe.show()
    pump(1.5)
    saved_state = probe.saveState()
    saved_geom = probe.saveGeometry()
    probe.close()
    pump(0.4)

    class SavedLayout:
        """A profile that already has a window layout stored."""
        def __init__(self, *a, **k):
            pass

        def value(self, key, default=None, type=None):
            return {"windowState": saved_state,
                    "windowGeometry": saved_geom,
                    "layoutVersion": mw.MainWindow._LAYOUT_VERSION}.get(key, default)

        def setValue(self, *a):
            pass

        def remove(self, *a):
            pass

        def sync(self):
            pass

    mw.QSettings = SavedLayout
    try:
        w = mw.MainWindow()
        w.skip_unsaved_prompts = True
        w.persist_settings = False
        w.resize(1400, 900)
        w.show()
        pump(2.0)

        def own_tabbars():
            return [b for b in w.findChildren(QTabBar) if b.parent() is w]

        def over_toolbar():
            """Visible dock tab bars in the top-left, where the toolbar is."""
            return [b for b in own_tabbars()
                    if b.isVisible() and b.x() >= 0 and b.y() >= 0]

        check("a saved layout really was restored", not w._first_show)
        strays = over_toolbar()
        check("no dock tab bar is drawn over the toolbar", not strays,
              f"{len(strays)} at {[(b.x(), b.y(), [b.tabText(i) for i in range(b.count())]) for b in strays]}")

        # The toolbar's own top-left corner must be the toolbar.
        tb = w.findChild(type(w.findChildren(QTabBar)[0]))  # noqa: F841 (touch)
        img = w.grab().toImage()
        toolbar = next(t for t in w.findChildren(mw.QToolBar)
                       if t.objectName() == "MainToolBar")
        check("the toolbar starts at the top-left", toolbar.y() == 0,
              f"toolbar at y={toolbar.y()}")
        check("the grabbed window is big enough to inspect",
              img.width() > 200 and img.height() > 100)

        # --- and real dock tabs must still work ------------------------
        # The fix hides only bars with <2 tabs; Qt re-shows and repositions
        # one when a second dock joins the group. If that were wrong, the
        # Customizer/AI Chat tabs would vanish.
        w._customizer_dock.show()
        w._ai_chat_dock.show()
        pump(1.2)
        grouped = [b for b in own_tabbars() if b.count() >= 2]
        check("a real two-dock tab group still shows its tabs",
              any(b.isVisible() for b in grouped),
              f"{[(b.count(), b.isVisible(), (b.x(), b.y())) for b in own_tabbars()]}")
        labels = sorted(t for b in grouped for t in
                        (b.tabText(i) for i in range(b.count())))
        check("and the group holds the docks that were shown",
              "AI Chat" in labels and "Customizer" in labels, str(labels))
        check("that group is positioned in the window, not at the origin",
              all(b.y() > 0 for b in grouped if b.isVisible()),
              str([(b.x(), b.y()) for b in grouped]))

        w.close()
        pump(0.3)
    finally:
        mw.QSettings = real_settings

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
