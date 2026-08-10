#!/usr/bin/env python3
"""The Use Library menu offers each installed library's entry point.

It reads the catalogue's `includes` list -- the row marked primary --
rather than a separate statement field that could drift out of step with
it.

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
    from belfryscad.window.library_manager import _library_dir, _load_catalog

    w = MainWindow()
    w.skip_unsaved_prompts = True
    w.persist_settings = False
    w.show()
    app.processEvents()

    lib_dir = _library_dir()
    catalog = _load_catalog()
    installed = [e for e in catalog if (lib_dir / e["install_as"]).is_dir()]
    check("at least one catalogued library is installed to test against",
          bool(installed), str(lib_dir))

    w._populate_use_library_menu()
    app.processEvents()
    entries = [a for a in w._use_library_menu.actions() if a.isEnabled()]
    check("the menu lists every installed library",
          len(entries) == len(installed),
          f"{len(entries)} entries vs {len(installed)} installed")

    for lib in installed:
        rows = lib["includes"]
        entry = next((a for a in entries if a.text() == lib["name"]), None)
        check(f"{lib['name']} is offered", entry is not None)
        if entry is None:
            continue

        if len(rows) == 1:
            check(f"{lib['name']} with one include is a plain item, not a submenu",
                  entry.menu() is None)
            acts = [entry]
        else:
            sub = entry.menu()
            check(f"{lib['name']} opens a submenu", sub is not None)
            if sub is None:
                continue
            acts = [a for a in sub.actions() if not a.isSeparator()]
            check(f"{lib['name']} lists all {len(rows)} of its includes",
                  len(acts) == len(rows), f"{len(acts)} items")
            check(f"{lib['name']} shows tool tips", sub.toolTipsVisible())
            check(f"{lib['name']} puts its entry point first and sets it apart",
                  sub.actions()[0] is acts[0]
                  and any(a.isSeparator() for a in sub.actions()[:2]),
                  str([a.text() or "---" for a in sub.actions()[:3]]))
            check(f"{lib['name']} labels items by file, not the whole statement",
                  all("<" not in a.text() for a in acts),
                  str([a.text() for a in acts[:2]]))

        # Every item carries its description, and inserts its own statement.
        for act, row in zip(acts, rows):
            check(f"{row['statement']} carries its description",
                  row["description"] in act.toolTip(), act.toolTip()[:60])
            w._current_tab().editor.setPlainText("cube(1);\n")
            act.trigger()
            app.processEvents()
            first = w._current_tab().editor.toPlainText().split("\n")[0]
            check(f"{row['statement']} is what it inserts",
                  first == row["statement"], repr(first))

    # The statement must name a file that is really there, or it is a
    # broken line in the user's script.
    for lib in installed:
        for row in lib["includes"]:
            named = row["statement"].split("<", 1)[1].rstrip(">")
            check(f"{named} exists on disk", (lib_dir / named).is_file())

    w.close()
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
