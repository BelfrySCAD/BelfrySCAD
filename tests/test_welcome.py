"""The Welcome window (OpenSCAD's LaunchingScreen): recent files and
examples, one double-click away, shown at startup without a file.

The window is driven in a subprocess, since it needs a MainWindow and
MainWindow needs GL.
"""
import json
import subprocess
import sys

from belfryscad.window.main_window import example_categories, examples_dir


def test_example_categories_follow_the_manifest():
    manifest = json.loads((examples_dir() / "examples.json").read_text(encoding="utf-8"))
    got = {cat: [p.name for p in paths] for cat, paths in example_categories()}
    assert got == manifest


def test_the_window_opens_recents_and_examples(tmp_path):
    scad = tmp_path / "mine.scad"
    scad.write_text("cube(1);\n")
    driver = tmp_path / "_welcome.py"
    driver.write_text(f'''
import json, tempfile
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import app_settings, use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-welcome-"), seed=False)
app_settings().setValue("recentFiles", [{str(scad)!r}])
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
from PySide6.QtWidgets import QMessageBox
errors = []
QMessageBox.critical = staticmethod(lambda *a, **k: errors.append(str(a[2:])))
help_menu = next(m for m in w.menuBar().findChildren(type(w.menuBar().addMenu("x")))
                 if m.title().replace("&", "") == "Help")
out = {{"in_help": "Welcome Screen…" in [a.text() for a in help_menu.actions()]}}

dlg = w.show_welcome()
out["recents"] = [dlg.recent_list.item(i).text() for i in range(dlg.recent_list.count())]
out["categories"] = [dlg.example_tree.topLevelItem(i).text(0)
                     for i in range(dlg.example_tree.topLevelItemCount())]
dlg.recent_list.itemActivated.emit(dlg.recent_list.item(0))
out["opened"] = str(w._current_tab().file_path)
out["tabs"] = [str(w._tabs.widget(i).file_path) for i in range(w._tabs.count())]
out["errors"] = errors
out["recent_data"] = dlg.recent_list.item(0).data(Qt.ItemDataRole.UserRole)

dlg = w.show_welcome()
leaf = dlg.example_tree.topLevelItem(0).child(0)
dlg.example_tree.itemActivated.emit(leaf, 0)
tab = w._current_tab()
out["example_tab_path"] = tab.file_path
out["example_suggested"] = tab.suggested_name
out["example_has_text"] = bool(tab.editor.toPlainText().strip())

dlg = w.show_welcome()
dlg.show_at_startup.setChecked(False)
out["pref"] = app_settings().value("app/showWelcome", True, type=bool)
dlg.close()
w.close()
print(json.dumps(out))
''', encoding="utf-8")  # the … in "Welcome Screen…"; Windows defaults to cp1252
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                         env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                              "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["in_help"]
    assert out["recents"] == ["mine.scad"]
    assert out["categories"] == [c for c, _ in example_categories()]
    assert out["opened"] == str(scad.resolve()), json.dumps({k: out[k] for k in ("opened", "tabs", "errors", "recent_data")})
    # An example opens as an untitled, editable copy, not the bundled file.
    assert out["example_tab_path"] is None
    assert out["example_suggested"] == example_categories()[0][1][0].name
    assert out["example_has_text"]
    assert out["pref"] is False
