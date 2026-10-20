"""Auto-closing brackets and quotes, Preferences ▸ Editor (#830).

Real key presses into a CodeEditor, in a subprocess (Qt widgets, offscreen).
Each case is the text with `|` for the caret (`<`...`>` for a selection),
the keys to press, and the text and caret after.
"""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
from belfryscad.window.editor import CodeEditor
KEYS = {"BS": Qt.Key.Key_Backspace, "RET": Qt.Key.Key_Return}
def run(on, start, keys):
    e = CodeEditor(); e.set_auto_close(on)
    a, b = (start.index("<"), start.index(">") - 1) if "<" in start else (start.index("|"),) * 2
    e.setPlainText(start.replace("|", "").replace("<", "").replace(">", ""))
    c = e.textCursor(); c.setPosition(a); c.setPosition(b, QTextCursor.MoveMode.KeepAnchor); e.setTextCursor(c)
    for k in keys:
        if k in KEYS:
            QTest.keyClick(e, KEYS[k])
        else:
            QTest.keyClicks(e, k)
    c = e.textCursor(); t = e.toPlainText()
    if c.hasSelection():
        return t[:c.selectionStart()] + "<" + t[c.selectionStart():c.selectionEnd()] + ">" + t[c.selectionEnd():]
    return t[:c.position()] + "|" + t[c.position():]
cases = json.loads(sys.argv[1])
print(json.dumps([run(*c[:3]) for c in cases])); sys.stdout.flush(); os._exit(0)
'''

CASES = [
    # off (the default): nothing added
    (False, "|", ["("], "(|"),
    # pairs, and the closer it added is typed over
    (True, "|", ["("], "(|)"),
    (True, "|", ["cube(1", ")"], "cube(1)|"),
    (True, "|", ["[", "{", '"'], '[{"|"}]'),
    (True, "|", ['"ab"'], '"ab"|'),
    # a closer the user typed is left alone
    (True, "f(|)", [")"], "f()|)"),
    # only before whitespace, a closer or the end
    (True, "|x", ["("], "(|x"),
    (True, "f(|)", ["["], "f([|])"),
    (True, "a |;", ["("], "a (|);"),
    # never in a comment or a string, nor a quote after a word
    (True, "// |", ["("], "// (|"),
    (True, '"a |"', ["("], '"a (|"'),
    (True, "/* |", ["{"], "/* {|"),
    (True, "a|", ['"'], 'a"|'),
    # Backspace between the pair it added deletes both, not one it didn't
    (True, "|", ["(", "BS"], "|"),
    (True, "(|)", ["BS"], "|)"),
    # Enter between brackets puts the closer on its own line
    (True, "  x = |", ["[", "RET"], "  x = [\n      |\n  ]"),
    (False, "f(|)", ["RET"], "f(\n    |)"),
    # an opener over a selection wraps it
    (True, "a <bc> d", ["("], "a (<bc>) d"),
    (False, "a <bc> d", ["("], "a (| d"),
]


def test_auto_close():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(CASES)],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert [(c[1], c[2], g) for c, g in zip(CASES, got)] == [(c[1], c[2], c[3]) for c in CASES]
