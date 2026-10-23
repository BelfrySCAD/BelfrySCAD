"""Changes to the background image are undoable in the editor that shows it,
one step per change and one for a whole drag; and an image set up in an
editor opened from New is the one Edit as... finds on the saved literal
(#861). Qt widgets: a subprocess, offscreen."""
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
from belfryscad.window.background_image import BackgroundImage, _session
from belfryscad.window.data_viewer_path import PathViewer
from belfryscad.window.data_viewer_turtle import TurtleViewer

def place(vp):
    return None if vp.background is None else [vp.background.x, vp.background.y, vp.background.width]

out = {}
for name, dlg in (("path", PathViewer("p", [[0, 0], [10, 5]], editable=True, is_2d=True)),
                  ("turtle", TurtleViewer("t", ["move", 10], editable=True))):
    vp, stack = dlg._vp, dlg._undo_stack
    vp.set_background(BackgroundImage(path="i.png", width=40, x=5, y=7))
    vp._anchor_to_origin()                       # a menu action: one step
    vp._bg_drag = ("move", None, None, None)     # a drag: many edits, one step
    for i in range(3):
        vp.background.x += 1
        vp.background_edited()
    vp._bg_drag = None
    vp.background_edited()                       # the release
    steps = stack.count()
    after = place(vp)
    stack.undo()
    after_drag_undo = place(vp)
    stack.undo()
    after_both = place(vp)
    stack.redo(); stack.redo()
    vp.clear_background()
    cleared = place(vp)
    stack.undo()
    out[name] = {"steps": steps, "after": after, "undo_drag": after_drag_undo,
                 "undo_both": after_both, "redo": None, "cleared": cleared,
                 "unclear": place(vp), "session": _session.get(vp.background_key) is not None}
# New > Turtle Path, then Edit as... on the turtle it saved (#861's video).
from PySide6.QtWidgets import QMenu
from belfryscad.window.data_viewers import build_new_literal_menu, _open_turtle_editor
menu = QMenu()
build_new_literal_menu(menu, "", lambda t: None)
next(a for a in menu.actions() if a.text() == "Turtle Path...").trigger()
new = next(w for w in QApplication.topLevelWidgets() if isinstance(w, TurtleViewer) and w.isVisible())
out["new_title"] = new.windowTitle()
new._vp.set_background(BackgroundImage(path="ruler.png", width=60, x=1, y=2))
new._vp._anchor_to_origin()
new.close()
text = 'n = turtle(["move", 10]);'
_open_turtle_editor("", ["move", 10], lambda *a: None, None, text=text,
                    start=text.index("["), end=text.index("]") + 1)
again = next(w for w in QApplication.topLevelWidgets()
             if isinstance(w, TurtleViewer) and w.isVisible())
out["reopened"] = place(again._vp)
print(json.dumps(out)); sys.stdout.flush()
hard_exit(0)
'''


def test_background_changes_undo_one_step_each_and_one_per_drag():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    for name in ("path", "turtle"):
        r = out[name]
        assert r["steps"] == 2, name
        assert r["after"] == [3, 0, 40]
        assert r["undo_drag"] == [0, 0, 40]
        assert r["undo_both"] == [5, 7, 40]
        assert r["cleared"] is None
        assert r["unclear"] == [3, 0, 40]
        assert r["session"] is True, name     # back where reopening finds it
    assert out["new_title"] == "Turtle Editor"     # as Edit as... titles it
    assert out["reopened"] == [0, 0, 60]
