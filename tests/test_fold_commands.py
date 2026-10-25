"""Edit ▸ Fold/Unfold and Fold/Unfold All, also on the editor's right-click
menu (#928). A CodeEditor in a subprocess (Qt widgets, offscreen)."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor

SRC = """module a() {
    if (true) {
        cube(1);
    }
}
module b() {
    sphere(1);
}
c = 1;"""
e = CodeEditor(); e.setPlainText(SRC); e.show(); app.processEvents()
shown = lambda: [b for b in range(e.document().blockCount())
                 if e.document().findBlockByNumber(b).isVisible()]
def caret_to(bn):
    e.setTextCursor(QTextCursor(e.document().findBlockByNumber(bn)))
out = {}
out["fold_at"] = [e.fold_at(bn) for bn in range(9)]

e.toggle_fold_at(2)                  # inside if: folds the if
out["inner"] = shown()
e.toggle_fold_at(2)                  # hidden line 2's block is the if again
out["inner_again"] = shown()

caret_to(2)
e.fold_all()                         # first top-level open: fold a and b
out["all"] = shown()
out["caret"] = e.textCursor().blockNumber()
e.fold_all()                         # now folded: unfold everything
out["unfolded"] = shown()

e.toggle_fold(1)                     # an inner one folded, the outer open
e.fold_all()
out["all_over_inner"] = shown()
e.fold_all()
out["unfolded_inner_too"] = shown()
print(json.dumps(out))
'''


def test_fold_commands():
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True,
                          text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # The innermost block holding each line; line 8 is in none.
    assert out["fold_at"] == [0, 1, 1, 1, 0, 5, 5, 5, None]
    assert out["inner"] == [0, 1, 4, 5, 6, 7, 8]
    assert out["inner_again"] == list(range(9))
    assert out["all"] == [0, 5, 8], "every outermost block folded"
    assert out["caret"] == 0, "the caret leaves the fold for its first line"
    assert out["unfolded"] == list(range(9))
    assert out["all_over_inner"] == [0, 5, 8]
    assert out["unfolded_inner_too"] == list(range(9)), "Unfold All leaves nothing folded"
