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
    actions = [a for a in w._use_library_menu.actions() if a.isEnabled()]
    check("the menu lists every installed library",
          len(actions) == len(installed),
          f"{len(actions)} actions vs {len(installed)} installed")

    # Each action inserts the entry point named in the catalogue.
    for lib in installed:
        primary = next((r["statement"] for r in lib["includes"] if r.get("primary")), None)
        act = next((a for a in actions if a.text() == lib["name"]), None)
        check(f"{lib['name']} is offered", act is not None)
        if act is None or primary is None:
            continue
        w._current_tab().editor.setPlainText("cube(1);\n")
        act.trigger()
        app.processEvents()
        text = w._current_tab().editor.toPlainText()
        check(f"{lib['name']} inserts its entry point",
              primary in text, repr(text.split("\n")[0]))

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
