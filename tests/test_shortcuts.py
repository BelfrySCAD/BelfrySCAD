"""Preferences > Keyboard (#821), in a real window: a changed shortcut lands
on the action and in every window, a taken key moves, and the standard
shortcuts stay put. Qt widgets, so it runs in a subprocess (offscreen)."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json, os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QAction, QKeySequence
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-test-"), seed=False)
import belfryscad.window.main_window as mw
from belfryscad.window import keyboard_pane, shortcuts
w1 = mw.MainWindow(); w1.skip_unsaved_prompts = True
w2 = mw.MainWindow(); w2.skip_unsaved_prompts = True
def act(w, ident):
    return next(b for b in w._shortcut_bindings if b.id == ident).target
def keys(w, ident):
    return [k.toString(QKeySequence.SequenceFormat.PortableText) for k in act(w, ident).shortcuts()]
out = {"ids": len(w1._shortcut_bindings)}
# Alt+letter opens each top-level menu on Windows and Linux (#839). Read from
# the titles: QKeySequence.mnemonic() is always empty on macOS, which has none.
out["mnemonics"] = [a.text() for a in w1.menuBar().actions()]
# And every command in them has one; the dynamic lists (Open Recent, Examples,
# the Window menu's documents) have none and are not bindings.
out["no_mnemonic"] = [b.id for b in w1._shortcut_bindings
                      if isinstance(b.target, QAction) and not b.id.startswith("Toolbar")
                      and "&" not in b.target.text().replace("&&", "")]
pane = keyboard_pane.KeyboardPane(w1._shortcut_bindings)
row = {b.id: i for i, b in enumerate(w1._shortcut_bindings)}
answers = []
QMessageBox.question = staticmethod(lambda *a, **k: (answers.append(a[2]), QMessageBox.StandardButton.Yes)[1])
QMessageBox.warning = staticmethod(lambda *a, **k: answers.append(a[2]))
def press(ident, key):
    pane._table.selectRow(row[ident]); pane._edit.setKeySequence(QKeySequence(key)); pane._on_key_entered()
# A free key: applied in both windows, and saved.
press("Edit > Comment", "Ctrl+Shift+K")
out["free"] = [keys(w1, "Edit > Comment"), keys(w2, "Edit > Comment")]
# A taken key: asked, then moved from Export to Quick Export.
press("File > Quick Export…", "Ctrl+E")
out["taken"] = [keys(w1, "File > Quick Export…"), keys(w1, "File > Export…"), len(answers)]
# A standard key: refused.
press("Edit > Comment", "Ctrl+C")
out["standard"] = [keys(w1, "Edit > Comment"), keys(w1, "Edit > Copy"), "standard" in answers[-1]]
# Restore Default on Export takes Ctrl+E back; Quick Export gets its own F7 back.
pane._table.selectRow(row["File > Export…"]); pane._restore_one()
out["restored"] = [keys(w1, "File > Export…"), keys(w1, "File > Quick Export…")]
# A new window starts with the saved changes.
w3 = mw.MainWindow(); w3.skip_unsaved_prompts = True
out["new_window"] = keys(w3, "Edit > Comment")
out["saved"] = json.loads(mw.app_settings().value("shortcuts/overrides"))
print(json.dumps(out)); sys.stdout.flush(); os._exit(0)
'''


def test_keyboard_preferences():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ids"] > 100
    assert out["no_mnemonic"] == []
    assert out["mnemonics"] == ["&File", "&Edit", "&Design", "&View", "&Window", "&Help"]
    assert out["free"] == [["Ctrl+Shift+K"], ["Ctrl+Shift+K"]]
    assert out["taken"] == [["Ctrl+E"], [], 1]
    assert out["standard"][0] == ["Ctrl+Shift+K"] and out["standard"][2]
    assert out["restored"] == [["Ctrl+E"], ["F7"]]
    assert out["new_window"] == ["Ctrl+Shift+K"]
    assert out["saved"] == {"Edit > Comment": "Ctrl+Shift+K"}
