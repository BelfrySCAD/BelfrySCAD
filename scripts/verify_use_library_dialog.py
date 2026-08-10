#!/usr/bin/env python3
"""Design ▸ Use Library… picks a library, then a file to pull in from it.

A window rather than a menu of submenus: a library can offer dozens of
files, which is awkward to aim at and leaves nowhere to put the
description saying what each one is for.

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
    from belfryscad.window.main_window import MainWindow
    from belfryscad.window.library_manager import (
        UseLibraryDialog, _library_dir, _load_catalog)

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()
    app.processEvents()

    lib_dir = _library_dir()
    installed = [e for e in _load_catalog()
                 if (lib_dir / e["install_as"]).is_dir() and e.get("includes")]
    check("at least one catalogued library is installed to test against",
          bool(installed), str(lib_dir))

    # --- the menu item opens the window ----------------------------------
    # Bound to a name, not chained off the generator: PySide frees the
    # temporary and the QMenu goes with it.
    design_action = next(a for a in w.menuBar().actions() if a.text() == "Design")
    design = design_action.menu()
    actions = list(design.actions())
    act = next((a for a in actions if a.text().startswith("Use Library")), None)
    check("Design has a Use Library item", act is not None)
    check("and it is a plain item, not a menu of submenus",
          act is not None and act.menu() is None)

    act.trigger()
    app.processEvents()
    dlg = w._use_library_dialog
    check("triggering it opens the window", dlg is not None and dlg.isVisible())

    # --- the dropdown ----------------------------------------------------
    names = [dlg._library.itemText(i) for i in range(dlg._library.count())]
    check("the dropdown lists every installed library",
          names == [lib["name"] for lib in installed], str(names))
    check("the dropdown is wide enough for its longest name",
          dlg._library.minimumWidth()
          >= dlg._library.fontMetrics().horizontalAdvance(max(names, key=len)),
          f"{dlg._library.minimumWidth()}px")

    # --- the list and the description ------------------------------------
    for index, lib in enumerate(installed):
        dlg._library.setCurrentIndex(index)
        app.processEvents()
        rows = lib["includes"]
        check(f"{lib['name']}: the list holds all {len(rows)} includes",
              dlg._files.count() == len(rows), str(dlg._files.count()))
        check(f"{lib['name']}: items are named by file, not by statement",
              all("<" not in dlg._files.item(i).text() for i in range(dlg._files.count())))
        first = dlg._files.item(0).text()
        check(f"{lib['name']}: the entry point is first and says so",
              "(entry point)" in first if rows[0].get("primary") else True, first)

        for i, row in enumerate(rows):
            dlg._files.setCurrentRow(i)
            app.processEvents()
            check(f"{row['statement']}: its description is shown",
                  dlg._description.text() == row["description"],
                  dlg._description.text()[:50])
            check(f"{row['statement']}: the statement is shown",
                  dlg._statement.text() == row["statement"], dlg._statement.text())

    check("the description label wraps", dlg._description.wordWrap())

    # --- inserting -------------------------------------------------------
    lib = installed[0]
    dlg._library.setCurrentIndex(0)
    dlg._files.setCurrentRow(0)
    app.processEvents()
    w._current_tab().editor.setPlainText("cube(1);\n")
    dlg._insert_btn.click()
    app.processEvents()
    first_line = w._current_tab().editor.toPlainText().split("\n")[0]
    check("Insert puts the selected statement in the script",
          first_line == lib["includes"][0]["statement"], repr(first_line))
    check("and the window stays open, so a second file is one more click",
          dlg.isVisible())

    if len(lib["includes"]) > 1:
        dlg._files.setCurrentRow(1)
        app.processEvents()
        dlg._insert_btn.click()
        app.processEvents()
        lines = w._current_tab().editor.toPlainText().split("\n")[:2]
        check("a second insert lands below the first",
              lines == [lib["includes"][0]["statement"], lib["includes"][1]["statement"]],
              str(lines))

    # Double-clicking a row does the same as Insert.
    w._current_tab().editor.setPlainText("cube(1);\n")
    dlg._files.setCurrentRow(0)
    dlg._files.itemActivated.emit(dlg._files.item(0))
    app.processEvents()
    check("activating a row inserts it too",
          w._current_tab().editor.toPlainText().split("\n")[0]
          == lib["includes"][0]["statement"])

    # --- reopening picks up an install -----------------------------------
    dlg.close()
    app.processEvents()
    act.trigger()
    app.processEvents()
    check("reopening reuses the same window", w._use_library_dialog is dlg)
    check("and it is visible again", dlg.isVisible())
    check("with the list still populated", dlg._files.count() > 0)

    # --- every statement names a real file -------------------------------
    for lib in installed:
        for row in lib["includes"]:
            named = row["statement"].split("<", 1)[1].rstrip(">")
            check(f"{named} exists on disk", (lib_dir / named).is_file())

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
