"""Tests for belfryscad.window.debugger's DebugSession -- specifically the
debug-session-start version of the F6/Render stale-content bug (see
tests/test_render_worker.py and project_render_stale_buffer_bug memory):
_start_debug had the identical "if tab.file_path: parse_path =
tab.file_path" pattern, ignoring the live editor buffer for any saved tab.

Fixed the same way as _do_render: always parse a temp copy of the live
buffer, in the real file's own directory. That has a knock-on effect
_do_render's fix didn't: checkDebug() reports every checkpoint's origin as
the temp path too, so a saved tab's own breakpoints (collected and keyed
by its real file_path) would silently stop firing unless also registered
under the temp path -- _start_debug now duplicates the debugged tab's own
breakpoint set under that key. Verified here against the real evaluator.

DebugSession is a plain QObject (no QWidget/QOpenGLWidget) -- same
QObject-safe-under-pytest class as _RenderWorker (see
feedback_gl_qt_tests_crash_pytest). DebuggerPane (the QWidget that
consumes DebugSession's signals and does the origin->real-path remap for
navigation/display) is NOT tested here, consistent with this project's
QWidget-in-pytest avoidance -- verified instead via a throwaway offscreen
script during development."""

import os
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(["pytest-debug-session"])

from belfryscad.window.debugger import DebugSession


def _pump_until(cond, timeout=10):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        QCoreApplication.processEvents()
        time.sleep(0.01)


class TestDebugSessionUsesLiveBuffer:
    def test_breakpoint_fires_on_augmented_temp_path_key(self, tmp_path):
        # tmp_path (pytest's own fixture) is never under macOS's symlinked
        # /var/folders default tempdir, matching _start_debug's own
        # same-directory-as-the-real-file placement -- confirmed during
        # development that a bare tempfile.NamedTemporaryFile() (system
        # default dir) doesn't reproduce real breakpoint-matching behavior
        # here, an unrelated environment quirk, not a fix bug.
        main = tmp_path / "main.scad"
        main.write_text("width = 1;\ncube([width, 1, 1]);\n")  # stale on disk
        live_source = "width = 1;\nx = 2;\ncube([width, 1, 1]);\n"  # live: extra line

        _tmp = tempfile.NamedTemporaryFile(suffix=".scad", mode="w", delete=False, dir=str(tmp_path))
        _tmp.write(live_source)
        _tmp.close()
        parse_path = _tmp.name

        real_key = str(main.resolve())
        parse_key = str((tmp_path / os.path.basename(parse_path)).resolve())
        breakpoints = {real_key: {3}, parse_key: {3}}  # _start_debug's augmentation

        session = DebugSession()
        pauses = []
        session.paused.connect(lambda *a: pauses.append(a))
        session.finished.connect(lambda *a: None)
        session.start(parse_path, breakpoints, {}, current_file=parse_path)

        _pump_until(lambda: len(pauses) >= 1)
        assert pauses, "break-on-first never fired"
        session.resume("continue")
        _pump_until(lambda: len(pauses) >= 2)
        assert len(pauses) >= 2, "breakpoint on the live buffer's line never fired"

        origin, line, *_rest = pauses[1]
        assert line == 3
        assert os.path.realpath(origin) == os.path.realpath(parse_path)
        session.stop()

    def test_cleanup_path_removed_after_session_ends(self, tmp_path):
        main = tmp_path / "main.scad"
        main.write_text("cube(1);\n")
        _tmp = tempfile.NamedTemporaryFile(suffix=".scad", mode="w", delete=False, dir=str(tmp_path))
        _tmp.write("cube(2);\n")
        _tmp.close()
        parse_path = _tmp.name

        session = DebugSession()
        finished = []
        pauses = []
        session.finished.connect(lambda *a: finished.append(True))
        session.paused.connect(lambda *a: pauses.append(a))
        session.start(parse_path, {}, {}, current_file=parse_path, cleanup_path=parse_path)

        _pump_until(lambda: pauses)  # break-on-first always pauses before running
        assert pauses
        session.resume("continue")
        _pump_until(lambda: finished)
        assert finished
        assert not os.path.exists(parse_path)
