"""Design > Preview After Drag (#838): off, a drag edits the code without
previewing, for a model slow to preview; Undo and Redo still preview, so
the view never shows a part where the code no longer puts it. Qt widgets:
a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.main import hard_exit
sys.excepthook = lambda *e: (__import__("traceback").print_exception(*e), hard_exit(1))
from belfryscad.window.main_window import MainWindow

d = tempfile.mkdtemp(); p = os.path.join(d, "m.scad")
open(p, "w").write("cube(5);\n")
w = MainWindow(); w.skip_unsaved_prompts = True
w.open_file_by_path(p, render=False)
tab = w._current_tab()
w._rendered_tab = tab
renders = []
w._render = lambda *a, **k: renders.append(k.get("preview", True))
w._gizmo_target = lambda: (tab.editor.toPlainText(), 0)
out = {"default_on": w._act_preview_after_drag.isChecked()}

w._act_preview_after_drag.setChecked(False)
w._on_translate_committed(5, 0, 0)
out["off_text"] = tab.editor.toPlainText().strip()
out["off_renders"] = len(renders)
out["status"] = w.statusBar().currentMessage()
stack = w._stack_for(tab)
stack.undo(); out["after_undo"] = len(renders)
stack.redo(); out["after_redo"] = len(renders)

w._act_preview_after_drag.setChecked(True)
renders.clear()
w._on_translate_committed(0, 3, 0)
out["on_renders"] = len(renders)
from belfryscad.settings import app_settings
out["saved"] = app_settings().value("previewAfterDrag", type=bool)
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_preview_after_drag_off_skips_only_the_drags_own_preview():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["default_on"] is True
    assert out["off_text"].startswith("translate([5, 0, 0])")   # the code still changes
    assert out["off_renders"] == 0
    assert "F5" in out["status"]
    assert out["after_undo"] == 1 and out["after_redo"] == 2
    assert out["on_renders"] == 1
    assert out["saved"] is True
