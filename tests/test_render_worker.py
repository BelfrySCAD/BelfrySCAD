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


# -- #554: a warning flood must not swamp the UI thread ----------------------

def _run_collecting(source: str, **kw) -> dict:
    cancel = kw.pop("cancel", threading.Event())
    worker = _RenderWorker(source, None, cancel, {}, **kw)
    out = {"lines": [], "batches": 0, "repeats": {}}
    worker.logged.connect(lambda msg: out["lines"].append(msg))

    def batch(msgs):
        out["batches"] += 1
        for m in msgs:
            out["lines"].append(f"<repeat {m[1]}: {m[2]}>" if isinstance(m, tuple) else m)
    worker.logged_batch.connect(batch)
    worker.repeat_counts.connect(lambda counts: out["repeats"].update(counts))
    worker.finished.connect(lambda bodies, *_a: out.update(bodies=bodies))
    worker.run()
    return out


FLOOD = "for (i=[1:3000]) if (nope) cube(1);\ncube(1);\n"          # one warning, 3,000 times
DISTINCT = "for (i=[1:3000]) echo(i);\ncube(1);\n"                  # 3,000 different lines


class TestWarningFlood:
    def test_console_output_is_capped_and_the_rest_counted(self):
        out = _run_collecting(DISTINCT)
        echoes = [l for l in out["lines"] if l.startswith("ECHO:")]
        assert len(echoes) == _RenderWorker._SHOWN_CAP
        summary = [l for l in out["lines"] if "not shown" in l]
        assert summary and "2,000 more messages" in summary[0]
        assert out.get("bodies"), "the render itself still completes"

    def test_output_arrives_in_batches_not_one_signal_per_message(self):
        out = _run_collecting(DISTINCT)
        assert out["batches"] < _RenderWorker._SHOWN_CAP / 10

    def test_the_summary_comes_before_the_workers_own_last_word(self):
        # "Render: no geometry" / errors follow everything the script said.
        out = _run_collecting("for (i=[1:1500]) echo(i);\n")
        summary = next(i for i, l in enumerate(out["lines"]) if "not shown" in l)
        assert out["lines"][-1].startswith("Render: no geometry")
        assert summary < len(out["lines"]) - 1

    def test_stop_after_n_warnings(self):
        out = _run_collecting(FLOOD, max_warnings=5)
        assert len([l for l in out["lines"] if l.startswith("WARNING:")]) == 5
        assert any("Render stopped at 5 warnings" in l and "Stop rendering after" in l for l in out["lines"])
        assert "bodies" not in out

    def test_stop_at_the_first_warning_says_why_nothing_was_drawn(self):
        # #566: the warning was repeated under "Eval error", which read as the
        # script failing and never said a setting had stopped the render.
        out = _run_collecting(FLOOD, max_warnings=1)
        warnings = [l for l in out["lines"] if "WARNING:" in l]
        assert len(warnings) == 1                   # shown once, not quoted again
        assert not any(l.startswith("Eval error") for l in out["lines"])
        stop = [l for l in out["lines"] if l.startswith("Render stopped at the first warning")]
        assert stop and "nothing was drawn" in stop[0] and "Stop rendering after" in stop[0]
        assert "bodies" not in out

    # A closed tetrahedron with two faces wound backwards: kept and drawn by
    # the evaluator, with a warning saying so (#566).
    REVERSED = ("polyhedron([[0,0,0], [10,0,0], [0,10,0], [0,0,10]],\n"
                "           [[0,1,2], [0,1,3], [0,2,3], [1,2,3]]);\n")

    def test_a_drawn_anyway_warning_at_the_limit_still_draws_the_shape(self):
        # #566: stopping there drew nothing, hiding the shape the warning is
        # about. The render runs on; later output is counted, not shown.
        # Mesh warnings are raised while geometry is built, after every
        # warning from running the script, so the second one comes later.
        out = _run_collecting("translate([20,0,0]) " + self.REVERSED + self.REVERSED, max_warnings=1)
        assert len(out.get("bodies") or []) == 2, "the render finished and drew both"
        assert len([l for l in out["lines"] if "WARNING:" in l]) == 1
        note = [l for l in out["lines"] if l.startswith("Reached the first warning")]
        assert note and "drawn anyway" in note[0] and "1 later message not shown" in note[0]
        assert not any(l.startswith("Render stopped") for l in out["lines"])

    def test_another_warning_at_the_limit_still_stops(self):
        out = _run_collecting(FLOOD + self.REVERSED, max_warnings=1)
        assert "bodies" not in out
        assert any(l.startswith("Render stopped at the first warning") for l in out["lines"])

    def test_cancel_interrupts_a_running_evaluation_quietly(self):
        cancel = threading.Event()
        cancel.set()                     # as if pressed while it runs
        out = _run_collecting(FLOOD, cancel=cancel)
        assert "bodies" not in out
        assert not any("error" in l.lower() for l in out["lines"]), out["lines"][-3:]


class TestRepeatedMessages:
    def test_first_ten_then_one_line_counting_the_rest(self):
        out = _run_collecting(FLOOD)
        warnings = [l for l in out["lines"] if l.startswith("WARNING:")]
        assert len(warnings) == _RenderWorker._REPEAT_SHOWN
        repeat_lines = [l for l in out["lines"] if l.startswith("<repeat")]
        assert len(repeat_lines) == 1
        # right after the tenth copy
        assert out["lines"].index(repeat_lines[0]) == _RenderWorker._REPEAT_SHOWN   # after copies 0..9
        assert out["repeats"] == {0: 3000 - _RenderWorker._REPEAT_SHOWN}
        assert not any("not shown" in l for l in out["lines"]), "repeats are not the cap"

    def test_repeats_need_not_be_adjacent(self):
        # Two warnings taking turns inside a loop: each collapses on its own.
        out = _run_collecting("for (i=[1:500]) { if (nope) cube(1); if (nada) cube(1); }\ncube(1);\n")
        assert len([l for l in out["lines"] if l.startswith("WARNING:")]) == 20
        assert sorted(out["repeats"].values()) == [490, 490]

    def test_different_messages_are_all_printed(self):
        out = _run_collecting("for (i=[1:30]) echo(i);\n")
        assert len([l for l in out["lines"] if l.startswith("ECHO:")]) == 30
        assert out["repeats"] == {}
