"""File > Open / untitled Save As start in the last .scad folder, else
Documents -- never the cwd, which on Windows is the install folder (#690)."""
import os
from pathlib import Path

from belfryscad import settings
from belfryscad.settings import app_settings, use_scratch_settings
from belfryscad.window.main_window import file_dialog_dir, remember_file_dir
from belfryscad import main


def test_remembers_the_folder_and_falls_back_when_it_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_scratch_dir", settings._scratch_dir)  # restored after
    use_scratch_settings(str(tmp_path / "settings"), seed=False)
    # never the real store; Path, since Qt writes "/" on Windows too
    assert Path(app_settings().fileName()).resolve().is_relative_to(tmp_path.resolve())
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(main, "_default_working_dir", lambda: docs)
    monkeypatch.chdir(tmp_path)

    assert file_dialog_dir() == str(docs)

    work = tmp_path / "work"
    work.mkdir()
    remember_file_dir(str(work / "part.scad"))
    assert file_dialog_dir() == str(work)

    os.rmdir(work)
    assert file_dialog_dir() == str(docs)


DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow, file_dialog_dir
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None      # only the folder matters here
out = {}
for name, render in (("lib", False), ("work", True)):
    d = os.path.realpath(tempfile.mkdtemp(prefix=name))
    path = os.path.join(d, "t.scad")
    open(path, "w").write("cube(1);")
    w.open_file_by_path(path, render=render)
    out[name] = file_dialog_dir() == d
print(json.dumps(out), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)                  # skip teardown of threads Qt left behind
'''


def test_any_open_remembers_the_folder_but_revealing_a_library_does_not():
    # Recent Files, a double-click in Explorer and a drop all arrive here,
    # never through the Open dialog, so it is where the folder is noted (#690).
    import json, subprocess, sys
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"lib": False, "work": True}, r.stderr


def test_exporting_a_design_again_starts_where_its_last_export_went(tmp_path, monkeypatch):
    """#863: the folder an export went to is offered the next time the same
    design is exported -- not for another design, and not once it is gone."""
    from types import SimpleNamespace
    from belfryscad.window.main_window import MainWindow
    from belfryscad.window.preferences import save_preferences
    monkeypatch.setattr(settings, "_scratch_dir", settings._scratch_dir)
    use_scratch_settings(str(tmp_path / "settings"), seed=False)
    assert Path(app_settings().fileName()).resolve().is_relative_to(tmp_path.resolve())
    design = tmp_path / "src" / "part.scad"
    out = tmp_path / "out"
    out.mkdir()
    tab = SimpleNamespace(file_path=design, export_name="part")
    win = SimpleNamespace(_current_tab=lambda: tab)
    offered = lambda: MainWindow._default_export_path(win)

    assert offered() == os.path.join(str(tmp_path / "src"), "part")
    save_preferences({"export/lastDir": str(out), "export/lastSource": str(design)})
    assert offered() == os.path.join(str(out), "part")
    tab.file_path = tmp_path / "src" / "other.scad"
    assert offered() == os.path.join(str(tmp_path / "src"), "part")
    tab.file_path = design
    os.rmdir(out)
    assert offered() == os.path.join(str(tmp_path / "src"), "part")


PICKER_DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QFileDialog
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow, file_dialog_dir, remember_file_dir
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
asked = []
QFileDialog.getExistingDirectory = staticmethod(lambda parent, title, start="", *a: (asked.append(start), "")[1])
last = os.path.realpath(tempfile.mkdtemp(prefix="last"))
remember_file_dir(os.path.join(last, "x.scad"))
w._dump_dir = None; w._on_dump_started()          # untitled tab: the remembered folder
w._open_testing_pane()                             # no suite chosen yet: the same
d = os.path.realpath(tempfile.mkdtemp(prefix="work"))
path = os.path.join(d, "t.scad"); open(path, "w").write("cube(1);")
w.open_file_by_path(path, render=False)
w._dump_dir = None; w._on_dump_started()          # a saved script: its own folder
print(json.dumps({"asked": asked, "last": last, "work": d}), flush=True)
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_folder_pickers_never_start_in_the_cwd():
    # Given no start folder, Qt falls back to the cwd: System32 for a
    # Windows Start-menu launch, where Dump Pictures opened (#938).
    import json, subprocess, sys
    r = subprocess.run([sys.executable, "-c", PICKER_DRIVER], capture_output=True, text=True, timeout=60)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["asked"] == [out["last"], out["last"], out["work"]], r.stderr
