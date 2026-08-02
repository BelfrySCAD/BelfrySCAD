"""Tests for belfryscad.window.main_window's _RenderWorker -- specifically
the F6/Render bug where a saved tab's render read tab.file_path directly
off disk instead of the live (possibly unsaved) editor buffer passed in as
*source*. Fixed by always writing *source* to a temp .scad file, in the
same directory as file_path (when set) so relative use/include still
resolves.

_RenderWorker itself is a plain QObject (no QWidget/QOpenGLWidget), unlike
the rest of window/main_window.py -- confirmed safe to instantiate and run
synchronously under pytest (5+ repeated full-suite runs, no crash/hang),
distinct from the QOpenGLWidget-under-offscreen hang documented in
feedback_gl_qt_tests_crash_pytest for the real windowed MainWindow/Viewport."""

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(["pytest-render-worker"])

from belfryscad.window.main_window import _RenderWorker


def _run(source: str, file_path: str | None) -> dict:
    worker = _RenderWorker(source, file_path, threading.Event(), {})
    captured = {}
    worker.ast_ready.connect(lambda nodes, rs, pp: captured.update(root_scope=rs, parse_path=pp))
    worker.finished.connect(lambda bodies, *_a: captured.update(bodies=bodies))
    worker.logged.connect(lambda msg: captured.setdefault('logs', []).append(msg))
    worker.parse_errored.connect(lambda msg: captured.update(parse_error=msg))
    worker.run()
    return captured


class TestRenderWorkerUsesLiveBuffer:
    def test_saved_tab_renders_live_edits_not_stale_disk_content(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("width = 1;\ncube([width, 1, 1]);\n")  # stale on disk

        live_source = "width = 99;\ncube([width, 1, 1]);\n"  # unsaved edit
        captured = _run(live_source, str(main))

        mesh = captured["bodies"][0].body.to_mesh()
        verts = np.asarray(mesh.vert_properties[:, :3])
        assert verts[:, 0].max() == 99.0
        assert "width = 1;" in main.read_text(), "on-disk file must not be touched"

    def test_temp_file_written_alongside_real_file_for_use_include(self, tmp_path):
        (tmp_path / "lib.scad").write_text("module deep() { cube(1); }\n")
        main = tmp_path / "main.scad"
        main.write_text("use <lib.scad>\ncube(1);\n")

        captured = _run("use <lib.scad>\ndeep();\n", str(main))

        assert captured["parse_path"] != str(main)
        assert os.path.dirname(captured["parse_path"]) == str(tmp_path)
        assert captured.get("bodies"), captured.get("parse_error") or captured.get("logs")

    def test_temp_file_cleaned_up_after_run(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("cube(1);\n")
        _run("cube(2);\n", str(main))
        leftovers = [p for p in tmp_path.iterdir() if p.name != "main.scad"]
        assert leftovers == []

    def test_unsaved_tab_still_works(self, tmp_path):
        # file_path=None (a never-saved "Untitled" tab) -- unaffected by
        # this fix, kept as a regression guard on the pre-existing path.
        captured = _run("cube(5);\n", None)
        mesh = captured["bodies"][0].body.to_mesh()
        verts = np.asarray(mesh.vert_properties[:, :3])
        assert verts[:, 0].max() == 5.0
