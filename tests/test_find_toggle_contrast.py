"""#545: the find bar's option toggles must be legible when switched ON.

On Windows a flat checkable button drew its checked label white with no
fill, so "Aa" and ".*" vanished on the light bar exactly when they were on.
This grabs each toggle, checked, and requires its label to contrast with its
own background -- under every style this platform has, light and dark.

Widget instantiation crashes the pytest runner here, so the editor is
driven in a subprocess (see test_editor_annoyances.py).
"""
import json
import subprocess
import sys

DRIVER = r'''
import json, sys
from PySide6.QtWidgets import QApplication, QStyleFactory
from PySide6.QtCore import Qt

app = QApplication([])
style, scheme = sys.argv[1], sys.argv[2]
app.setStyle(QStyleFactory.create(style))
app.styleHints().setColorScheme(Qt.ColorScheme.Dark if scheme == "dark" else Qt.ColorScheme.Light)
from belfryscad.window.editor import CodeEditor

def lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())

ed = CodeEditor()
ed.show()
bar = ed._find_bar
bar.open_find()
out = {}
for name in ("_btn_case", "_btn_word", "_btn_regex"):
    btn = getattr(bar, name)
    btn.setChecked(True)
    app.processEvents()
    img = btn.grab().toImage()
    w, h = img.width(), img.height()
    bg = img.pixelColor(w // 2, 2)          # the fill, away from the label
    lb = lum(bg)
    best = max(((max(lb, lum(img.pixelColor(x, y))) + 0.05) /
                (min(lb, lum(img.pixelColor(x, y))) + 0.05))
               for x in range(w) for y in range(h // 4, 3 * h // 4))
    out[name] = round(best, 2)
print(json.dumps(out))
'''


def test_checked_toggles_are_legible_in_every_style():
    from PySide6.QtWidgets import QApplication, QStyleFactory   # noqa: F401
    styles = QStyleFactory.keys()
    for style in styles:
        for scheme in ("light", "dark"):
            r = subprocess.run([sys.executable, "-c", DRIVER, style, scheme],
                               capture_output=True, text=True, timeout=60,
                               env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"})
            assert r.returncode == 0, r.stderr
            contrast = json.loads(r.stdout.strip().splitlines()[-1])
            for name, ratio in contrast.items():
                assert ratio >= 3.0, f"{name} checked, {style}/{scheme}: {ratio}:1"
