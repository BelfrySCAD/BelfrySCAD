"""Edit ▸ Jump to Next Error: the cursor goes to the marked syntax error; with
none marked, the status bar says so and the cursor stays put.

Qt widgets, so it runs in a subprocess with the offscreen platform -- the
same pattern as test_editor_trio.py.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)  # never touch the real store
from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
tab = w._current_tab(); ed = tab.editor
ed.setPlainText("cube(1);\\nsphere(2)\\ncylinder(3);\\n")
out = {}
c = ed.textCursor(); c.setPosition(0); ed.setTextCursor(c)
w._jump_to_next_error()
out["none_pos"] = ed.textCursor().position()
out["none_msg"] = w.statusBar().currentMessage()
# The parser's message, marked the way a failed render marks it.
w._parse_error_to_editor(tab, "ERROR: Parser error: syntax error at line 3, column 1")
w._jump_to_next_error()
out["pos"] = ed.textCursor().position()
out["line"] = ed.textCursor().blockNumber() + 1
print(json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def test_jump_to_next_error():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["none_pos"] == 0 and out["none_msg"] == "No error to jump to."
    assert out["line"] == 3
    assert out["pos"] == len("cube(1);\nsphere(2)\n")
