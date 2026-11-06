"""The gear menu every editor and viewer keeps its options in: built from
the window's own controls, which keep their state and signals. Qt widgets:
a subprocess, offscreen."""
import json
import os
import subprocess
import sys

DRIVER = r'''
import json
from PySide6.QtWidgets import (QApplication, QDialog, QCheckBox, QComboBox, QSpinBox,
                               QDoubleSpinBox, QPushButton, QInputDialog)
app = QApplication([])
from belfryscad.window import data_viewer_common as c

d = QDialog(); d.show()
cb = QCheckBox("Show Vertices"); cb.setChecked(True)
combo = QComboBox(); combo.addItems(["A", "B", "C"])
combo.model().item(2).setEnabled(False)
activated = []
combo.activated.connect(activated.append)
small = QSpinBox(); small.setRange(1, 4); small.setValue(3)
big = QDoubleSpinBox(); big.setRange(0, 1e9); big.setValue(2.5)
clicked = []
btn = QPushButton("Validate"); btn.clicked.connect(lambda: clicked.append(1))
gone = QPushButton("Hidden"); gone.hide()
off = QCheckBox("Disabled"); off.setEnabled(False)
z = QComboBox(); z.setEditable(True); z.addItems(["50%", "100%"])
typed = []
z.lineEdit().editingFinished.connect(lambda: typed.append(z.currentText()))
button = c.settings_button(d, [cb, None, ("Mode", combo), ("Degree", small), ("Length", big),
                               None, btn, gone, off, ("Z", z), None])
extra = QCheckBox("Show tiling")
button.add_settings(None, extra)
menu = button.menu()

def show():
    menu.aboutToShow.emit()
    return {a.text() or "-": a for a in menu.actions()}

out = {}
acts = show()
out["entries"] = [a.text() or "-" for a in menu.actions()]
out["checked"] = acts["Show Vertices"].isChecked()
acts["Show Vertices"].trigger()
out["cb_after"] = cb.isChecked()
mode = [a for a in acts["Mode"].menu().actions()]
out["mode"] = [[a.text(), a.isChecked(), a.isEnabled()] for a in mode]
mode[1].trigger()
out["combo_after"] = [combo.currentIndex(), activated]
out["degree"] = [a.text() for a in acts["Degree"].menu().actions()]
acts["Degree"].menu().actions()[0].trigger()
out["small_after"] = small.value()
c.QInputDialog.getDouble = staticmethod(lambda *a, **k: (7.25, True))
acts["Length: 2.5…"].trigger()
out["big_after"] = big.value()
acts["Validate"].trigger()
out["clicked"] = clicked
out["disabled"] = acts["Disabled"].isEnabled()
c.QInputDialog.getText = staticmethod(lambda *a, **k: ("150%", True))
[a for a in acts["Z"].menu().actions() if a.text() == "Other…"][0].trigger()
out["typed"] = typed
cb.setVisible(True); app.processEvents()
out["never_on_screen"] = cb.isVisible()
gone.show()
out["shown_later"] = "Hidden" in [a.text() for a in show().values()]
print(json.dumps(out))
'''


def test_the_gear_menu_drives_the_windows_own_controls():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["entries"] == ["Show Vertices", "-", "Mode", "Degree", "Length: 2.5…", "-",
                              "Validate", "Disabled", "Z", "-", "Show tiling"], \
        "hidden controls left out, separators only between entries"
    assert out["checked"] is True and out["cb_after"] is False
    assert out["mode"] == [["A", True, True], ["B", False, True], ["C", False, False]]
    assert out["combo_after"] == [1, [1]], "a pick also emits activated"
    assert out["degree"] == ["1", "2", "3", "4"]
    assert out["small_after"] == 1
    assert out["big_after"] == 7.25, "a wide range asks in a dialog"
    assert out["clicked"] == [1]
    assert out["disabled"] is False
    assert out["typed"] == ["150%"], "an editable box takes a typed value"
    assert out["never_on_screen"] is False, "the controls themselves never appear"
    assert out["shown_later"] is True
