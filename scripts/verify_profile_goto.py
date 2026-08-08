#!/usr/bin/env python3
"""Double-clicking a profile row must navigate, including for a modified tab.

A modified tab renders from a temp copy of its live buffer, so its profile
rows carry that temp path as call_origin -- not the tab's own file_path.
_find_or_open_tab used to match file_path only, so those rows navigated
nowhere, and the temp file is unlinked after the render so the fallback
"open it in a new tab" could not rescue it either.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtWidgets import QApplication  # noqa: E402

from belfryscad.window.main_window import MainWindow  # noqa: E402

failures = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    with tempfile.TemporaryDirectory() as td:
        saved = Path(td) / "model.scad"
        saved.write_text("module m() { cube(1); }\nm(); m();\n")

        mw = MainWindow()
        mw.skip_unsaved_prompts = True   # a prompt would hang, not fail
        mw.open_file_by_path(str(saved))
        tab = mw._tabs.currentWidget()
        check("the file opened in a tab", tab is not None and tab.file_path == str(saved))

        # A saved tab was never broken -- pin it so a future change cannot
        # trade one case for the other.
        found, idx = mw._find_or_open_tab(str(saved))
        check("a saved tab is found by its own path", found is tab and idx >= 0)

        # What a render of a MODIFIED buffer produces: a temp .scad that no
        # tab's file_path will ever equal, recorded on the tab as
        # _last_parse_path (RenderCallback.on_ast_ready does this for real).
        tmp = Path(td) / "belfry_live_buffer.scad"
        tmp.write_text(saved.read_text())
        tab._last_parse_path = str(tmp)

        found, idx = mw._find_or_open_tab(str(tmp))
        check("a temp parse path resolves to its own tab", found is tab and idx >= 0)

        # The real failure mode: the temp file is gone by the time the user
        # double-clicks, so a lookup that falls through to "open it" fails.
        tmp.unlink()
        found, idx = mw._find_or_open_tab(str(tmp))
        check("still resolves after the temp file is unlinked", found is tab and idx >= 0)

        # And the navigation slot the profile viewer actually emits into.
        mw._on_debug_frame_selected(str(tmp), 2)
        check("double-click navigation selects that tab", mw._tabs.currentWidget() is tab)

        # An unrelated missing path must still report "no tab", not silently
        # land on whichever tab happens to be open.
        missing, midx = mw._find_or_open_tab(str(Path(td) / "not_here.scad"))
        check("an unknown path still returns no tab", missing is None and midx == -1)

        mw.close()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
