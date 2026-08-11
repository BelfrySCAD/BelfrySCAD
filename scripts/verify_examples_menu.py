#!/usr/bin/env python3
"""File ▸ Examples opens the scripts that ship with the application.

Every example is opened and rendered here, not merely listed: an example
that does not render is worse than no example at all, since it is the
first thing someone tries.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import json
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
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from belfryscad.window.main_window import MainWindow, examples_dir

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()

    def pump(seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if until():
                return True
        return False

    pump(0.8)

    root = examples_dir()
    manifest = json.loads((root / "examples.json").read_text(encoding="utf-8"))
    listed = [(cat, n) for cat, names in manifest.items() for n in names]
    check("the manifest lists some examples", bool(listed), str(manifest))

    # --- everything named exists, and nothing on disk is unlisted --------
    for cat, name in listed:
        check(f"{cat}/{name} is there", (root / cat / name).is_file())
    on_disk = {(p.parent.name, p.name) for p in root.rglob("*.scad")}
    check("no example on disk is missing from the manifest",
          on_disk == set(listed), str(sorted(on_disk - set(listed))))

    # --- the menu ---------------------------------------------------------
    file_action = next(a for a in w.menuBar().actions() if a.text() == "File")
    file_menu = file_action.menu()
    entry = next((a for a in file_menu.actions() if a.text() == "Examples"), None)
    check("File has an Examples entry", entry is not None)
    check("and it is a submenu", entry is not None and entry.menu() is not None)

    w._populate_examples_menu()
    app.processEvents()
    cats = [a for a in entry.menu().actions() if a.menu() is not None]
    check("one submenu per category",
          [a.text() for a in cats] == list(manifest), str([a.text() for a in cats]))
    total = sum(len([x for x in c.menu().actions()]) for c in cats)
    check("every example is offered", total == len(listed), f"{total} of {len(listed)}")
    check("items are named without the .scad",
          all(".scad" not in x.text() for c in cats for x in c.menu().actions()))

    # --- opening one ------------------------------------------------------
    first = cats[0].menu().actions()[0]
    first.trigger()
    pump(30, lambda: bool(w._geometry_summary()) and not w._render_busy())
    tab = w._current_tab()
    check("choosing an example opens it", tab.file_path is not None
          and Path(tab.file_path).is_relative_to(root), str(tab.file_path))
    check("and it renders", bool(w._geometry_summary()), w._console_tail()[-120:])
    check("opened read-only, since it lives inside the application",
          tab.editor.isReadOnly())
    check("but it does have its text", len(tab.editor.toPlainText()) > 50)

    # --- every example renders -------------------------------------------
    # The whole point of shipping them.
    for cat, name in listed:
        path = root / cat / name
        w.open_file_by_path(str(path))
        ok = pump(60, lambda: bool(w._geometry_summary()) and not w._render_busy())
        summary = w._geometry_summary()
        tail = w._console_tail()
        bad = [ln for ln in tail.splitlines()[-6:] if "ERROR" in ln or "WARNING" in ln]
        check(f"{cat}/{name} renders", ok and bool(summary),
              (bad[0] if bad else tail[-100:]))
        check(f"{cat}/{name} renders without warnings", not bad, bad[0] if bad else "")

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
