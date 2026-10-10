"""#728: with wrap on, a very long line put the end of the file out of reach.

CodeEditor re-lays out every wrapped line with a hanging indent, so its
continuation rows are narrower than the ones Qt laid out -- and a long line
needs more of them. The scrollbar's range is the sum of each block's
lineCount(), which Qt set from ITS layout; left stale, one 1000-element line
alone kept the last lines several hundred pixels below the viewport.
Qt widgets: a subprocess, offscreen.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, sys, tempfile, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QPlainTextEdit
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor
v = ", ".join(str(i) for i in range(1, 1001))
ed = CodeEditor()
ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
ed.setPlainText(f"include <x.scad>\\n\\nv=[{v}];\\n\\necho(v);\\n\\nlast = 1;\\n")
ed.resize(400, 400)
ed.show()
for _ in range(10): app.processEvents(); time.sleep(0.01)
sb = ed.verticalScrollBar()
sb.setValue(sb.maximum())
for _ in range(10): app.processEvents(); time.sleep(0.01)
g = ed.blockBoundingGeometry(ed.document().lastBlock()).translated(ed.contentOffset())
print(json.dumps({"last_bottom": g.bottom(), "viewport": ed.viewport().height()}), flush=True)
os._exit(0)
'''


def test_the_end_of_the_file_can_be_scrolled_to_past_a_long_wrapped_line():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["last_bottom"] <= out["viewport"] + 1, out
