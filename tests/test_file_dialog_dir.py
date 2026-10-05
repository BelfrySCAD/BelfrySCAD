"""File > Open / untitled Save As start in the last .scad folder, else
Documents -- never the cwd, which on Windows is the install folder (#690)."""
import os

from belfryscad import settings
from belfryscad.settings import app_settings, use_scratch_settings
from belfryscad.window.main_window import file_dialog_dir, remember_file_dir
from belfryscad import main


def test_remembers_the_folder_and_falls_back_when_it_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_scratch_dir", settings._scratch_dir)  # restored after
    use_scratch_settings(str(tmp_path / "settings"), seed=False)
    assert app_settings().fileName().startswith(str(tmp_path))  # never the real store
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
