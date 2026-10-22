"""The editor's selection keeps its colour without the focus (#838): Qt
uses the inactive colour then, which on Windows is nearly the background,
so a selection seemed to vanish as a right-click menu opened."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor
app = QApplication([])
pal = app.palette()      # an inactive highlight like Windows': the background
pal.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, QColor("#f0f0f0"))
app.setPalette(pal)
from belfryscad.window.editor import CodeEditor
e = CodeEditor()
p = e.palette()
H, A, I = QPalette.ColorRole.Highlight, QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive
out = [p.color(I, H).name() == p.color(A, H).name(), p.color(I, H).name() != "#f0f0f0"]
# A theme change still reaches the editor's active colour, and the inactive follows it.
pal.setColor(A, H, QColor("#123456")); app.setPalette(pal); app.processEvents()
p = e.palette()
out += [p.color(A, H).name() == "#123456", p.color(I, H).name() == "#123456"]
print(json.dumps(out)); sys.stdout.flush(); from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_selection_keeps_its_colour_without_the_focus():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [True, True, True, True]
