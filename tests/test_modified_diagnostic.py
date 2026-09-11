"""#394 diagnostic: BELFRYSCAD_DEBUG_MODIFIED=1 reports why a tab became modified."""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["BELFRYSCAD_DEBUG_MODIFIED"] = "1"
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import Qt, QEvent
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
logged = []
w.log = lambda msg: logged.append(msg)
d = tempfile.mkdtemp(); path = os.path.join(d, "t.scad"); open(path, "w").write("abc\\ndef\\n")
w.open_file_by_path(path)
tab = w._current_tab()
out = {"modified_after_open": tab.is_modified, "logged_after_open": any("DEBUG_MODIFIED" in m for m in logged)}
c = tab.editor.textCursor(); c.setPosition(2); tab.editor.setTextCursor(c)
tab.editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_X, Qt.KeyboardModifier.NoModifier, "X"))
diag = [m for m in logged if "DEBUG_MODIFIED" in m]
out["diag_count_after_one_edit"] = len(diag)
out["diag"] = diag[0] if diag else ""
tab.editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Y, Qt.KeyboardModifier.NoModifier, "Y"))
out["diag_count_after_two_edits"] = len([m for m in logged if "DEBUG_MODIFIED" in m])
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_diagnostic_reports_the_first_flip_with_stack_and_diff():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["modified_after_open"] is False
    assert out["logged_after_open"] is False        # a clean load says nothing
    assert out["diag_count_after_one_edit"] == 1
    assert "first difference at offset 2" in out["diag"]
    assert "read-only: False" in out["diag"]
    assert "t.scad" in out["diag"]
    assert "keyPressEvent" in out["diag"] or "_on_editor_changed" in out["diag"]
    assert out["diag_count_after_two_edits"] == 1   # only the flip, not every keystroke
    assert "DEBUG_MODIFIED" in proc.stderr           # and it reaches stderr too


def test_diagnostic_is_silent_without_the_variable():
    driver = DRIVER.replace('os.environ["BELFRYSCAD_DEBUG_MODIFIED"] = "1"', 'os.environ.pop("BELFRYSCAD_DEBUG_MODIFIED", None)')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen"); env.pop("BELFRYSCAD_DEBUG_MODIFIED", None)
    proc = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["diag_count_after_one_edit"] == 0


# #394: contentsChanged fires for format-only changes too. A light/dark
# switch rehighlights every tab, which emitted it with the text untouched
# and flipped the tab to modified -- visible as "* (ro)" on a read-only
# file the user had only just opened.
RETHEME_DRIVER = '''
import json, os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
d = tempfile.mkdtemp(); path = os.path.join(d, "t.scad"); open(path, "w").write("cube(1);\\n")
w.open_file_by_path(path)
tab = w._current_tab()
out = {"modified_after_open": tab.is_modified}

# Exactly what a theme switch does -- the stack in the bug report ends in
# OpenSCADHighlighter._retheme_syntax -> rehighlight().
tab.editor._highlighter._retheme_syntax()
out["modified_after_retheme"] = tab.is_modified
out["text_intact"] = tab.editor.toPlainText() == "cube(1);\\n"

# A real edit must still register.
tab.editor.setPlainText("cube(2);\\n")
out["modified_after_edit"] = tab.is_modified
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_rehighlight_does_not_mark_the_tab_modified():
    proc = subprocess.run([sys.executable, "-c", RETHEME_DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["modified_after_open"] is False
    assert out["text_intact"] is True
    assert out["modified_after_retheme"] is False   # the bug: this was True
    assert out["modified_after_edit"] is True       # and a real edit still counts
