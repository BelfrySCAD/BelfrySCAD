"""File ▸ Save All: every modified tab, in order; an untitled one asks for a
name, and cancelling that stops the rest.

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
import belfryscad.window.main_window as mw
w = mw.MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
d = tempfile.mkdtemp()
paths = {n: os.path.join(d, n + ".scad") for n in ("a", "b", "c")}
for n, p in paths.items():
    open(p, "w").write(n + "\\n")
    w.open_file_by_path(p)
tabs = {n: w._tabs.widget(i) for i, n in enumerate(paths)}
tabs["a"].editor.insertPlainText("A")                       # a modified, b not
w._new_document()
untitled = w._current_tab(); untitled.editor.insertPlainText("cube(1);")
tabs["c"].editor.insertPlainText("C")
w._tabs.setCurrentIndex(0)
b_mtime = os.stat(paths["b"]).st_mtime_ns
out = {"modified": [w._tabs.widget(i).is_modified for i in range(w._tabs.count())]}

answers = [("", "")]                                        # first: cancel the name dialog
mw.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: answers.pop(0))
out["cancel_result"] = w._save_all()
out["after_cancel"] = {n: open(p).read() for n, p in paths.items()}
out["untitled_still_modified"] = untitled.is_modified
out["untitled_brought_forward"] = w._current_tab() is untitled

new_path = os.path.join(d, "new.scad")
answers.append((new_path, ""))
out["ok_result"] = w._save_all()
out["new_file"] = open(new_path).read()
out["untitled_path"] = untitled.file_path == new_path
out["none_modified"] = not any(w._tabs.widget(i).is_modified for i in range(w._tabs.count()))
out["b_untouched"] = os.stat(paths["b"]).st_mtime_ns == b_mtime
print(json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def test_save_all():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["modified"] == [True, False, True, True]       # a, b, c, untitled
    # Cancelling the untitled tab's name stops there: a and c, before it, are saved.
    assert out["cancel_result"] is False
    assert out["after_cancel"] == {"a": "Aa\n", "b": "b\n", "c": "Cc\n"}
    assert out["untitled_still_modified"] and out["untitled_brought_forward"]
    # Named, everything is saved; an unmodified tab is not rewritten.
    assert out["ok_result"] is True
    assert out["new_file"] == "cube(1);"
    assert out["untitled_path"] and out["none_modified"] and out["b_untouched"]
