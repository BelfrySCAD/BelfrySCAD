"""Dropping a mesh inserts import(), an image surface().

The editor part runs in a subprocess with the offscreen platform, as
test_editor_trio.py does.
"""
import json
import os
import subprocess
import sys

from belfryscad.drop_files import statement_for


def test_statements():
    base = os.path.join(os.sep, "w", "proj")
    assert statement_for(os.path.join(base, "parts", "Knob.STL"), base) == 'import("parts/Knob.STL");'
    assert statement_for(os.path.join(base, "logo.png"), base) == 'surface(file="logo.png");'
    assert statement_for(os.path.join(os.sep, "w", "x.jpg"), base) == 'surface(file="../x.jpg");'
    assert statement_for(os.path.join(os.sep, "w", "a.3mf"), None) == f'import("{os.sep}w{os.sep}a.3mf");'.replace(os.sep, "/")
    assert statement_for(os.path.join(base, 'say "hi".svg'), base) == 'import("say \\"hi\\".svg");'
    for ext in (".scad", ".json", ".txt", ""):
        assert statement_for(os.path.join(base, "f" + ext), base) is None


DRIVER = '''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QMimeData, QUrl
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.main_window import MainWindow
out = {}
d = os.path.realpath(tempfile.mkdtemp())
scad = os.path.join(d, "main.scad")
open(scad, "w").write("module m() {\\n    cube(1);\\n}\\n")
w = MainWindow(); w.skip_unsaved_prompts = True
w._render = lambda *a, **k: None
w.open_file_by_path(scad)
tab = w._current_tab(); ed = tab.editor

def mime(*names):
    m = QMimeData(); m.setUrls([QUrl.fromLocalFile(os.path.join(d, n)) for n in names]); return m

c = ed.textCursor(); c.setPosition(ed.document().findBlockByNumber(1).position() + 6); ed.setTextCursor(c)
out["can"] = ed.canInsertFromMimeData(mime("parts/a.stl"))
ed.insertFromMimeData(mime("parts/a.stl", "img.png", "notes.txt"))
out["after_drop"] = ed.toPlainText()
w._undo_group.activeStack().undo()
out["after_undo"] = ed.toPlainText()
c = ed.textCursor(); c.setPosition(0); ed.setTextCursor(c)
class E:
    def __init__(s, m): s.m = m
    def mimeData(s): return s.m
    def acceptProposedAction(s): pass
w.dropEvent(E(mime("b.obj")))
out["window_drop"] = ed.toPlainText()
print("RESULT" + json.dumps(out)); import sys; sys.stdout.flush()
try:  # close first, as test_editor_trio.py explains (#496)
    w.persist_settings = False
    w.close()
except Exception:
    pass
from belfryscad.main import hard_exit; hard_exit(0)
'''


def test_editor_drop():
    r = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), None)
    assert line, r.stdout + r.stderr
    out = json.loads(line[6:])
    assert out["can"] is True
    assert out["after_drop"] == ('module m() {\n    import("parts/a.stl");\n'
                                 '    surface(file="img.png");\n    cube(1);\n}\n')
    assert out["after_undo"] == "module m() {\n    cube(1);\n}\n"
    assert out["window_drop"] == 'import("b.obj");\nmodule m() {\n    cube(1);\n}\n'
