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
os._exit(0)                  # skip teardown of threads Qt left behind
'''


def test_any_open_remembers_the_folder_but_revealing_a_library_does_not():
    # Recent Files, a double-click in Explorer and a drop all arrive here,
    # never through the Open dialog, so it is where the folder is noted (#690).
    import json, subprocess, sys
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"lib": False, "work": True}, r.stderr
