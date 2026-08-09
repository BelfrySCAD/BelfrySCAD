#!/usr/bin/env python3
"""Automatic Reload and Render picks up external edits -- and only those.

Driven with real files and the real QFileSystemWatcher, because the whole
feature is about what the filesystem does: a save that replaces the file
rather than writing it in place is the normal case, not an edge one, and it
is exactly what a mocked watcher would hide.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if until():
                return True
        return False

    # Canonical path: on macOS tempfile hands back /var/... while resolve()
    # gives /private/var/..., and the reload matches tabs by resolved path.
    tmp = Path(os.path.realpath(tempfile.mkdtemp()))
    scad = tmp / "watched.scad"
    scad.write_text("cube(1);\n")

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()
    pump(0.6)
    w.open_file_by_path(str(scad))
    pump(1.5)
    tab = w._current_tab()
    check("the file opened", tab.file_path == str(scad), str(tab.file_path))

    # Off by default: nothing is watched until asked for.
    w._act_auto_reload.setChecked(False)
    w._refresh_watched_files()
    check("nothing is watched while the feature is off",
          w._file_watcher.files() == [], str(w._file_watcher.files()))

    scad.write_text("cube(2);\n")
    pump(1.0)
    check("and an external change is ignored",
          "cube(1);" in tab.editor.toPlainText(), tab.editor.toPlainText())

    # Turning it on adopts the already-open file.
    scad.write_text("cube(1);\n")
    tab.editor.replace_span(0, len(tab.editor.toPlainText()), "cube(1);\n")
    tab.is_modified = False
    w._act_auto_reload.setChecked(True)
    w._set_auto_reload(True)
    pump(0.3)
    check("switching it on watches the open file",
          str(scad) in w._file_watcher.files(), str(w._file_watcher.files()))

    # The main case: an external edit lands in the editor.
    scad.write_text("cube(3);\n")
    got = pump(5, lambda: "cube(3);" in tab.editor.toPlainText())
    check("an external edit is loaded into the editor", got,
          tab.editor.toPlainText())
    check("and the tab is not left looking modified", not tab.is_modified)

    # Twice, because a save-by-rename unwatches the path -- without
    # re-arming, only the first change would ever be seen.
    scad.write_text("cube(4);\n")
    check("a second external edit is picked up too",
          pump(5, lambda: "cube(4);" in tab.editor.toPlainText()),
          tab.editor.toPlainText())

    # Save-by-rename, which is how most editors actually write.
    side = tmp / "side.tmp"
    side.write_text("cube(5);\n")
    os.replace(side, scad)
    check("a save that replaces the file is picked up",
          pump(5, lambda: "cube(5);" in tab.editor.toPlainText()),
          tab.editor.toPlainText())

    # Our own save must not bounce back as an external change.
    #
    # Counted at the source rather than by reading the text or the console:
    # a reload of identical text leaves the buffer byte-for-byte the same,
    # and saving triggers a render, which clears the console -- so both of
    # the obvious checks pass whether or not the guard is there.
    reloads = []
    real_reload_one = w._reload_one

    def counting_reload_one(path):
        did = real_reload_one(path)
        if did:
            reloads.append(path)
        return did
    w._reload_one = counting_reload_one

    w._write_file(tab, str(scad))
    pump(1.5)
    check("saving from the app does not trigger a reload", reloads == [],
          str(reloads))

    # The one that would be data loss: unsaved edits must survive.
    tab.editor.replace_span(0, len(tab.editor.toPlainText()), "// mine\ncube(9);\n")
    tab.is_modified = True
    scad.write_text("// theirs\ncube(8);\n")
    pump(2.0)
    check("unsaved edits are never overwritten by a disk change",
          "// mine" in tab.editor.toPlainText(), tab.editor.toPlainText())
    check("and the console says why",
          "unsaved changes" in w._console_tail(), w._console_tail()[-200:])

    # Once saved, the tab is back in sync and reloads resume.
    w._reload_one = real_reload_one
    w._write_file(tab, str(scad))
    pump(1.0)
    scad.write_text("cube(7);\n")
    check("reloads resume after the conflict is resolved",
          pump(5, lambda: "cube(7);" in tab.editor.toPlainText()),
          tab.editor.toPlainText())

    # A deleted file must not empty the tab.
    kept = tab.editor.toPlainText()
    scad.unlink()
    pump(1.5)
    check("deleting the file leaves the tab's contents alone",
          tab.editor.toPlainText() == kept, tab.editor.toPlainText())

    # ...and the watch must survive it. A path that stops existing is
    # dropped for good, so without re-arming after each change this is
    # where the feature would quietly stop working.
    scad.write_text("cube(11);\n")
    check("a recreated file is watched again and reloads",
          pump(5, lambda: "cube(11);" in tab.editor.toPlainText()),
          tab.editor.toPlainText())

    # Switching off stops watching.
    scad.write_text("cube(1);\n")
    pump(0.5)
    w._act_auto_reload.setChecked(False)
    w._set_auto_reload(False)
    check("switching it off unwatches everything",
          w._file_watcher.files() == [], str(w._file_watcher.files()))

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
